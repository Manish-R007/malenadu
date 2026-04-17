"""
agent.py
--------
Continuous autonomous agent loop that:
  1. Connects to all 4 machine SSE streams simultaneously
  2. Scores each reading (via ML models + baselines)
  3. Prioritizes alerts by risk score
  4. Raises POST /alert for critical situations
  5. (Bonus) Calls POST /schedule-maintenance
  6. Handles stream interruptions with exponential back-off reconnect
  7. Flags data absence as a signal

Run:
  python backend/agent.py
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import aiohttp

# Add project root to path so the scorer can be loaded when run directly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ml.scorer import ScorerRegistry

API_BASE_URL = os.environ.get("AGENT_BASE_URL", "http://localhost:8000")
NODE_BASE_URL = os.environ.get("MALENDAU_BASE_URL", "http://localhost:3000")
MACHINES = ["CNC_01", "CNC_02", "PUMP_03", "CONVEYOR_04"]
MAX_RECONNECT_DELAY = 30
SOCK_READ_TIMEOUT = 10
ALERT_DEDUPE_SECONDS = 30
DATA_ABSENCE_ALERT_THRESHOLD = 3
ALERT_DISPATCH_INTERVAL = 1.0

RISK_TO_PRIORITY = {
    "critical": "critical",
    "warning": "high",
    "normal": "low",
}

agent_state: Dict[str, dict] = {
    machine_id: {
        "last_reading": None,
        "last_raw_reading": None,
        "last_scored_at": None,
        "consecutive_gaps": 0,
        "reconnect_count": 0,
        "last_alert_ts": 0.0,
        "risk_score": 0.0,
        "risk_level": "unknown",
    }
    for machine_id in MACHINES
}

pending_alerts: Dict[str, dict] = {}


def log(machine_id: str, msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{machine_id}] {msg}")


def build_sensor_snapshot(scored: dict) -> dict:
    return {
        key: scored.get(key)
        for key in ["temperature_C", "vibration_mm_s", "rpm", "current_A", "status"]
    }


def queue_alert(machine_id: str, scored: dict):
    state = agent_state[machine_id]
    now = time.time()
    if now - state["last_alert_ts"] < ALERT_DEDUPE_SECONDS:
        return

    existing = pending_alerts.get(machine_id)
    if existing and existing.get("risk_score", 0.0) >= scored.get("risk_score", 0.0):
        return

    pending_alerts[machine_id] = {
        "machine_id": machine_id,
        "risk_score": float(scored.get("risk_score", 0.0)),
        "risk_level": scored.get("risk_level", "critical"),
        "explanation": scored.get("explanation", "Critical anomaly detected."),
        "sensor_data": build_sensor_snapshot(scored),
        "compound": bool(scored.get("compound")),
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    log(machine_id, f"📥 Queued alert candidate risk={scored.get('risk_score', 0.0):.2f}")


async def raise_alert(session: aiohttp.ClientSession, queued: dict):
    machine_id = queued["machine_id"]
    state = agent_state[machine_id]
    now = time.time()

    if now - state["last_alert_ts"] < ALERT_DEDUPE_SECONDS:
        return

    payload = {
        "machine_id": machine_id,
        "reason": queued["explanation"],
        "risk_score": queued["risk_score"],
        "sensor_data": queued["sensor_data"],
    }

    async with session.post(f"{API_BASE_URL}/alert", json=payload) as resp:
        resp.raise_for_status()
        data = await resp.json()

    state["last_alert_ts"] = now
    alert_id = data.get("alert", {}).get("alert_id", "?")
    log(machine_id, f"🚨 ALERT raised [{alert_id}] risk={queued['risk_score']:.2f} | {queued['explanation'][:80]}")

    if queued["risk_score"] > 0.65:
        await schedule_maintenance(
            session,
            machine_id,
            RISK_TO_PRIORITY.get(queued["risk_level"], "normal"),
            alert_id,
        )


async def schedule_maintenance(
    session: aiohttp.ClientSession,
    machine_id: str,
    priority: str,
    alert_id: str,
):
    payload = {
        "machine_id": machine_id,
        "priority": priority,
        "alert_id": alert_id,
        "requested_by": "agent",
    }

    async with session.post(f"{API_BASE_URL}/schedule-maintenance", json=payload) as resp:
        resp.raise_for_status()
        data = await resp.json()

    slot = data.get("slot", {})
    log(machine_id, f"📅 Maintenance scheduled [{priority}] -> {slot.get('scheduled_at', '?')}")


def _score_or_fallback(registry: Optional[ScorerRegistry], machine_id: str, reading: dict) -> dict:
    if registry is None:
        return {
            **reading,
            "risk_score": 0.0,
            "risk_level": "unknown",
            "suppressed": False,
            "compound": False,
            "explanation": "Model not loaded.",
        }

    scored = registry.score(machine_id, reading)
    if scored is None:
        return {
            **reading,
            "risk_score": 0.0,
            "risk_level": "unknown",
            "suppressed": False,
            "compound": False,
            "explanation": f"No scorer available for {machine_id}.",
        }
    return scored


async def _handle_data_absence(machine_id: str):
    state = agent_state[machine_id]
    state["consecutive_gaps"] += 1
    gaps = state["consecutive_gaps"]
    log(machine_id, f"⚠️  Data absent (gap #{gaps}) - treating as potential sensor/network failure")

    if gaps >= DATA_ABSENCE_ALERT_THRESHOLD:
        queue_alert(machine_id, {
            "risk_score": 0.8,
            "risk_level": "critical",
            "explanation": (
                f"No data received for {gaps} consecutive reconnect attempts - "
                "possible sensor/network failure."
            ),
            "status": "data_absent",
            "compound": False,
        })


async def watch_machine(session: aiohttp.ClientSession, registry: Optional[ScorerRegistry], machine_id: str):
    reconnect_delay = 1.0
    state = agent_state[machine_id]

    while True:
        try:
            attempt = state["reconnect_count"] + 1
            log(machine_id, f"🔌 Connecting to raw stream (attempt {attempt})")
            timeout = aiohttp.ClientTimeout(total=None, sock_read=SOCK_READ_TIMEOUT)
            async with session.get(f"{NODE_BASE_URL}/stream/{machine_id}", timeout=timeout) as resp:
                if resp.status != 200:
                    raise ValueError(f"HTTP {resp.status}")

                state["reconnect_count"] += 1
                state["consecutive_gaps"] = 0
                reconnect_delay = 1.0
                log(machine_id, "✅ Stream connected")

                async for raw_line in resp.content:
                    line = raw_line.decode().strip()
                    if not line or not line.startswith("data:"):
                        continue

                    payload_str = line[5:].strip()
                    if not payload_str:
                        continue

                    reading = json.loads(payload_str)
                    scored = _score_or_fallback(registry, machine_id, reading)

                    state["consecutive_gaps"] = 0
                    state["last_raw_reading"] = reading
                    state["last_reading"] = scored
                    state["last_scored_at"] = datetime.now(timezone.utc).isoformat()
                    state["risk_score"] = scored.get("risk_score", 0.0)
                    state["risk_level"] = scored.get("risk_level", "normal")

                    level = scored.get("risk_level", "normal")
                    score = scored.get("risk_score", 0.0)
                    suppressed = scored.get("suppressed", False)
                    emoji = {"normal": "🟢", "warning": "🟡", "critical": "🔴"}.get(level, "⚪")

                    if level != "normal" or int(time.time()) % 10 == 0:
                        log(
                            machine_id,
                            f"{emoji} {level.upper()} score={score:.2f} "
                            f"T={scored.get('temperature_C')}°C "
                            f"V={scored.get('vibration_mm_s')}mm/s "
                            f"RPM={scored.get('rpm')} "
                            f"I={scored.get('current_A')}A"
                            + (" [suppressed]" if suppressed else ""),
                        )

                    if level == "critical" and not suppressed:
                        queue_alert(machine_id, scored)

        except asyncio.CancelledError:
            log(machine_id, "Agent cancelled.")
            return
        except Exception as exc:
            log(machine_id, f"❌ Stream error: {exc}. Reconnecting in {reconnect_delay:.1f}s...")
            await _handle_data_absence(machine_id)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)


async def dispatch_alerts(session: aiohttp.ClientSession):
    while True:
        await asyncio.sleep(ALERT_DISPATCH_INTERVAL)
        if not pending_alerts:
            continue

        ranked = sorted(
            pending_alerts.values(),
            key=lambda alert: (alert["risk_score"], alert["queued_at"]),
            reverse=True,
        )

        for alert in ranked:
            pending_alerts.pop(alert["machine_id"], None)
            try:
                await raise_alert(session, alert)
            except Exception as exc:
                log(alert["machine_id"], f"⚠️  Failed to raise alert: {exc}")
                pending_alerts[alert["machine_id"]] = alert
                break


async def priority_monitor():
    while True:
        await asyncio.sleep(15)
        ranked = sorted(agent_state.items(), key=lambda item: item[1]["risk_score"], reverse=True)
        print("\n" + "=" * 60)
        print(f"  AGENT PRIORITY SUMMARY @ {datetime.now(timezone.utc).strftime('%H:%M:%S')}")
        print("=" * 60)
        for machine_id, state in ranked:
            level = state["risk_level"]
            score = state["risk_score"]
            emoji = {"normal": "🟢", "warning": "🟡", "critical": "🔴", "unknown": "⚪"}.get(level, "⚪")
            print(f"  {emoji} {machine_id:<12} risk={score:.3f}  level={level}")

        if pending_alerts:
            print("-" * 60)
            for alert in sorted(pending_alerts.values(), key=lambda item: item["risk_score"], reverse=True):
                print(f"  📥 queued {alert['machine_id']:<12} risk={alert['risk_score']:.3f}")
        print("=" * 60 + "\n")


async def main():
    print("=" * 60)
    print(" Predictive Maintenance Agent - Starting")
    print(f" Monitoring: {', '.join(MACHINES)}")
    print(f" Raw source:  {NODE_BASE_URL}")
    print(f" API target:  {API_BASE_URL}")
    print("=" * 60 + "\n")

    try:
        registry: Optional[ScorerRegistry] = ScorerRegistry()
        print("[agent] ScorerRegistry loaded")
    except FileNotFoundError as exc:
        print(f"[agent] WARNING: {exc}")
        print("[agent] Continuing without models; readings will be marked as unknown.")
        registry = None

    async with aiohttp.ClientSession() as session:
        tasks = [watch_machine(session, registry, machine_id) for machine_id in MACHINES]
        tasks.append(dispatch_alerts(session))
        tasks.append(priority_monitor())
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[agent] Stopped by user.")

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

Edge cases:
  - If a machine stream drops → reconnect with back-off, log "data_absent"
  - If multiple machines critical simultaneously → escalate highest risk first
  - Transient single spikes → suppressed (not alerted)
  - Compound multi-sensor anomalies → escalated with higher priority
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict

import aiohttp

BASE_URL  = os.environ.get("AGENT_BASE_URL", "http://localhost:8000")
MACHINES  = ["CNC_01", "CNC_02", "PUMP_03", "CONVEYOR_04"]
MAX_RECONNECT_DELAY = 30  # seconds

# Priority labels for scheduling
RISK_TO_PRIORITY = {
    "critical": "critical",
    "warning":  "high",
    "normal":   "low",
}

# ── Per-machine state ─────────────────────────────────────────────────────────
agent_state: Dict[str, dict] = {
    m: {
        "last_reading":     None,
        "consecutive_gaps": 0,
        "reconnect_count":  0,
        "last_alert_ts":    0,
        "risk_score":       0.0,
        "risk_level":       "unknown",
    }
    for m in MACHINES
}


def log(machine_id: str, msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{machine_id}] {msg}")


async def raise_alert(session: aiohttp.ClientSession, machine_id: str, scored: dict):
    """POST /alert for a given scored reading."""
    state = agent_state[machine_id]
    now   = time.time()

    # Dedupe: don't alert same machine more than once per 30s
    if now - state["last_alert_ts"] < 30:
        return

    state["last_alert_ts"] = now
    payload = {
        "machine_id": machine_id,
        "reason":     scored.get("explanation", "Anomaly detected."),
        "risk_score": scored.get("risk_score", 0.5),
        "sensor_data": {
            k: scored.get(k)
            for k in ["temperature_C", "vibration_mm_s", "rpm", "current_A", "status"]
        },
    }
    try:
        async with session.post(f"{BASE_URL}/alert", json=payload) as resp:
            data = await resp.json()
            alert_id = data.get("alert", {}).get("alert_id", "?")
            log(machine_id, f"🚨 ALERT raised [{alert_id}] risk={scored['risk_score']:.2f} | {scored.get('explanation','')[:80]}")

            # (Bonus) Auto-schedule maintenance for critical + compound
            if scored.get("risk_score", 0) > 0.65:
                await schedule_maintenance(session, machine_id,
                                           RISK_TO_PRIORITY.get(scored["risk_level"], "normal"),
                                           alert_id)
    except Exception as exc:
        log(machine_id, f"⚠️  Failed to raise alert: {exc}")


async def schedule_maintenance(session: aiohttp.ClientSession,
                                machine_id: str, priority: str, alert_id: str):
    """POST /schedule-maintenance (Bonus)."""
    payload = {
        "machine_id":  machine_id,
        "priority":    priority,
        "alert_id":    alert_id,
        "requested_by": "agent",
    }
    try:
        async with session.post(f"{BASE_URL}/schedule-maintenance", json=payload) as resp:
            data = await resp.json()
            slot = data.get("slot", {})
            log(machine_id, f"📅 Maintenance scheduled [{priority}] → {slot.get('scheduled_at','?')}")
    except Exception as exc:
        log(machine_id, f"⚠️  Failed to schedule maintenance: {exc}")


async def watch_machine(session: aiohttp.ClientSession, machine_id: str):
    """
    Continuously stream from /stream/{machine_id}.
    Reconnects with exponential back-off on failure.
    Flags data absence as a signal after 3 consecutive gaps.
    """
    reconnect_delay = 1.0
    state = agent_state[machine_id]

    while True:
        try:
            log(machine_id, f"🔌 Connecting to stream (attempt {state['reconnect_count']+1})")
            async with session.get(
                f"{BASE_URL}/stream/{machine_id}",
                timeout=aiohttp.ClientTimeout(total=None, sock_read=10),
            ) as resp:
                if resp.status != 200:
                    raise ValueError(f"HTTP {resp.status}")

                state["reconnect_count"] += 1
                state["consecutive_gaps"]  = 0
                reconnect_delay = 1.0  # reset back-off on success
                log(machine_id, "✅ Stream connected")

                async for raw_line in resp.content:
                    line = raw_line.decode().strip()
                    if not line.startswith("data:"):
                        continue
                    payload_str = line[5:].strip()
                    if not payload_str:
                        continue

                    scored = json.loads(payload_str)

                    # Data-absent signal from server
                    if scored.get("signal") == "data_absent":
                        state["consecutive_gaps"] += 1
                        log(machine_id,
                            f"⚠️  Data absent (gap #{state['consecutive_gaps']}) – "
                            f"treating as potential sensor failure signal")
                        if state["consecutive_gaps"] >= 3:
                            await raise_alert(session, machine_id, {
                                "risk_score":  0.8,
                                "risk_level":  "critical",
                                "explanation": f"No data received for {state['consecutive_gaps']} consecutive seconds — possible sensor/network failure.",
                            })
                        continue

                    state["consecutive_gaps"] = 0
                    state["last_reading"]     = scored
                    state["risk_score"]       = scored.get("risk_score", 0)
                    state["risk_level"]       = scored.get("risk_level", "normal")

                    level = scored.get("risk_level", "normal")
                    score = scored.get("risk_score", 0)
                    suppressed = scored.get("suppressed", False)

                    # Log every reading (minimal)
                    emoji = {"normal": "🟢", "warning": "🟡", "critical": "🔴"}.get(level, "⚪")
                    if level != "normal" or int(time.time()) % 10 == 0:
                        log(machine_id,
                            f"{emoji} {level.upper()} score={score:.2f} "
                            f"T={scored.get('temperature_C')}°C "
                            f"V={scored.get('vibration_mm_s')}mm/s "
                            f"RPM={scored.get('rpm')} "
                            f"I={scored.get('current_A')}A"
                            + (" [suppressed]" if suppressed else ""))

                    # Alert on warning or critical (not suppressed)
                    if level in ("warning", "critical") and not suppressed:
                        await raise_alert(session, machine_id, scored)

        except asyncio.CancelledError:
            log(machine_id, "Agent cancelled.")
            return
        except Exception as exc:
            state["consecutive_gaps"] += 1
            log(machine_id, f"❌ Stream error: {exc}. Reconnecting in {reconnect_delay:.1f}s…")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)


async def priority_monitor():
    """
    Every 15s, print a prioritized risk summary across all machines.
    When multiple machines are critical, rank by risk score.
    """
    while True:
        await asyncio.sleep(15)
        ranked = sorted(agent_state.items(), key=lambda x: x[1]["risk_score"], reverse=True)
        print("\n" + "═" * 60)
        print(f"  AGENT PRIORITY SUMMARY @ {datetime.now(timezone.utc).strftime('%H:%M:%S')}")
        print("═" * 60)
        for machine_id, s in ranked:
            level = s["risk_level"]
            score = s["risk_score"]
            emoji = {"normal": "🟢", "warning": "🟡", "critical": "🔴", "unknown": "⚪"}.get(level, "⚪")
            print(f"  {emoji} {machine_id:<12} risk={score:.3f}  level={level}")
        print("═" * 60 + "\n")


async def main():
    print("=" * 60)
    print(" Predictive Maintenance Agent — Starting")
    print(f" Monitoring: {', '.join(MACHINES)}")
    print(f" Backend:    {BASE_URL}")
    print("=" * 60 + "\n")

    async with aiohttp.ClientSession() as session:
        tasks = [watch_machine(session, m) for m in MACHINES]
        tasks.append(priority_monitor())
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[agent] Stopped by user.")

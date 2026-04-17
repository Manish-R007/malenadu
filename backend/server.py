"""
server.py
---------
FastAPI backend for the Predictive Maintenance Agent.

Endpoints:
  GET  /stream/{machine_id}          – Live sensor readings via SSE (1/sec)
  GET  /history/{machine_id}         – Last 7 days of historical readings (JSON)
  POST /alert                        – Raise a maintenance alert with reason
  POST /schedule-maintenance         – (Bonus) Auto-book a maintenance slot
  GET  /status                       – Health + current risk scores for all machines
  GET  /alerts                       – List all raised alerts
  GET  /dashboard-data               – Snapshot of all machines for polling

Edge cases handled:
  - Stream reconnection: client disconnect detected, generator cleaned up
  - Missing machine_id: 404 with clear message
  - Absent data: gap flagged as its own signal in SSE stream
  - Concurrent streams: one generator per machine_id (thread-safe via asyncio)
  - Priority queue: alerts sorted by risk_score descending
"""

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# ── Add project root to path ──────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ml.scorer import ScorerRegistry

app = FastAPI(title="Predictive Maintenance Agent API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global state ──────────────────────────────────────────────────────────────
scorer_registry: Optional[ScorerRegistry] = None
alerts_store:    List[dict]               = []   # priority-sorted alert log
machine_state:   Dict[str, dict]          = {}   # latest reading per machine
alert_priority_queue: List[dict]          = []   # sorted by risk_score desc

MACHINES = ["CNC_01", "CNC_02", "PUMP_03", "CONVEYOR_04"]
NODE_BASE_URL = os.environ.get("MALENDAU_BASE_URL", "http://localhost:3000")
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global scorer_registry
    try:
        scorer_registry = ScorerRegistry()
        print("[server] ScorerRegistry loaded ✓")
    except FileNotFoundError as e:
        print(f"[server] WARNING: {e}")
        print("[server] Run `python ml/train_model.py` first, then restart.")
        scorer_registry = None


async def _request_node(method: str, path: str, *, json_body: Optional[dict] = None):
    url = f"{NODE_BASE_URL}{path}"
    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            async with session.request(method, url, json=json_body) as resp:
                if resp.status >= 400:
                    detail = await resp.text()
                    raise HTTPException(status_code=resp.status, detail=detail or f"Node API error for {path}")
                return await resp.json()
    except HTTPException:
        raise
    except aiohttp.ClientError as exc:
        raise HTTPException(status_code=503, detail=f"Node API unavailable at {NODE_BASE_URL}: {exc}") from exc


def _record_local_alert(machine_id: str, reason: str, risk_score: float, sensor_data: Optional[dict],
                        *, node_alert_id: Optional[str] = None, timestamp: Optional[str] = None,
                        auto: bool = False) -> dict:
    alert = {
        "alert_id": str(uuid.uuid4())[:8],
        "node_alert_id": node_alert_id,
        "machine_id": machine_id,
        "reason": reason,
        "risk_score": risk_score,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "sensor_data": sensor_data or {},
        "acknowledged": False,
        "auto": auto,
    }
    alerts_store.append(alert)
    alerts_store.sort(key=lambda a: a["risk_score"], reverse=True)
    return alert


def _enrich_node_alerts(node_alerts: List[dict], limit: int) -> dict:
    local_by_node_id = {
        alert["node_alert_id"]: alert
        for alert in alerts_store
        if alert.get("node_alert_id")
    }
    enriched = []
    for alert in node_alerts[:limit]:
        local = local_by_node_id.get(alert.get("id"))
        enriched.append({
            "alert_id": alert.get("id") or (local or {}).get("alert_id"),
            "machine_id": alert.get("machine_id"),
            "reason": alert.get("reason"),
            "risk_score": (local or {}).get("risk_score", 0.0),
            "timestamp": alert.get("triggered_at") or (local or {}).get("timestamp"),
            "sensor_data": alert.get("reading") or (local or {}).get("sensor_data", {}),
            "acknowledged": False,
            "auto": (local or {}).get("auto", False),
        })
    return {"count": len(node_alerts), "alerts": enriched}


# ── SSE stream endpoint ───────────────────────────────────────────────────────
@app.get("/stream/{machine_id}")
async def stream(machine_id: str, request: Request):
    if machine_id not in MACHINES:
        raise HTTPException(status_code=404, detail=f"Unknown machine_id: {machine_id}")

    async def generator():
        consecutive_gaps = 0
        timeout = aiohttp.ClientTimeout(total=None, sock_read=15)

        while not await request.is_disconnected():
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(f"{NODE_BASE_URL}/stream/{machine_id}") as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"Node stream returned HTTP {resp.status}")

                        async for raw_line in resp.content:
                            if await request.is_disconnected():
                                return

                            line = raw_line.decode().strip()
                            if not line or not line.startswith("data:"):
                                continue

                            reading = json.loads(line[5:].strip())

                            if scorer_registry:
                                scored = scorer_registry.score(machine_id, reading)
                            else:
                                scored = {
                                    **reading,
                                    "risk_score": 0.0,
                                    "risk_level": "unknown",
                                    "explanation": "Model not loaded.",
                                }

                            machine_state[machine_id] = scored

                            if scored.get("risk_level") == "critical" and not scored.get("suppressed"):
                                await _raise_alert_internal(machine_id, scored)

                            consecutive_gaps = 0
                            yield f"data: {json.dumps(scored)}\n\n"

            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_gaps += 1
                gap_event = {
                    "machine_id": machine_id,
                    "timestamp":  datetime.now(timezone.utc).isoformat(),
                    "error":      str(exc),
                    "signal":     "data_absent",
                    "consecutive_gaps": consecutive_gaps,
                }
                yield f"data: {json.dumps(gap_event)}\n\n"
                await asyncio.sleep(min(2 * consecutive_gaps, 10))

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ── History endpoint ──────────────────────────────────────────────────────────
@app.get("/history/{machine_id}")
async def get_history(machine_id: str, limit: int = 500):
    if machine_id not in MACHINES:
        raise HTTPException(status_code=404, detail=f"Unknown machine_id: {machine_id}")
    data = await _request_node("GET", f"/history/{machine_id}")
    readings = data.get("readings", [])[-limit:]
    return {"machine_id": machine_id, "count": len(readings), "data": readings}


# ── Alert endpoint ────────────────────────────────────────────────────────────
class AlertRequest(BaseModel):
    machine_id: str
    reason:     str
    risk_score: float = 0.5
    sensor_data: Optional[dict] = None


@app.post("/alert")
async def raise_alert(payload: AlertRequest):
    node_data = await _request_node("POST", "/alert", json_body={
        "machine_id": payload.machine_id,
        "reason": payload.reason,
        "reading": payload.sensor_data or {},
    })
    node_alert = node_data.get("alert", {})
    alert = _record_local_alert(
        payload.machine_id,
        payload.reason,
        payload.risk_score,
        payload.sensor_data,
        node_alert_id=node_alert.get("id"),
        timestamp=node_alert.get("triggered_at"),
    )
    return {"status": "alert_raised", "alert": alert}


async def _raise_alert_internal(machine_id: str, scored: dict):
    """Auto-raise alert from agent loop (deduped within 30s per machine)."""
    now = time.time()
    for a in reversed(alerts_store[-20:]):
        if a["machine_id"] == machine_id:
            ts = datetime.fromisoformat(a["timestamp"]).timestamp()
            if now - ts < 30:
                return   # dedupe: already alerted this machine recently
    sensor_data = {k: scored.get(k) for k in ["temperature_C", "vibration_mm_s", "rpm", "current_A", "status"]}
    node_data = await _request_node("POST", "/alert", json_body={
        "machine_id": machine_id,
        "reason": scored.get("explanation", "Critical anomaly detected."),
        "reading": sensor_data,
    })
    node_alert = node_data.get("alert", {})
    _record_local_alert(
        machine_id,
        scored.get("explanation", "Critical anomaly detected."),
        scored.get("risk_score", 1.0),
        sensor_data,
        node_alert_id=node_alert.get("id"),
        timestamp=node_alert.get("triggered_at"),
        auto=True,
    )


# ── Schedule maintenance endpoint (Bonus) ────────────────────────────────────
class ScheduleRequest(BaseModel):
    machine_id:  str
    priority:    str = "normal"   # low / normal / high / critical
    alert_id:    Optional[str] = None
    requested_by: str = "agent"


@app.post("/schedule-maintenance")
async def schedule_maintenance(payload: ScheduleRequest):
    node_data = await _request_node("POST", "/schedule-maintenance", json_body={
        "machine_id": payload.machine_id,
    })
    booking = node_data.get("booking", {})
    slot = {
        "booking_id": booking.get("id"),
        "machine_id": booking.get("machine_id", payload.machine_id),
        "priority": payload.priority,
        "scheduled_at": booking.get("slot"),
        "alert_id": payload.alert_id,
        "requested_by": payload.requested_by,
        "status": "confirmed",
    }
    return {"status": "scheduled", "slot": slot}


# ── Status / dashboard endpoints ──────────────────────────────────────────────
@app.get("/status")
async def status():
    node_alerts = await _request_node("GET", "/alerts")
    return {
        "machines": MACHINES,
        "model_loaded": scorer_registry is not None,
        "active_alerts": node_alerts.get("count", 0),
        "machine_states": machine_state,
    }


@app.get("/alerts")
async def get_alerts(limit: int = 50):
    node_alerts = await _request_node("GET", "/alerts")
    return _enrich_node_alerts(node_alerts.get("alerts", []), limit)


@app.get("/dashboard-data")
async def dashboard_data():
    """Polling endpoint for the frontend dashboard."""
    node_alerts = await _request_node("GET", "/alerts")
    enriched_alerts = _enrich_node_alerts(node_alerts.get("alerts", []), 10)
    return {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "machine_states": machine_state,
        "recent_alerts": enriched_alerts["alerts"],
        "summary": {
            m: {
                "risk_level": machine_state.get(m, {}).get("risk_level", "unknown"),
                "risk_score": machine_state.get(m, {}).get("risk_score", 0),
            }
            for m in MACHINES
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

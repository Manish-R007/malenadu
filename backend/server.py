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
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
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
maintenance_store: List[dict]             = []   # newest first
machine_state:   Dict[str, dict]          = {}   # latest reading per machine
alert_priority_queue: List[dict]          = []   # sorted by risk_score desc
dashboard_subscribers: List[asyncio.Queue] = []
machine_subscribers: List[asyncio.Queue] = []

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
    global alert_priority_queue
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
    alert_priority_queue = list(alerts_store)
    return alert


async def _publish_dashboard_event(kind: str, payload: dict):
    if not dashboard_subscribers:
        return

    event = {
        "event": kind,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    stale = []
    for queue in dashboard_subscribers:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            stale.append(queue)

    for queue in stale:
        if queue in dashboard_subscribers:
            dashboard_subscribers.remove(queue)


async def _publish_machine_event(payload: dict):
    if not machine_subscribers:
        return

    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }

    stale = []
    for queue in machine_subscribers:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            stale.append(queue)

    for queue in stale:
        if queue in machine_subscribers:
            machine_subscribers.remove(queue)


def _sort_alerts_by_risk(alerts: List[dict]) -> List[dict]:
    return sorted(
        alerts,
        key=lambda alert: (
            float(alert.get("risk_score", 0.0)),
            alert.get("timestamp") or "",
        ),
        reverse=True,
    )


def _enrich_node_alerts(node_alerts: List[dict], limit: int) -> dict:
    local_by_node_id = {
        alert["node_alert_id"]: alert
        for alert in alerts_store
        if alert.get("node_alert_id")
    }
    enriched = []
    for alert in node_alerts:
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
    enriched = _sort_alerts_by_risk(enriched)
    return {"count": len(node_alerts), "alerts": enriched[:limit]}


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _build_sensor_stats(readings: List[dict]) -> dict:
    stats = {}
    for sensor in ["temperature_C", "vibration_mm_s", "rpm", "current_A"]:
        values = [
            float(reading[sensor])
            for reading in readings
            if reading.get(sensor) is not None
        ]
        if not values:
            stats[sensor] = {"min": None, "max": None, "avg": None}
            continue
        stats[sensor] = {
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "avg": round(sum(values) / len(values), 4),
        }
    return stats


def _serialize_warning_event(reading: dict) -> dict:
    return {
        "timestamp": reading.get("timestamp"),
        "status": reading.get("status"),
        "temperature_C": reading.get("temperature_C"),
        "vibration_mm_s": reading.get("vibration_mm_s"),
        "rpm": reading.get("rpm"),
        "current_A": reading.get("current_A"),
    }


def _warning_counts_by_day(readings: List[dict]) -> List[dict]:
    grouped: Dict[str, dict] = defaultdict(lambda: {"warning": 0, "fault": 0})
    for reading in readings:
        ts = _parse_timestamp(reading.get("timestamp"))
        if ts is None:
            continue
        key = ts.date().isoformat()
        status = (reading.get("status") or "").lower()
        if status == "warning":
            grouped[key]["warning"] += 1
        elif status == "fault":
            grouped[key]["fault"] += 1

    return [
        {"date": day, "warning": counts["warning"], "fault": counts["fault"]}
        for day, counts in sorted(grouped.items())
    ]


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

                            consecutive_gaps = 0
                            await _publish_machine_event(scored)
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
                await _publish_machine_event(gap_event)
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


@app.get("/machine-events")
async def machine_events(request: Request):
    async def generator():
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        machine_subscribers.append(queue)

        snapshot = {
            "type": "snapshot",
            "machines": [machine_state.get(machine_id) for machine_id in MACHINES if machine_state.get(machine_id)],
        }
        yield f"data: {json.dumps(snapshot)}\n\n"

        try:
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            if queue in machine_subscribers:
                machine_subscribers.remove(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    await _publish_dashboard_event("alert", alert)
    return {"status": "alert_raised", "alert": alert}


# ── Schedule maintenance endpoint (Bonus) ────────────────────────────────────
class ScheduleRequest(BaseModel):
    machine_id:  str
    priority:    str = "normal"   # low / normal / high / critical
    alert_id:    Optional[str] = None
    risk_score:  float = 0.0
    requested_by: str = "agent"


@app.post("/schedule-maintenance")
async def schedule_maintenance(payload: ScheduleRequest):
    node_data = await _request_node("POST", "/schedule-maintenance", json_body={
        "machine_id": payload.machine_id,
        "priority": payload.priority,
        "risk_score": payload.risk_score,
    })
    booking = node_data.get("booking", {})
    slot = {
        "booking_id": booking.get("id"),
        "machine_id": booking.get("machine_id", payload.machine_id),
        "priority": payload.priority,
        "risk_score": booking.get("risk_score", payload.risk_score),
        "scheduled_at": booking.get("slot"),
        "alert_id": payload.alert_id,
        "requested_by": payload.requested_by,
        "status": "confirmed",
    }
    maintenance_store[:] = [
        item for item in maintenance_store
        if item.get("machine_id") != slot["machine_id"]
    ]
    maintenance_store.insert(0, slot)
    await _publish_dashboard_event("maintenance", slot)
    return {"status": "scheduled", "slot": slot}


@app.get("/maintenance")
async def get_maintenance(limit: int = 20):
    return {"count": len(maintenance_store), "slots": maintenance_store[:limit]}


@app.get("/dashboard-events")
async def dashboard_events(request: Request):
    async def generator():
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        dashboard_subscribers.append(queue)

        snapshot = {
            "alerts": alerts_store[:12],
            "maintenance": maintenance_store[:10],
        }
        yield f"data: {json.dumps({'event': 'snapshot', 'payload': snapshot, 'timestamp': datetime.now(timezone.utc).isoformat()})}\n\n"

        try:
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            if queue in dashboard_subscribers:
                dashboard_subscribers.remove(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    ranked_machines = sorted(
        [
            {
                "machine_id": machine_id,
                "risk_level": machine_state.get(machine_id, {}).get("risk_level", "unknown"),
                "risk_score": machine_state.get(machine_id, {}).get("risk_score", 0.0),
                "timestamp": machine_state.get(machine_id, {}).get("timestamp"),
                "explanation": machine_state.get(machine_id, {}).get("explanation"),
            }
            for machine_id in MACHINES
        ],
        key=lambda item: item["risk_score"],
        reverse=True,
    )
    return {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "machine_states": machine_state,
        "recent_alerts": enriched_alerts["alerts"],
        "recent_maintenance": maintenance_store[:10],
        "ranked_machines": ranked_machines,
        "summary": {
            m: {
                "risk_level": machine_state.get(m, {}).get("risk_level", "unknown"),
                "risk_score": machine_state.get(m, {}).get("risk_score", 0),
            }
            for m in MACHINES
        },
    }


@app.get("/analytics/{machine_id}")
async def machine_analytics(machine_id: str, for_date: Optional[str] = None):
    if machine_id not in MACHINES:
        raise HTTPException(status_code=404, detail=f"Unknown machine_id: {machine_id}")

    try:
        selected_date = date.fromisoformat(for_date) if for_date else datetime.now(timezone.utc).date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="for_date must be in YYYY-MM-DD format") from exc

    history_payload = await _request_node("GET", f"/history/{machine_id}")
    readings = history_payload.get("readings", [])

    dated_readings = []
    for reading in readings:
        ts = _parse_timestamp(reading.get("timestamp"))
        if ts is None:
            continue
        dated_readings.append((ts, reading))

    selected_readings = [
        reading for ts, reading in dated_readings
        if ts.date() == selected_date
    ]
    warning_events = [
        reading for _, reading in dated_readings
        if (reading.get("status") or "").lower() in {"warning", "fault"}
    ]
    selected_warning_events = [
        reading for reading in selected_readings
        if (reading.get("status") or "").lower() in {"warning", "fault"}
    ]

    node_alerts = await _request_node("GET", "/alerts")
    enriched_alerts = _enrich_node_alerts(node_alerts.get("alerts", []), limit=200)["alerts"]
    machine_alerts = [
        alert for alert in enriched_alerts
        if alert.get("machine_id") == machine_id
    ]

    machine_maintenance = [
        slot for slot in maintenance_store
        if slot.get("machine_id") == machine_id
    ]

    status_counts = {"running": 0, "warning": 0, "fault": 0}
    for reading in selected_readings:
        status = (reading.get("status") or "").lower()
        if status in status_counts:
            status_counts[status] += 1

    sensor_stats = _build_sensor_stats(selected_readings)
    warning_timestamps = [
        reading.get("timestamp")
        for reading in selected_warning_events
        if reading.get("timestamp")
    ]

    return {
        "machine_id": machine_id,
        "date": selected_date.isoformat(),
        "summary": {
            "total_readings": len(selected_readings),
            "running_count": status_counts["running"],
            "warning_count": status_counts["warning"],
            "fault_count": status_counts["fault"],
            "current_state": machine_state.get(machine_id, {}),
        },
        "sensor_stats": sensor_stats,
        "warnings_today": {
            "count": len(selected_warning_events),
            "timestamps": warning_timestamps[:20],
            "latest": warning_timestamps[-1] if warning_timestamps else None,
            "events": [_serialize_warning_event(item) for item in selected_warning_events[-10:]],
        },
        "warning_history": {
            "total_count": len(warning_events),
            "daily_counts": _warning_counts_by_day(warning_events),
            "recent_events": [_serialize_warning_event(item) for item in warning_events[-20:]],
        },
        "alerts": {
            "count": len(machine_alerts),
            "recent": machine_alerts[:10],
        },
        "maintenance": {
            "count": len(machine_maintenance),
            "recent": machine_maintenance[:10],
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

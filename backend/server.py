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
import random
import sys
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np
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

MACHINES = ["CNC_01", "CNC_02", "HVAC_01", "PUMP_01"]

# Simulated "wear" state per machine (increases over time to simulate drift)
_wear: Dict[str, float] = {m: 0.0 for m in MACHINES}
_rng = np.random.default_rng(int(time.time()))


# ── Normal operating ranges ───────────────────────────────────────────────────
MACHINE_NORMS = {
    "CNC_01":  {"temperature_C": (65, 90),  "vibration_mm_s": (0.6, 2.8), "rpm": (1250, 1580), "current_A": (10.5, 15.5)},
    "CNC_02":  {"temperature_C": (58, 88),  "vibration_mm_s": (0.5, 2.6), "rpm": (1150, 1530), "current_A": (9.5,  14.5)},
    "HVAC_01": {"temperature_C": (32, 52),  "vibration_mm_s": (0.2, 1.4), "rpm": (820,  1180), "current_A": (5.2,  9.8)},
    "PUMP_01": {"temperature_C": (42, 72),  "vibration_mm_s": (0.3, 1.9), "rpm": (970,  1380), "current_A": (7.2,  12.8)},
}

# Fault injection schedule: every ~120 s a random machine gets a fault burst
_next_fault_time: Dict[str, float] = {m: time.time() + random.randint(60, 180) for m in MACHINES}
_fault_active:    Dict[str, int]   = {m: 0 for m in MACHINES}   # fault remaining ticks


def _simulate_reading(machine_id: str) -> dict:
    """Generate a realistic (possibly anomalous) sensor reading."""
    norms = MACHINE_NORMS[machine_id]
    wear  = _wear[machine_id]

    def sample(lo, hi, noise_frac=0.08):
        mid  = (lo + hi) / 2
        half = (hi - lo) / 2
        return float(_rng.normal(mid + wear * 0.3, half * noise_frac * 6))

    t_C  = sample(*norms["temperature_C"])
    v_ms = sample(*norms["vibration_mm_s"])
    rpm  = sample(*norms["rpm"])
    curr = sample(*norms["current_A"])

    status = "running"
    now    = time.time()

    # Fault injection
    if now >= _next_fault_time[machine_id] and _fault_active[machine_id] == 0:
        _fault_active[machine_id] = random.randint(3, 8)           # fault lasts N ticks
        _next_fault_time[machine_id] = now + random.randint(90, 200)

    if _fault_active[machine_id] > 0:
        severity = 1.0 - (_fault_active[machine_id] / 8.0)          # ramps up
        t_C  += _rng.uniform(12, 22) * (0.5 + severity)
        v_ms += _rng.uniform(1.5, 4.0) * (0.5 + severity)
        curr += _rng.uniform(2.0, 5.0) * (0.5 + severity)
        status = "fault" if severity > 0.5 else "warning"
        _fault_active[machine_id] -= 1

    # Transient single-tick spike (noise, ~3% chance)
    elif random.random() < 0.03:
        t_C  += _rng.normal(0, 8)
        v_ms += _rng.normal(0, 0.8)
        status = "warning"

    # Gradual wear accumulation
    _wear[machine_id] += 0.0005

    return {
        "machine_id":     machine_id,
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "temperature_C":  round(t_C,  2),
        "vibration_mm_s": round(max(v_ms, 0.01), 3),
        "rpm":            round(max(rpm, 100.0), 1),
        "current_A":      round(max(curr, 0.5), 2),
        "status":         status,
    }


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


# ── SSE stream endpoint ───────────────────────────────────────────────────────
@app.get("/stream/{machine_id}")
async def stream(machine_id: str, request: Request):
    if machine_id not in MACHINES:
        raise HTTPException(status_code=404, detail=f"Unknown machine_id: {machine_id}")

    async def generator():
        consecutive_gaps = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                reading = _simulate_reading(machine_id)

                # Score if model available
                if scorer_registry:
                    scored = scorer_registry.score(machine_id, reading)
                else:
                    scored = {**reading, "risk_score": 0.0, "risk_level": "unknown",
                              "explanation": "Model not loaded."}

                machine_state[machine_id] = scored

                # Auto-raise alert if critical
                if scored.get("risk_level") == "critical" and not scored.get("suppressed"):
                    _raise_alert_internal(machine_id, scored)

                consecutive_gaps = 0
                yield f"data: {json.dumps(scored)}\n\n"
                await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_gaps += 1
                gap_event = {
                    "machine_id": machine_id,
                    "timestamp":  datetime.now(timezone.utc).isoformat(),
                    "error":      str(exc),
                    "signal":     "data_absent",             # absence is a signal
                    "consecutive_gaps": consecutive_gaps,
                }
                yield f"data: {json.dumps(gap_event)}\n\n"
                await asyncio.sleep(2.0)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ── History endpoint ──────────────────────────────────────────────────────────
@app.get("/history/{machine_id}")
async def get_history(machine_id: str, limit: int = 500):
    if machine_id not in MACHINES:
        raise HTTPException(status_code=404, detail=f"Unknown machine_id: {machine_id}")

    history_csv = os.path.join(os.path.dirname(__file__), "../data/history.csv")
    if not os.path.exists(history_csv):
        raise HTTPException(status_code=503, detail="Historical data not yet generated. Run train_model.py first.")

    import pandas as pd
    df = pd.read_csv(history_csv, parse_dates=["timestamp"])
    df = df[df["machine_id"] == machine_id].tail(limit)
    return {"machine_id": machine_id, "count": len(df), "data": df.to_dict(orient="records")}


# ── Alert endpoint ────────────────────────────────────────────────────────────
class AlertRequest(BaseModel):
    machine_id: str
    reason:     str
    risk_score: float = 0.5
    sensor_data: Optional[dict] = None


@app.post("/alert")
async def raise_alert(payload: AlertRequest):
    alert = {
        "alert_id":   str(uuid.uuid4())[:8],
        "machine_id": payload.machine_id,
        "reason":     payload.reason,
        "risk_score": payload.risk_score,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "sensor_data": payload.sensor_data or {},
        "acknowledged": False,
    }
    alerts_store.append(alert)
    alerts_store.sort(key=lambda a: a["risk_score"], reverse=True)
    return {"status": "alert_raised", "alert": alert}


def _raise_alert_internal(machine_id: str, scored: dict):
    """Auto-raise alert from agent loop (deduped within 30s per machine)."""
    now = time.time()
    for a in reversed(alerts_store[-20:]):
        if a["machine_id"] == machine_id:
            ts = datetime.fromisoformat(a["timestamp"]).timestamp()
            if now - ts < 30:
                return   # dedupe: already alerted this machine recently
    alert = {
        "alert_id":    str(uuid.uuid4())[:8],
        "machine_id":  machine_id,
        "reason":      scored.get("explanation", "Critical anomaly detected."),
        "risk_score":  scored.get("risk_score", 1.0),
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "sensor_data": {k: scored.get(k) for k in ["temperature_C","vibration_mm_s","rpm","current_A"]},
        "acknowledged": False,
        "auto": True,
    }
    alerts_store.append(alert)
    alerts_store.sort(key=lambda a: a["risk_score"], reverse=True)


# ── Schedule maintenance endpoint (Bonus) ────────────────────────────────────
class ScheduleRequest(BaseModel):
    machine_id:  str
    priority:    str = "normal"   # low / normal / high / critical
    alert_id:    Optional[str] = None
    requested_by: str = "agent"


@app.post("/schedule-maintenance")
async def schedule_maintenance(payload: ScheduleRequest):
    priority_offsets = {"critical": 2, "high": 8, "normal": 24, "low": 72}
    offset_hours = priority_offsets.get(payload.priority, 24)

    from datetime import timedelta
    slot_time = datetime.now(timezone.utc) + timedelta(hours=offset_hours)
    slot = {
        "booking_id":  str(uuid.uuid4())[:8],
        "machine_id":  payload.machine_id,
        "priority":    payload.priority,
        "scheduled_at": slot_time.isoformat(),
        "alert_id":    payload.alert_id,
        "requested_by": payload.requested_by,
        "status":      "confirmed",
    }
    return {"status": "scheduled", "slot": slot}


# ── Status / dashboard endpoints ──────────────────────────────────────────────
@app.get("/status")
async def status():
    return {
        "machines": MACHINES,
        "model_loaded": scorer_registry is not None,
        "active_alerts": len([a for a in alerts_store if not a["acknowledged"]]),
        "machine_states": machine_state,
    }


@app.get("/alerts")
async def get_alerts(limit: int = 50):
    return {
        "count": len(alerts_store),
        "alerts": alerts_store[:limit],
    }


@app.get("/dashboard-data")
async def dashboard_data():
    """Polling endpoint for the frontend dashboard."""
    return {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "machine_states": machine_state,
        "recent_alerts": alerts_store[:10],
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
# PredictaMaint — AI-Powered Predictive Maintenance Agent
## Hack Malenadu '26 | Industrial Intelligence Track

---

## Project Structure

```
predictive-maintenance/
├── ml/
│   ├── train_model.py      # ML training: Isolation Forest + dynamic baselines
│   └── scorer.py           # Inference engine (loaded by server + agent)
├── backend/
│   ├── server.py           # FastAPI: SSE streams, alerts, scheduling
│   └── agent.py            # Autonomous agent loop (multi-machine, prioritized)
├── frontend/
│   └── dashboard.html      # Live industrial dashboard (no framework needed)
├── data/                   # Auto-created on first run
│   ├── history.csv         # 7-day synthetic / real historical data
│   ├── baselines.json      # Per-machine dynamic baselines
│   └── models/             # Trained Isolation Forest models (.pkl)
├── requirements.txt
└── README.md
```

---

## Setup & Run

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Train the ML models (generates synthetic history if no CSV provided)
```bash
python ml/train_model.py
```
Output:
- `data/history.csv` (7-day synthetic data with wear drift + faults injected)
- `data/baselines.json` (per-machine mean/std/range per sensor)
- `data/models/CNC_01.pkl` etc. (Isolation Forest pipelines)

### 3. Start the FastAPI server
```bash
python backend/server.py
# Or: uvicorn backend.server:app --host 0.0.0.0 --port 8000 --reload
```

Kafka-backed live mode:
```bash
set PM_DATA_SOURCE=kafka
set KAFKA_BOOTSTRAP_SERVERS=localhost:9092
set KAFKA_TOPIC=machine.telemetry
python backend/server.py
```

Kafka message shape:
```json
{
  "machine_id": "CNC_01",
  "timestamp": "2026-04-20T10:15:00Z",
  "temperature_C": 84.2,
  "vibration_mm_s": 2.31,
  "rpm": 1485,
  "current_A": 13.8,
  "status": "running"
}
```

### 4. Start the autonomous agent loop (separate terminal)
```bash
python backend/agent.py
```

### 5. Open the dashboard
Open `frontend/dashboard.html` in your browser.
*(No web server needed — it connects directly to localhost:8000)*

---

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/stream/{machine_id}` | GET | SSE live readings (1/sec) |
| `/history/{machine_id}` | GET | Last 7 days JSON |
| `/alert` | POST | Raise alert with reason |
| `/schedule-maintenance` | POST | *(Bonus)* Auto-book slot |
| `/status` | GET | Health + all machine states |
| `/alerts` | GET | Alert priority queue |
| `/dashboard-data` | GET | Snapshot for polling |
| `PM_DATA_SOURCE=kafka` | env | Consume live telemetry from Kafka instead of simulator |

Machine IDs: `CNC_01`, `CNC_02`, `HVAC_01`, `PUMP_01`

---

## ML Architecture

### Per-Machine Isolation Forest
- Trained **only on `status=running` rows** (normal behaviour)
- `contamination` estimated from labelled fault ratio in history
- StandardScaler → IsolationForest (200 trees) pipeline
- Feature names stored on model object for safe inference

### Dynamic Baselines (per-machine, per-sensor)
- Computed from 7-day normal-only rows
- Stores: mean, std, min_normal (μ-3σ), max_normal (μ+3σ), p5, p95
- Falls back to domain-knowledge priors if < 100 normal samples

### Risk Scoring (0–1)
| Component | Weight | Logic |
|---|---|---|
| Isolation Forest | 40% | Normalised decision_function score |
| Max z-score | 40% | Max sensor deviation in σ units |
| Compound anomaly | 20% | ≥2 sensors simultaneously out-of-range |
| Drift bonus | +15% | Rolling-mean shift > 2σ over 20 readings |

### Edge Cases Handled
| Edge Case | Handling |
|---|---|
| Transient single spike | Suppressed if < 2 anomalies in last 5 readings |
| Compound multi-sensor | Risk boosted, labelled explicitly in explanation |
| Gradual drift / wear | 20-reading rolling mean vs baseline mean |
| Missing / NaN sensor | Imputed with machine baseline mean |
| Constant sensor (zero var) | Dropped before fitting, warned |
| Too few training samples | Falls back to prior ranges |
| Stream disconnect | Exponential back-off reconnect (max 30s) |
| Data absence | Flagged as explicit signal after 3 consecutive gaps |
| Multiple critical machines | Prioritized by risk_score (highest first) |
| Duplicate alerts | Deduplicated within 30s per machine |

---

## Submission Checklist (vs PDF)

| Expectation | Status | Notes |
|---|---|---|
| All 4 machines simultaneously | ✅ | Async SSE per machine |
| Spike detection | ✅ | z-score + IF |
| Drift detection | ✅ | Rolling mean z-score |
| Compound anomaly | ✅ | ≥2 sensors OOR |
| Risk score + plain-English explanation | ✅ | In every scored reading |
| Transient spike suppression | ✅ | 5-reading window |
| Live visual dashboard | ✅ | `dashboard.html` |
| Auto-scheduling (Bonus) | ✅ | POST /schedule-maintenance |
| Priority queue | ✅ | Alerts sorted by risk_score |

---

*Sponsor: Jnanik AI | 36 Hours | Team Size: 2–4*

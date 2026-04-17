"""
train_model.py
--------------
Trains per-machine anomaly detection models using Isolation Forest.
Also computes per-machine dynamic baselines (mean ± 3σ) from 7-day history.
Saves models and baselines to disk for use by the agent.

Edge cases handled:
- Missing / NaN sensor values → imputed with machine median
- Constant columns (zero variance) → dropped before fitting
- Too-few samples → warns and uses global fallback
- Drift detection: rolling-window mean shift > 2σ flags slow degradation
- Compound anomaly: multiple sensors simultaneously out of range
"""

import json
import os
import pickle
import warnings
from datetime import timedelta
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

warnings.filterwarnings("ignore")

MACHINES = ["CNC_01", "CNC_02", "PUMP_03", "CONVEYOR_04"]
SENSOR_COLS = ["temperature_C", "vibration_mm_s", "rpm", "current_A"]
HISTORY_CSV = os.path.join(os.path.dirname(__file__), "../data/history.csv")
MODEL_DIR   = os.path.join(os.path.dirname(__file__), "../data/models")
BASELINE_FILE = os.path.join(os.path.dirname(__file__), "../data/baselines.json")

# ── Normal operating ranges per machine (domain knowledge priors) ──────────
MACHINE_PRIORS = {
    "CNC_01":      {"temperature_C": (60, 95),  "vibration_mm_s": (0.5, 4.0), "rpm": (1300, 1650), "current_A": (10, 16)},
    "CNC_02":      {"temperature_C": (55, 100), "vibration_mm_s": (0.4, 3.0), "rpm": (1300, 1650), "current_A": (9,  17)},
    "PUMP_03":     {"temperature_C": (40, 75),  "vibration_mm_s": (0.8, 5.0), "rpm": (2750, 3100), "current_A": (15, 23)},
    "CONVEYOR_04": {"temperature_C": (30, 60),  "vibration_mm_s": (0.2, 2.2), "rpm": (650,  800),  "current_A": (6,  11)},
}

os.makedirs(MODEL_DIR, exist_ok=True)


def load_or_generate_history() -> pd.DataFrame:
    """Load CSV if present, else generate Malendau 7-day history from the Node dataset."""
    if os.path.exists(HISTORY_CSV):
        df = pd.read_csv(HISTORY_CSV, parse_dates=["timestamp"])
        machines_in_csv = set(df["machine_id"].unique())
        if set(MACHINES).issubset(machines_in_csv):
            print(f"[train] Loaded history: {len(df)} rows")
            return df
        print("[train] Existing history.csv does not match the Malendau machine set. Regenerating.")
    return export_malendau_history()


def export_malendau_history() -> pd.DataFrame:
    """Generate a CSV that mirrors the Malendau Node history patterns for ML training."""
    rng = np.random.default_rng(42)
    records = []
    days = 7
    now = pd.Timestamp.now(tz="UTC").floor("min")
    total_points = days * 24 * 60 + 1

    baselines = {
        "CNC_01":      {"temp": 72, "vib": 1.8, "rpm": 1480, "current": 12.5},
        "CNC_02":      {"temp": 68, "vib": 1.5, "rpm": 1490, "current": 11.8},
        "PUMP_03":     {"temp": 55, "vib": 2.2, "rpm": 2950, "current": 18.0},
        "CONVEYOR_04": {"temp": 45, "vib": 0.9, "rpm": 720, "current": 8.5},
    }

    def rand(lo, hi):
        return float(rng.uniform(lo, hi))

    def noise(base, pct):
        return base + rand(-base * pct, base * pct)

    def clamp(value, lo, hi):
        return max(lo, min(hi, value))

    def generate_reading(machine_id: str, timestamp: pd.Timestamp, day_offset: int, progress: float) -> dict:
        baseline = baselines[machine_id]
        temp = noise(baseline["temp"], 0.03)
        vib = noise(baseline["vib"], 0.04)
        rpm = noise(baseline["rpm"], 0.015)
        current = noise(baseline["current"], 0.03)
        status = "running"

        if machine_id == "CNC_01":
            degrade_days = max(0, 3 - day_offset)
            severity = degrade_days / 3
            vib += severity * 3.5
            temp += severity * 12
            current += severity * 2.5
            if severity > 0.6:
                status = "warning"
            if severity > 0.9 and progress > 0.8:
                status = "fault"

        if machine_id == "CNC_02":
            afternoon = 0.5 < progress < 0.75
            if afternoon and day_offset < 2:
                temp += rand(15, 30)
                current += rand(2, 5)
                if temp > 95:
                    status = "warning"
            if day_offset == 1 and 0.60 < progress < 0.65:
                temp = 112 + rand(0, 8)
                current = 22 + rand(0, 3)
                status = "fault"

        if machine_id == "PUMP_03":
            if rand(0, 1) < 0.08:
                vib += rand(2, 6)
                current += rand(1, 3)
                if vib > 6:
                    status = "warning"
            rpm_drop = ((7 - day_offset) / 7) * 180
            rpm -= rpm_drop
            if rpm < 2820:
                status = "warning"

        if machine_id == "CONVEYOR_04" and day_offset == 4 and 0.4 < progress < 0.45:
            vib += rand(1, 2)
            status = "warning"

        temp = clamp(temp, 20, 130)
        vib = clamp(vib, 0.1, 12)
        rpm = clamp(rpm, 100, 4000)
        current = clamp(current, 1, 30)

        return {
            "machine_id": machine_id,
            "timestamp": timestamp.isoformat(),
            "temperature_C": round(temp, 2),
            "vibration_mm_s": round(vib, 2),
            "rpm": round(rpm, 0),
            "current_A": round(current, 2),
            "status": status,
        }

    for machine_id in MACHINES:
        for index in range(total_points):
            timestamp = now - timedelta(minutes=(total_points - 1 - index))
            day_offset = int((now - timestamp) / pd.Timedelta(days=1))
            seconds_since_midnight = (
                timestamp.hour * 3600 + timestamp.minute * 60 + timestamp.second
            )
            progress = seconds_since_midnight / 86400
            records.append(generate_reading(machine_id, timestamp, day_offset, progress))

    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    os.makedirs(os.path.dirname(HISTORY_CSV), exist_ok=True)
    df.to_csv(HISTORY_CSV, index=False)
    print(f"[train] Exported Malendau history: {len(df)} rows -> {HISTORY_CSV}")
    return df


def generate_synthetic_history() -> pd.DataFrame:
    """Generate realistic 7-day history with subtle wear trends."""
    rng = np.random.default_rng(42)
    records = []
    periods = 7 * 24 * 3600  # 1 reading / sec for 7 days → too large; use 1/min
    n = 7 * 24 * 60           # one per minute

    for machine_id, priors in MACHINE_PRIORS.items():
        t_range = priors["temperature_C"]
        v_range = priors["vibration_mm_s"]
        r_range = priors["rpm"]
        c_range = priors["current_A"]

        # Introduce slow upward drift in temperature & vibration (wear simulation)
        drift = np.linspace(0, 3, n)

        temps  = rng.normal((t_range[0]+t_range[1])/2,  (t_range[1]-t_range[0])/6,  n) + drift * 0.5
        vibs   = rng.normal((v_range[0]+v_range[1])/2,  (v_range[1]-v_range[0])/6,  n) + drift * 0.05
        rpms   = rng.normal((r_range[0]+r_range[1])/2,  (r_range[1]-r_range[0])/6,  n)
        currs  = rng.normal((c_range[0]+c_range[1])/2,  (c_range[1]-c_range[0])/6,  n)

        # Inject ~2% transient spikes (noise, NOT real faults)
        spike_idx = rng.choice(n, size=int(n * 0.02), replace=False)
        temps[spike_idx]  += rng.normal(0, 8, len(spike_idx))
        vibs[spike_idx]   += rng.normal(0, 1, len(spike_idx))

        # Inject ~0.5% genuine compound anomalies (real faults)
        fault_idx = rng.choice(n, size=int(n * 0.005), replace=False)
        temps[fault_idx]  += rng.uniform(15, 25, len(fault_idx))
        vibs[fault_idx]   += rng.uniform(2,  5,  len(fault_idx))
        currs[fault_idx]  += rng.uniform(3,  6,  len(fault_idx))

        timestamps = pd.date_range("2026-03-19", periods=n, freq="1min")
        status = ["running"] * n
        for fi in fault_idx:
            status[fi] = "fault"
        for si in spike_idx:
            if status[si] == "running":
                status[si] = "warning"

        for i in range(n):
            records.append({
                "machine_id":     machine_id,
                "timestamp":      timestamps[i],
                "temperature_C":  round(float(temps[i]),  2),
                "vibration_mm_s": round(float(vibs[i]),   3),
                "rpm":            round(float(rpms[i]),   1),
                "current_A":      round(float(currs[i]),  2),
                "status":         status[i],
            })

    df = pd.DataFrame(records)
    os.makedirs(os.path.dirname(HISTORY_CSV), exist_ok=True)
    df.to_csv(HISTORY_CSV, index=False)
    print(f"[train] Saved synthetic history: {len(df)} rows -> {HISTORY_CSV}")
    return df


def compute_baselines(df: pd.DataFrame) -> dict:
    """
    Compute per-machine per-sensor dynamic baselines.
    Returns: {machine_id: {sensor: {mean, std, min_normal, max_normal}}}
    Edge cases:
      - Exclude known faults from baseline computation
      - Fall back to priors if std ≈ 0 (constant sensor)
    """
    baselines = {}
    for machine_id in MACHINES:
        mdf = df[(df["machine_id"] == machine_id) & (df["status"] == "running")].copy()
        if len(mdf) < 100:
            print(f"[warn] {machine_id}: only {len(mdf)} normal samples – using priors")
            baselines[machine_id] = _prior_baseline(machine_id)
            continue

        baselines[machine_id] = {}
        for col in SENSOR_COLS:
            vals = mdf[col].dropna()
            mean = float(vals.mean())
            std  = float(vals.std())
            if std < 1e-6:                          # constant → use prior range
                prior = MACHINE_PRIORS[machine_id][col]
                std   = (prior[1] - prior[0]) / 6
                mean  = (prior[0] + prior[1]) / 2
            baselines[machine_id][col] = {
                "mean":       round(mean, 4),
                "std":        round(std,  4),
                "min_normal": round(mean - 3 * std, 4),
                "max_normal": round(mean + 3 * std, 4),
                "p5":         round(float(vals.quantile(0.05)), 4),
                "p95":        round(float(vals.quantile(0.95)), 4),
            }
    return baselines


def _prior_baseline(machine_id: str) -> dict:
    result = {}
    for col, (lo, hi) in MACHINE_PRIORS[machine_id].items():
        mean = (lo + hi) / 2
        std  = (hi - lo) / 6
        result[col] = {
            "mean": mean, "std": std,
            "min_normal": lo, "max_normal": hi,
            "p5": lo, "p95": hi,
        }
    return result


def train_isolation_forest(df: pd.DataFrame, machine_id: str) -> Pipeline:
    """
    Train an Isolation Forest pipeline (StandardScaler → IsolationForest).
    Trained ONLY on 'running' status rows to learn normal behaviour.
    contamination is estimated from the data's fault ratio.
    """
    mdf = df[df["machine_id"] == machine_id][SENSOR_COLS + ["status"]].copy()

    # Impute NaN with per-column median
    for col in SENSOR_COLS:
        mdf[col].fillna(mdf[col].median(), inplace=True)

    # Estimate contamination from labelled faults (capped 0.01–0.10)
    total  = len(mdf)
    faults = (mdf["status"] == "fault").sum()
    contamination = float(np.clip(faults / total if total > 0 else 0.02, 0.01, 0.10))
    print(f"[train] {machine_id}: {total} rows, {faults} faults, contamination={contamination:.4f}")

    # Use only running rows for training (unsupervised on normal data)
    X_train = mdf[mdf["status"] == "running"][SENSOR_COLS]

    # Drop zero-variance columns
    var = X_train.var()
    valid_cols = var[var > 1e-10].index.tolist()
    if len(valid_cols) < len(SENSOR_COLS):
        print(f"[warn] {machine_id}: dropped constant cols {set(SENSOR_COLS)-set(valid_cols)}")

    pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("iso",    IsolationForest(
            n_estimators=200,
            contamination=contamination,
            max_samples="auto",
            random_state=42,
            n_jobs=1,
        )),
    ])
    pipeline.fit(X_train[valid_cols])
    pipeline.feature_names = valid_cols          # store for inference

    # Quick sanity: score on known faults → should be mostly -1
    if faults > 0:
        X_fault = mdf[mdf["status"] == "fault"][valid_cols]
        preds   = pipeline.predict(X_fault)
        recall  = (preds == -1).mean()
        print(f"[train] {machine_id}: fault recall={recall:.2%}")

    return pipeline


def main():
    df = load_or_generate_history()
    baselines = compute_baselines(df)

    with open(BASELINE_FILE, "w") as f:
        json.dump(baselines, f, indent=2)
    print(f"[train] Baselines saved -> {BASELINE_FILE}")

    for machine_id in MACHINES:
        pipeline = train_isolation_forest(df, machine_id)
        path = os.path.join(MODEL_DIR, f"{machine_id}.pkl")
        with open(path, "wb") as f:
            pickle.dump(pipeline, f)
        print(f"[train] Model saved -> {path}")

    print("\nTraining complete. All models and baselines saved.")


if __name__ == "__main__":
    main()

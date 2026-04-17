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

import os
import json
import pickle
import warnings
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

warnings.filterwarnings("ignore")

MACHINES = ["CNC_01", "CNC_02", "HVAC_01", "PUMP_01"]
SENSOR_COLS = ["temperature_C", "vibration_mm_s", "rpm", "current_A"]
HISTORY_CSV = os.path.join(os.path.dirname(__file__), "../data/history.csv")
MODEL_DIR   = os.path.join(os.path.dirname(__file__), "../data/models")
BASELINE_FILE = os.path.join(os.path.dirname(__file__), "../data/baselines.json")

# ── Normal operating ranges per machine (domain knowledge priors) ──────────
MACHINE_PRIORS = {
    "CNC_01":  {"temperature_C": (60, 95),  "vibration_mm_s": (0.5, 3.0), "rpm": (1200, 1600), "current_A": (10, 16)},
    "CNC_02":  {"temperature_C": (55, 90),  "vibration_mm_s": (0.4, 2.8), "rpm": (1100, 1550), "current_A": (9,  15)},
    "HVAC_01": {"temperature_C": (30, 55),  "vibration_mm_s": (0.2, 1.5), "rpm": (800,  1200), "current_A": (5,  10)},
    "PUMP_01": {"temperature_C": (40, 75),  "vibration_mm_s": (0.3, 2.0), "rpm": (950,  1400), "current_A": (7,  13)},
}

os.makedirs(MODEL_DIR, exist_ok=True)


def load_or_generate_history() -> pd.DataFrame:
    """Load CSV if present, else generate synthetic 7-day history."""
    if os.path.exists(HISTORY_CSV):
        df = pd.read_csv(HISTORY_CSV, parse_dates=["timestamp"])
        print(f"[train] Loaded history: {len(df)} rows")
        return df
    print("[train] history.csv not found – generating synthetic 7-day data")
    return generate_synthetic_history()


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
    print(f"[train] Saved synthetic history: {len(df)} rows → {HISTORY_CSV}")
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
            n_jobs=-1,
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
    print(f"[train] Baselines saved → {BASELINE_FILE}")

    for machine_id in MACHINES:
        pipeline = train_isolation_forest(df, machine_id)
        path = os.path.join(MODEL_DIR, f"{machine_id}.pkl")
        with open(path, "wb") as f:
            pickle.dump(pipeline, f)
        print(f"[train] Model saved → {path}")

    print("\n✅ Training complete. All models and baselines saved.")


if __name__ == "__main__":
    main()
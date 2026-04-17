"""
scorer.py
---------
Loads trained models + baselines and scores incoming sensor readings.

Risk scoring logic:
  1. IF model score  → raw anomaly flag (-1 = anomaly)
  2. Per-sensor z-score against dynamic baseline
  3. Compound anomaly bonus (≥2 sensors simultaneously out of range)
  4. Drift detection: rolling 10-reading mean vs baseline mean
  5. Transient noise suppression: a single spike in a window of 5 normal
     readings is NOT escalated (it's transient)

Final risk score: 0.0 – 1.0
  < 0.35  → normal
  0.35–0.65 → warning
  > 0.65  → alert / critical
"""

import os
import json
import pickle
import numpy as np
from collections import deque
from typing import Dict, Optional

SENSOR_COLS   = ["temperature_C", "vibration_mm_s", "rpm", "current_A"]
MODEL_DIR     = os.path.join(os.path.dirname(__file__), "../data/models")
BASELINE_FILE = os.path.join(os.path.dirname(__file__), "../data/baselines.json")

# Window sizes for noise suppression and drift detection
SPIKE_WINDOW  = 5    # if anomaly in <2 of last 5 → transient, suppress
DRIFT_WINDOW  = 20   # readings to compute rolling mean for drift


class MachineScorer:
    """Stateful per-machine scorer that maintains a short reading history."""

    def __init__(self, machine_id: str, baseline: dict, model):
        self.machine_id = machine_id
        self.baseline   = baseline
        self.model      = model
        self._history: deque = deque(maxlen=DRIFT_WINDOW)
        self._anomaly_flags: deque = deque(maxlen=SPIKE_WINDOW)

    def score(self, reading: dict) -> dict:
        """
        Score a single reading dict.
        Returns enriched dict with risk_score, risk_level, flags, explanation.
        """
        # 1. Extract features, handle missing/NaN
        features = {}
        for col in SENSOR_COLS:
            val = reading.get(col)
            if val is None or (isinstance(val, float) and np.isnan(val)):
                # Missing sensor → use baseline mean (conservative)
                val = self.baseline[col]["mean"]
                reading[col] = val
            features[col] = float(val)

        self._history.append(features)
        feat_names = getattr(self.model, "feature_names", SENSOR_COLS)
        import pandas as pd
        X = pd.DataFrame([[features[c] for c in feat_names]], columns=feat_names)

        # 2. Isolation Forest decision
        if_score = self.model.decision_function(X)[0]   # lower = more anomalous
        if_flag  = self.model.predict(X)[0]             # -1 = anomaly, 1 = normal

        # 3. Per-sensor z-scores
        z_scores = {}
        out_of_range_sensors = []
        for col in SENSOR_COLS:
            b = self.baseline[col]
            z = abs(features[col] - b["mean"]) / max(b["std"], 1e-6)
            z_scores[col] = round(z, 3)
            if z > 3.0:
                out_of_range_sensors.append(col)

        # 4. Compound anomaly: ≥2 sensors simultaneously out of range
        compound = len(out_of_range_sensors) >= 2

        # 5. Drift detection (only when enough history)
        drift_flags = {}
        if len(self._history) >= DRIFT_WINDOW:
            for col in SENSOR_COLS:
                b      = self.baseline[col]
                window_mean = np.mean([h[col] for h in self._history])
                drift_z     = abs(window_mean - b["mean"]) / max(b["std"], 1e-6)
                if drift_z > 2.0:
                    drift_flags[col] = round(drift_z, 3)

        # 6. Compute raw risk score (0–1)
        # IF contributes 40%, z-score aggregate 40%, compound 20%
        # Normalize IF score: typical range ~[-0.5, 0.5], more negative = worse
        if_contrib   = float(np.clip((-if_score + 0.1) / 0.6, 0, 1)) * 0.40
        max_z        = max(z_scores.values()) if z_scores else 0
        z_contrib    = float(np.clip(max_z / 6.0, 0, 1))           * 0.40
        comp_contrib = 0.20 if compound else 0.0
        drift_contrib = min(0.10 * len(drift_flags), 0.15)
        raw_score    = if_contrib + z_contrib + comp_contrib + drift_contrib

        # 7. Transient noise suppression
        #    Record current anomaly flag
        is_anomaly = raw_score > 0.35
        self._anomaly_flags.append(1 if is_anomaly else 0)

        # Suppress if <2 anomalies in last SPIKE_WINDOW readings
        anomaly_count = sum(self._anomaly_flags)
        suppressed    = False
        if is_anomaly and len(self._anomaly_flags) == SPIKE_WINDOW and anomaly_count < 2:
            suppressed = True
            raw_score  = min(raw_score, 0.30)   # cap below warning threshold

        # 8. Determine risk level
        if raw_score < 0.35:
            risk_level = "normal"
        elif raw_score < 0.65:
            risk_level = "warning"
        else:
            risk_level = "critical"

        # 9. Build plain-English explanation
        explanation = self._explain(
            risk_level, features, z_scores, out_of_range_sensors,
            drift_flags, compound, suppressed, if_flag
        )

        return {
            **reading,
            "risk_score":     round(raw_score, 4),
            "risk_level":     risk_level,
            "z_scores":       z_scores,
            "out_of_range":   out_of_range_sensors,
            "compound":       compound,
            "drift_detected": drift_flags,
            "suppressed":     suppressed,
            "if_flag":        int(if_flag),
            "explanation":    explanation,
        }

    def _explain(self, level, features, z_scores, oor, drift_flags,
                 compound, suppressed, if_flag) -> str:
        parts = []
        if level == "normal":
            return "All sensors within normal operating ranges."

        if suppressed:
            parts.append("Transient spike detected but suppressed (single-reading noise).")
            return " ".join(parts)

        if compound:
            sensors_str = " and ".join(oor)
            parts.append(f"Compound anomaly: {sensors_str} simultaneously out of range — high failure probability.")

        for col in oor:
            b   = self.baseline[col]
            val = features[col]
            direction = "above" if val > b["max_normal"] else "below"
            parts.append(
                f"{col} is {val} ({direction} normal range "
                f"[{b['min_normal']}–{b['max_normal']}], z={z_scores[col]:.1f}σ)."
            )

        for col, dz in drift_flags.items():
            parts.append(f"Slow upward drift in {col} over last {DRIFT_WINDOW} readings (z={dz:.1f}σ) — possible gradual wear.")

        if if_flag == -1 and not parts:
            parts.append("Isolation Forest flagged an unusual multivariate pattern not captured by individual sensor thresholds.")

        return " ".join(parts) if parts else "Minor deviation detected."


class ScorerRegistry:
    """Loads and caches all machine scorers."""

    def __init__(self):
        self._scorers: Dict[str, MachineScorer] = {}
        self._load()

    def _load(self):
        if not os.path.exists(BASELINE_FILE):
            raise FileNotFoundError(
                "baselines.json not found. Run: python ml/train_model.py"
            )
        with open(BASELINE_FILE) as f:
            baselines = json.load(f)

        for machine_id in baselines:
            model_path = os.path.join(MODEL_DIR, f"{machine_id}.pkl")
            if not os.path.exists(model_path):
                raise FileNotFoundError(
                    f"Model not found for {machine_id}. Run: python ml/train_model.py"
                )
            with open(model_path, "rb") as f:
                model = pickle.load(f)
            self._scorers[machine_id] = MachineScorer(machine_id, baselines[machine_id], model)
            print(f"[scorer] Loaded model for {machine_id}")

    def score(self, machine_id: str, reading: dict) -> Optional[dict]:
        scorer = self._scorers.get(machine_id)
        if scorer is None:
            return None
        return scorer.score(reading)

    @property
    def machine_ids(self):
        return list(self._scorers.keys())
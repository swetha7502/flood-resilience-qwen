"""
train_model.py — trains the edge agent's local ML fallback model.

DEV-TIME ONLY. Requires scikit-learn (`pip install scikit-learn`), which is
NOT a runtime dependency of the deployed edge agent -- real edge/IoT
hardware often can't afford an sklearn install. This script runs once (or
whenever you want to retrain), and exports the trained model as a small,
plain-JSON file of coefficients. inference.py then does the actual
prediction with nothing but the Python standard library, so the thing that
actually runs on the edge device has zero ML dependencies.

WHAT THIS MODEL IS AND ISN'T:
  There is no real historical flood dataset for this project -- it's a
  hackathon simulation. So this is NOT "trained on real flood outcomes."
  It's trained on synthetic sensor samples drawn from the same
  scenario/threshold definitions already in config/scenarios.json, labeled
  using the same deterministic weighted-rule logic edge_agent.py's
  _local_weighted_decision already implements (see _score_to_risk there).
  In other words: this is a DISTILLATION of the hand-crafted rule into a
  learned model, not an independent source of truth.

  Why bother, if it's trained on the same rule it's replacing?
    - The hand-crafted rule has hard threshold cutoffs (a score of 0.449
      is WATCH, 0.450 is WARNING); a learned model gives a smoother,
      continuous decision surface between scenarios instead of a cliff edge.
    - Real-valued softmax probabilities are a more principled confidence
      score than the current "confidence = weighted_score * 0.85" formula.
    - Most importantly: the audit log (history_store.py) now durably
      records every real decision Qwen ever makes. Once a deployment has
      accumulated enough real field history, THAT becomes the training set
      instead of synthetic distillation -- this script and inference.py
      don't change, only the data source does. That's the actual point:
      this sets up the pipeline for the model to get better than the rule
      it started from, once real data exists.

Run with: python3 train_model.py
"""

import json
import os
import random
import sys

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, confusion_matrix

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config.loader import THRESHOLDS, SIGNAL_WEIGHTS, SCENARIOS_CONFIG  # noqa: E402

FEATURE_ORDER = ["rainfall", "river_level", "soil_saturation", "drain_flow"]
CLASSES = ["WATCH", "WARNING", "EMERGENCY"]
SAMPLES_PER_SCENARIO = 1500
DRAIN_FLIP_PROB = 0.15  # occasionally flip drain_flow independent of scenario,
                          # so the model also sees "isolated drain backup
                          # during otherwise calm weather" -- a real scenario
                          # the 4 canned demo scenarios never represent on
                          # their own, since drain_flow there always just
                          # mirrors overall storm severity.

random.seed(42)
np.random.seed(42)


def normalize(sensor: str, value: float) -> float:
    """Same normalization edge_agent.py's _local_weighted_decision uses:
    value relative to that sensor's emergency threshold. Deliberately NOT
    capped at 1.0 here (unlike the hand-written rule) -- letting it float
    above 1.0 gives the model information about "how far past emergency"
    a reading is, which the capped version throws away."""
    emergency = THRESHOLDS[sensor]["emergency"]
    return value / emergency


def score_to_label(weighted_score: float) -> str:
    """Mirrors edge_agent.py's _score_to_risk cutoffs exactly -- this is
    the "teacher" the model is distilling."""
    if weighted_score >= 0.70:
        return "EMERGENCY"
    elif weighted_score >= 0.45:
        return "WARNING"
    return "WATCH"


def generate_samples():
    X, y = [], []
    scenarios = {k: v for k, v in SCENARIOS_CONFIG.items() if k not in ("thresholds", "zones")}
    for scenario_name, scenario_cfg in scenarios.items():
        if not isinstance(scenario_cfg, dict) or "rainfall" not in scenario_cfg:
            continue  # skip _notes and other non-scenario keys
        for _ in range(SAMPLES_PER_SCENARIO):
            readings = {}
            for sensor in FEATURE_ORDER:
                cfg = scenario_cfg[sensor]
                val = random.gauss(cfg["base"], max(cfg["variance"], 1e-6))
                if sensor == "drain_flow":
                    val = 1.0 if val >= 0.5 else 0.0
                    if random.random() < DRAIN_FLIP_PROB:
                        val = 1.0 - val  # isolated flip, independent of scenario
                readings[sensor] = max(val, 0.0)

            features = [normalize(s, readings[s]) for s in FEATURE_ORDER]
            weighted_score = sum(
                min(normalize(s, readings[s]), 1.0) * SIGNAL_WEIGHTS.get(s, 0.0)
                for s in FEATURE_ORDER
            )
            label = score_to_label(weighted_score)
            X.append(features)
            y.append(label)
    return np.array(X), np.array(y)


def main():
    print("Generating synthetic training samples from config/scenarios.json ...")
    X, y = generate_samples()
    print(f"  {len(X)} samples across classes: "
          f"{dict(zip(*np.unique(y, return_counts=True)))}")

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    model = LogisticRegression(max_iter=2000)
    model.fit(X_train, y_train)

    print("\n--- Held-out evaluation ---")
    y_pred = model.predict(X_test)
    print(classification_report(y_test, y_pred))
    print("Confusion matrix (rows=true, cols=pred), classes:", list(model.classes_))
    print(confusion_matrix(y_test, y_pred, labels=model.classes_))

    print("\n--- Learned feature weights vs. hand-designed SIGNAL_WEIGHTS ---")
    print("(sanity check: does the model roughly agree with domain intuition?)")
    for i, cls in enumerate(model.classes_):
        print(f"  {cls}: " + ", ".join(
            f"{feat}={model.coef_[i][j]:+.3f}" for j, feat in enumerate(FEATURE_ORDER)
        ))
    print(f"  (hand-designed weights for comparison: {SIGNAL_WEIGHTS})")

    out = {
        "feature_order": FEATURE_ORDER,
        "classes": list(model.classes_),
        "coef": model.coef_.tolist(),
        "intercept": model.intercept_.tolist(),
        "normalization": "value / THRESHOLDS[sensor]['emergency'], uncapped",
        "trained_on": "synthetic samples distilled from the hand-crafted weighted-rule "
                      "in edge_agent.py, NOT real flood history (none exists for this "
                      "project) -- see this file's module docstring.",
    }
    out_path = os.path.join(os.path.dirname(__file__), "model_weights.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()

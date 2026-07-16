"""
inference.py — runtime inference for the edge agent's local ML fallback.

Deliberately stdlib-only (no numpy, no sklearn) -- train_model.py needs
those, but they're dev-time tools, not something we should require the
deployed edge device to install. This module just loads a small JSON of
already-trained coefficients and does the matrix multiply + softmax by
hand with plain Python math.

If model_weights.json is missing or fails to load (e.g. someone runs the
edge agent without ever running train_model.py), predict() raises
ModelUnavailable -- edge_agent.py catches this and falls back to the
original hand-written weighted-rule logic. This is deliberate: even the ML
fallback has its own fallback, which is very on-theme for a project whose
entire premise is graceful degradation at every layer.
"""

import json
import math
import os

_WEIGHTS_PATH = os.path.join(os.path.dirname(__file__), "model_weights.json")

_model = None  # lazy-loaded singleton


class ModelUnavailable(Exception):
    pass


def _load_model() -> dict:
    global _model
    if _model is not None:
        return _model
    try:
        with open(_WEIGHTS_PATH) as f:
            _model = json.load(f)
        return _model
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise ModelUnavailable(
            f"model_weights.json not found or invalid ({e}). "
            f"Run edge_agent/ml_fallback/train_model.py first."
        )


def predict(readings: dict, thresholds: dict) -> tuple[str, float, dict]:
    """
    readings: {"rainfall": 55.0, "river_level": 0.8, ...}
    thresholds: THRESHOLDS dict from config/loader.py (needed for the same
                normalization used at training time -- value / emergency).

    Returns (risk_level, confidence, class_probabilities).
    Raises ModelUnavailable if the model file can't be loaded -- caller
    is expected to fall back to the weighted-rule logic in that case.
    """
    model = _load_model()
    feature_order = model["feature_order"]

    features = []
    for sensor in feature_order:
        emergency = thresholds.get(sensor, {}).get("emergency")
        value = readings.get(sensor, 0.0)
        if not emergency:
            features.append(0.0)
        else:
            features.append(value / emergency)

    logits = []
    for coef_row, intercept in zip(model["coef"], model["intercept"]):
        logit = intercept + sum(c * f for c, f in zip(coef_row, features))
        logits.append(logit)

    # Softmax, numerically stabilized by subtracting the max logit.
    max_logit = max(logits)
    exps = [math.exp(l - max_logit) for l in logits]
    total = sum(exps)
    probs = [e / total for e in exps]

    classes = model["classes"]
    class_probs = dict(zip(classes, probs))
    best_idx = probs.index(max(probs))
    risk_level = classes[best_idx]
    confidence = probs[best_idx]

    return risk_level, confidence, class_probs

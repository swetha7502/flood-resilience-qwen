"""
config/loader.py — single source of truth for sensor thresholds, zone
layout, and edge-local signal weights.

Why this exists:
  Previously THRESHOLDS was hand-copied into both edge_agent.py and
  coordinator.py, and a THIRD, different copy lived in scenarios.json
  (used by sensor_agent.py to decide reading.flagged). The three copies
  had drifted out of sync -- most seriously, river_level and drain_flow
  had thresholds edge_agent/coordinator could never actually reach given
  the ranges sensor_agent simulates, meaning those two sensors could
  never be counted as "flagged" by the cloud/edge decision logic even
  during a flash_flood scenario. That silently broke Zone B, which
  depends on river_level + drain_flow as 2 of its 3 signals.

  This module loads config/scenarios.json once and exposes the derived
  constants so every component (sensor simulation, edge agent, cloud
  coordinator) reasons about the exact same numbers.
"""

import json
import os

# FLOODGUARD_CONFIG_PATH lets a different deployment point at its own
# scenarios.json (different zones, thresholds, reference locations) without
# any code changes -- e.g. running this same codebase for a second city just
# means shipping a second config file and setting one env var, not forking
# the repo. Every component that needs zone/threshold/location data goes
# through this one module, so there is exactly one place this can be
# reconfigured from.
_CONFIG_PATH = os.getenv(
    "FLOODGUARD_CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "scenarios.json"),
)

with open(_CONFIG_PATH) as _f:
    SCENARIOS_CONFIG = json.load(_f)

# {"rainfall": {"watch":..., "warning":..., "emergency":...}, ...}
THRESHOLDS: dict = SCENARIOS_CONFIG["thresholds"]

# {"A": {"sensors": [...], "description": "...", "reference_location": {...}}, ...}
ZONES_CONFIG: dict = SCENARIOS_CONFIG["zones"]
ZONES: list = list(ZONES_CONFIG.keys())
SENSOR_TYPES: list = ["rainfall", "river_level", "soil_saturation", "drain_flow"]

# {"A": {"lat":..., "lon":..., "label": "..."}, ...} -- used by the
# coordinator's get_regional_weather tool so Qwen can cross-check simulated
# readings against real current conditions at each zone's real-world analog.
# Falls back to an empty dict per zone if a config omits reference_location,
# so older/minimal configs (or a deployment that doesn't want this feature)
# don't break -- the weather tool just reports itself unavailable for that zone.
ZONE_LOCATIONS: dict = {
    zone: cfg.get("reference_location", {})
    for zone, cfg in ZONES_CONFIG.items()
}

# Edge-local fusion weights used ONLY by edge_agent's weighted-rules fallback
# (State 2 / OFFLINE) and the cache-replay similarity score (State 1 /
# DEGRADED). These are NOT sent to Qwen -- Qwen reasons over raw readings
# and history directly. The weights encode which signal is most
# predictive of imminent flooding for THIS neighborhood layout, based on
# the zone design in scenarios.json:
#   - river_level (0.35): the most direct flood signal -- Zone B (highest
#     historical risk) is defined primarily by this sensor.
#   - rainfall (0.30): strong leading indicator, but has to travel through
#     soil/drainage before it becomes an actual flood, so weighted just
#     under river_level.
#   - soil_saturation (0.25): present in all 3 zones; matters most as a
#     modifier on top of rainfall/river_level (saturated ground can't
#     absorb further rain), so weighted lower than the two "output"
#     signals it modifies.
#   - drain_flow (0.10): binary backup indicator (0/1, see below) --
#     lowest weight because it's a lagging/confirming signal, not a
#     predictive one, but still contributes to the local weighted score
#     and to cache-replay similarity.
SIGNAL_WEIGHTS: dict = {
    "river_level": 0.35,
    "rainfall": 0.30,
    "soil_saturation": 0.25,
    "drain_flow": 0.10,
}

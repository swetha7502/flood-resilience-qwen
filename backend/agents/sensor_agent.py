"""
Base class for all simulated edge sensor agents.

Each sensor agent:
  - Runs as an independent asyncio task (mirrors how a real edge device
    would run its own loop, independent of other sensors).
  - Reads its target behavior from the active scenario config.
  - Publishes a reading to its own Redis pub/sub channel every INTERVAL_SECONDS.
  - Tracks its own "flagged" state (whether it's currently above a threshold),
    which the edge agent uses as raw input -- it does NOT decide anything
    itself.

All decisioning (including graceful degradation when the cloud is
unreachable) lives in edge_agent.py, not here. An earlier version of this
file had its own local_fallback_rule() that independently published
warning-level decisions per-sensor to the same action:{zone} channel
edge_agent.py owns -- with a different message shape (nested "payload",
lowercase risk strings) than edge_agent's flat/uppercase format, and driven
by a totally separate control path (Redis demo:control) than the one that
actually reaches edge_agent (real HTTP failures against the coordinator).
The two systems could fire independently and inconsistently, and the
frontend would receive two incompatible shapes on one channel. It's removed
here because edge_agent.py's 4-state machine is strictly a superset of what
it did: multi-signal fusion instead of single-sensor, escalating
conservatism instead of one fixed "warning", and it's driven by the link's
actual health rather than a manually toggled flag.
"""

import asyncio
import json
import random
import time
from dataclasses import dataclass, asdict

INTERVAL_SECONDS = 2


@dataclass
class SensorReading:
    sensor: str
    zone: str
    value: float
    unit: str
    flagged: bool
    timestamp: float

    def to_json(self) -> str:
        return json.dumps(asdict(self))


class SensorAgent:
    def __init__(self, sensor_type: str, zone: str, redis_client, scenarios: dict):
        self.sensor_type = sensor_type
        self.zone = zone
        self.redis = redis_client
        self.scenarios = scenarios
        self.channel = f"sensor:{zone}:{sensor_type}"
        self.current_scenario = "normal"

    def set_scenario(self, scenario_name: str):
        """Called externally (by a demo control script) to change conditions.
        Validates against actual scenario blocks specifically (not just any
        key in the config file) -- scenarios.json also has "thresholds",
        "zones", and note keys at the same top level, and accepting one of
        those as a "scenario" would crash this agent's loop the next time
        it tries to read cfg["base"]/cfg["variance"] from a dict that
        doesn't have them."""
        non_scenario_keys = {"thresholds", "zones"} | {
            k for k in self.scenarios if k.startswith("_")
        }
        valid_scenarios = set(self.scenarios) - non_scenario_keys
        if scenario_name not in valid_scenarios:
            raise ValueError(f"Unknown scenario: {scenario_name!r} (valid: {sorted(valid_scenarios)})")
        self.current_scenario = scenario_name

    def _generate_value(self) -> float:
        cfg = self.scenarios[self.current_scenario][self.sensor_type]
        return round(cfg["base"] + random.uniform(-cfg["variance"], cfg["variance"]), 2)

    def _is_flagged(self, value: float) -> bool:
        threshold = self.scenarios["thresholds"][self.sensor_type]["watch"]
        return value >= threshold

    def _unit(self) -> str:
        return self.scenarios[self.current_scenario][self.sensor_type]["unit"]

    async def run(self):
        """Main loop -- runs forever, mirrors a real device's sense loop."""
        while True:
            value = self._generate_value()
            reading = SensorReading(
                sensor=self.sensor_type,
                zone=self.zone,
                value=value,
                unit=self._unit(),
                flagged=self._is_flagged(value),
                timestamp=time.time(),
            )
            await self.redis.publish(self.channel, reading.to_json())
            await asyncio.sleep(INTERVAL_SECONDS)


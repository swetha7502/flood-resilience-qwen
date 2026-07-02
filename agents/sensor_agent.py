"""
Base class for all simulated edge sensor agents.

Each sensor agent:
  - Runs as an independent asyncio task (mirrors how a real edge device
    would run its own loop, independent of other sensors).
  - Reads its target behavior from the active scenario config.
  - Publishes a reading to its own Redis pub/sub channel every INTERVAL_SECONDS.
  - Tracks its own "flagged" state (whether it's currently above a threshold).
  - Has a local_fallback_rule() that runs ONLY when the agent has been told
    the cloud is unreachable. This is what proves graceful degradation:
    the agent does not need the coordinator or Qwen to make a safe decision.
"""

import asyncio
import json
import random
import time
from dataclasses import dataclass, asdict
from typing import Optional

INTERVAL_SECONDS = 2
LOCAL_ESCALATION_HOLD_SECONDS = 60  # how long a flagged reading must persist
                                      # in degraded mode before self-escalating


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
        self.cloud_available = True

        # Tracks how long this sensor has been continuously flagged,
        # used only for the local fallback escalation timer.
        self._flagged_since: Optional[float] = None

    def set_scenario(self, scenario_name: str):
        """Called externally (by a demo control script) to change conditions."""
        if scenario_name not in self.scenarios:
            raise ValueError(f"Unknown scenario: {scenario_name}")
        self.current_scenario = scenario_name

    def set_cloud_available(self, available: bool):
        """Called externally to simulate network degradation."""
        self.cloud_available = available
        if available:
            self._flagged_since = None  # reset escalation timer on recovery

    def _generate_value(self) -> float:
        cfg = self.scenarios[self.current_scenario][self.sensor_type]
        return round(cfg["base"] + random.uniform(-cfg["variance"], cfg["variance"]), 2)

    def _is_flagged(self, value: float) -> bool:
        threshold = self.scenarios["thresholds"][self.sensor_type]["watch"]
        return value >= threshold

    def _unit(self) -> str:
        return self.scenarios[self.current_scenario][self.sensor_type]["unit"]

    async def local_fallback_rule(self, reading: SensorReading):
        """
        Runs only when cloud_available is False.
        This is the graceful degradation logic: if a sensor stays flagged
        for longer than LOCAL_ESCALATION_HOLD_SECONDS with no cloud reasoning
        available, it escalates on its own, conservatively, without waiting
        for Qwen. This must be loud and visible in logs/UI -- it is the
        proof point for the EdgeAgent track's degradation requirement.
        """
        now = time.time()

        if reading.flagged:
            if self._flagged_since is None:
                self._flagged_since = now
            elapsed = now - self._flagged_since
            if elapsed >= LOCAL_ESCALATION_HOLD_SECONDS:
                await self.redis.publish(
                    f"action:{self.zone}",
                    json.dumps({
                        "type": "risk_decision",
                        "zone": self.zone,
                        "timestamp": now,
                        "payload": {
                            "risk_level": "warning",
                            "reasoning": (
                                f"Local rule: {self.sensor_type} has stayed above "
                                f"the watch threshold for {int(elapsed)}s while cloud "
                                f"reasoning is unavailable. Escalating conservatively "
                                f"without Qwen fusion."
                            ),
                            "confidence": 0.5,
                            "source": "local_rule",
                        },
                    }),
                )
        else:
            self._flagged_since = None

    async def run(self):
        """Main loop -- runs forever, mirrors a real device's sense loop."""
        while True:
            value = self._generate_value()
            flagged = self._is_flagged(value)
            reading = SensorReading(
                sensor=self.sensor_type,
                zone=self.zone,
                value=value,
                unit=self._unit(),
                flagged=flagged,
                timestamp=time.time(),
            )

            await self.redis.publish(self.channel, reading.to_json())

            if not self.cloud_available:
                await self.local_fallback_rule(reading)

            await asyncio.sleep(INTERVAL_SECONDS)

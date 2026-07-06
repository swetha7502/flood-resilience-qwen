"""
edge_agent.py — Edge Agent for FloodGuard AI
=============================================
Sits between sensors (Redis pub/sub) and the cloud coordinator (HTTP).
Owns all local intelligence: pre-filtering, action execution, and a
4-state graceful degradation machine.

State machine:
  CLOUD_CONNECTED    → all decisions go to Qwen via coordinator
  CLOUD_DEGRADED     → cloud timing out; replay cached decisions by signal similarity
  CLOUD_OFFLINE      → weighted local rules, conservative bias starts
  CLOUD_EXTENDED     → maximum conservatism, any 2-signal co-occurrence = WARNING minimum

On recovery: sends catch-up batch to coordinator so Redis memory isn't stale.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import httpx
import redis.asyncio as redis

logging.basicConfig(level=logging.INFO, format="[edge_agent] %(message)s")
log = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://localhost:8000")

ZONES = ["A", "B", "C"]
SENSOR_TYPES = ["rainfall", "river_level", "soil_saturation", "drain_flow"]

THRESHOLDS = {
    "rainfall":        {"watch": 20,  "warning": 40,  "emergency": 60},
    "river_level":     {"watch": 1.5, "warning": 2.5, "emergency": 3.5},
    "soil_saturation": {"watch": 60,  "warning": 75,  "emergency": 90},
    "drain_flow":      {"watch": 70,  "warning": 85,  "emergency": 95},
}

SIGNAL_WEIGHTS = {
    "river_level":     0.35,
    "rainfall":        0.30,
    "soil_saturation": 0.25,
    "drain_flow":      0.10,
}

DEGRADED_AFTER_SEC = 10
OFFLINE_AFTER_SEC = 30
EXTENDED_AFTER_SEC = 90
CLOUD_CALL_TIMEOUT_SEC = 3.0
CLOUD_PROBE_INTERVAL_SEC = 15   # probe cloud every 15s when degraded/offline/extended
CACHE_SIZE = 20


class CloudState(Enum):
    CONNECTED = "connected"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    EXTENDED = "extended_outage"


@dataclass
class SensorSnapshot:
    zone: str
    readings: dict
    timestamp: float = field(default_factory=time.time)

    def flagged_sensors(self, bias: float = 0.0) -> dict:
        out = {}
        for sensor, value in self.readings.items():
            watch = THRESHOLDS.get(sensor, {}).get("watch", float("inf"))
            if value >= watch * (1.0 - bias):
                out[sensor] = value
        return out

    def signal_fingerprint(self) -> dict:
        fp = {}
        for sensor, value in self.readings.items():
            t = THRESHOLDS.get(sensor, {})
            if value >= t.get("emergency", float("inf")):
                fp[sensor] = "emergency"
            elif value >= t.get("warning", float("inf")):
                fp[sensor] = "warning"
            elif value >= t.get("watch", float("inf")):
                fp[sensor] = "watch"
            else:
                fp[sensor] = "low"
        return fp


@dataclass
class CachedDecision:
    zone: str
    fingerprint: dict
    risk_level: str
    confidence: float
    reasoning: str
    recommended_actions: list
    requires_human_approval: bool
    timestamp: float = field(default_factory=time.time)


class EdgeAgent:
    def __init__(self):
        self.redis: Optional[redis.Redis] = None
        self.http: Optional[httpx.AsyncClient] = None
        self.zone_readings: dict = {z: {} for z in ZONES}
        self.cloud_state = CloudState.CONNECTED
        self.cloud_last_success: float = time.time()
        self.cloud_failure_streak: int = 0
        self.decision_cache: list = []
        self.outage_decisions: list = []

    async def start(self):
        self.redis = redis.from_url(REDIS_URL, decode_responses=True)
        self.http = httpx.AsyncClient(base_url=COORDINATOR_URL, timeout=CLOUD_CALL_TIMEOUT_SEC)
        log.info("Edge agent started. Coordinator: %s | Redis: %s", COORDINATOR_URL, REDIS_URL)

        await asyncio.gather(
            self._subscribe_sensors(),
            self._cloud_state_watchdog(),
            self._cloud_probe_loop(),       # NEW: auto-recovery probe
        )

    async def _subscribe_sensors(self):
        pubsub = self.redis.pubsub()
        channels = [f"sensor:{zone}:{sensor}" for zone in ZONES for sensor in SENSOR_TYPES]
        await pubsub.subscribe(*channels)
        log.info("Subscribed to %d sensor channels", len(channels))

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                payload = json.loads(message["data"])
                zone = payload["zone"]
                sensor = payload["sensor"]
                value = payload["value"]
                self.zone_readings[zone][sensor] = value
                await self._evaluate_zone(zone)
            except Exception as e:
                log.warning("Bad sensor message: %s", e)

    # -----------------------------------------------------------------------
    # Cloud probe loop — auto-recovery from any degraded state
    # -----------------------------------------------------------------------

    async def _cloud_probe_loop(self):
        """
        Periodically probes the coordinator when not CONNECTED.
        This allows the edge agent to recover automatically without a restart.
        """
        while True:
            await asyncio.sleep(CLOUD_PROBE_INTERVAL_SEC)
            if self.cloud_state == CloudState.CONNECTED:
                continue

            elapsed = time.time() - self.cloud_last_success
            log.info("Cloud probe: state=%s, offline=%.0fs — probing coordinator...",
                     self.cloud_state.value, elapsed)
            try:
                response = await self.http.get("/health", timeout=3.0)
                if response.status_code == 200:
                    log.info("Cloud probe: coordinator reachable — resetting to CONNECTED")
                    was_offline = self.cloud_state != CloudState.CONNECTED
                    self.cloud_state = CloudState.CONNECTED
                    self.cloud_last_success = time.time()
                    self.cloud_failure_streak = 0
                    await self._broadcast_cloud_state()
                    if was_offline and self.outage_decisions:
                        asyncio.create_task(self._sync_outage_decisions())
            except Exception:
                log.info("Cloud probe: coordinator still unreachable")

    # -----------------------------------------------------------------------
    # Zone evaluation
    # -----------------------------------------------------------------------

    async def _evaluate_zone(self, zone: str):
        readings = self.zone_readings[zone]
        if len(readings) < 2:
            return

        snapshot = SensorSnapshot(zone=zone, readings=dict(readings))
        bias = self._conservatism_bias()
        flagged = snapshot.flagged_sensors(bias=bias)

        if len(flagged) < 2:
            return

        log.info("Zone %s: %d sensors flagged %s | cloud_state=%s",
                 zone, len(flagged), list(flagged.keys()), self.cloud_state.value)

        decision = await self._route_decision(zone, snapshot, flagged)
        if decision:
            await self._execute_action(zone, decision)

    async def _route_decision(self, zone: str, snapshot: SensorSnapshot, flagged: dict) -> Optional[dict]:
        state = self.cloud_state

        if state == CloudState.CONNECTED:
            return await self._cloud_decision(zone, snapshot.readings)

        elif state == CloudState.DEGRADED:
            decision = await self._cloud_decision(zone, snapshot.readings)
            if decision:
                return decision
            log.info("Zone %s: cloud timed out, attempting cache replay", zone)
            return self._replay_cached_decision(zone, snapshot)

        elif state == CloudState.OFFLINE:
            log.info("Zone %s: cloud offline, using weighted local rules", zone)
            return self._local_weighted_decision(zone, snapshot, flagged)

        elif state == CloudState.EXTENDED:
            log.info("Zone %s: extended outage, maximum conservatism", zone)
            return self._extended_outage_decision(zone, snapshot, flagged)

        return None

    # -----------------------------------------------------------------------
    # State 0 — Cloud decision
    # -----------------------------------------------------------------------

    async def _cloud_decision(self, zone: str, readings: dict) -> Optional[dict]:
        try:
            response = await self.http.post(f"/analyze/{zone}", json=readings)
            if response.status_code == 200:
                data = response.json()
                if "error" not in data:
                    self._on_cloud_success(zone, readings, data)
                    return data
        except (httpx.TimeoutException, httpx.ConnectError):
            self._on_cloud_failure()
        except Exception as e:
            log.warning("Zone %s: unexpected cloud error: %s", zone, e)
            self._on_cloud_failure()
        return None

    # -----------------------------------------------------------------------
    # State 1 — Cache replay
    # -----------------------------------------------------------------------

    def _replay_cached_decision(self, zone: str, snapshot: SensorSnapshot) -> Optional[dict]:
        current_fp = snapshot.signal_fingerprint()
        candidates = [d for d in self.decision_cache if d.zone == zone]

        if not candidates:
            return self._local_weighted_decision(zone, snapshot, snapshot.flagged_sensors())

        def similarity(cached: CachedDecision) -> float:
            score = 0.0
            for sensor in SENSOR_TYPES:
                if current_fp.get(sensor) == cached.fingerprint.get(sensor):
                    score += SIGNAL_WEIGHTS.get(sensor, 0.1)
            return score

        best = max(candidates, key=similarity)
        best_score = similarity(best)
        log.info("Zone %s: replaying cached decision (similarity=%.2f, risk=%s)",
                 zone, best_score, best.risk_level)

        return {
            "zone": zone,
            "risk_level": best.risk_level,
            "confidence": round(best.confidence * 0.75, 2),
            "reasoning": f"[CACHE REPLAY, similarity={best_score:.2f}] {best.reasoning}",
            "recommended_actions": best.recommended_actions,
            "requires_human_approval": best.requires_human_approval,
            "source": "cache_replay",
            "timestamp": time.time(),
        }

    # -----------------------------------------------------------------------
    # State 2 — Weighted local rules
    # -----------------------------------------------------------------------

    def _local_weighted_decision(self, zone: str, snapshot: SensorSnapshot, flagged: dict) -> dict:
        weighted_score = 0.0
        for sensor, value in snapshot.readings.items():
            t = THRESHOLDS.get(sensor, {})
            emergency = t.get("emergency", 1.0)
            normalized = min(value / emergency, 1.0)
            weighted_score += normalized * SIGNAL_WEIGHTS.get(sensor, 0.1)

        risk_level, confidence = self._score_to_risk(weighted_score)
        requires_approval = risk_level == "WARNING"

        decision = {
            "zone": zone,
            "risk_level": risk_level,
            "confidence": round(confidence, 2),
            "reasoning": (
                f"[LOCAL WEIGHTED RULES] Weighted risk score {weighted_score:.2f}. "
                f"Dominant signals: {', '.join(sorted(flagged.keys(), key=lambda s: -SIGNAL_WEIGHTS.get(s, 0)))}."
            ),
            "recommended_actions": self._default_actions(risk_level),
            "requires_human_approval": requires_approval,
            "source": "local_weighted",
            "timestamp": time.time(),
        }
        self.outage_decisions.append(decision)
        return decision

    # -----------------------------------------------------------------------
    # State 3 — Extended outage
    # -----------------------------------------------------------------------

    def _extended_outage_decision(self, zone: str, snapshot: SensorSnapshot, flagged: dict) -> dict:
        emergency_sensors = [
            s for s, v in snapshot.readings.items()
            if v >= THRESHOLDS.get(s, {}).get("emergency", float("inf"))
        ]

        if len(flagged) >= 3 or emergency_sensors:
            risk_level = "EMERGENCY"
            confidence = 0.70
        else:
            risk_level = "WARNING"
            confidence = 0.65

        decision = {
            "zone": zone,
            "risk_level": risk_level,
            "confidence": confidence,
            "reasoning": (
                f"[EXTENDED OUTAGE — MAX CONSERVATISM] Cloud offline >90s. "
                f"{len(flagged)} sensors flagged. Defaulting to {risk_level} to protect life safety."
            ),
            "recommended_actions": self._default_actions(risk_level),
            "requires_human_approval": False,
            "source": "extended_outage",
            "timestamp": time.time(),
        }
        self.outage_decisions.append(decision)
        return decision

    # -----------------------------------------------------------------------
    # Action execution
    # -----------------------------------------------------------------------

    async def _execute_action(self, zone: str, decision: dict):
        risk = decision.get("risk_level", "WATCH")
        source = decision.get("source", "cloud")
        requires_approval = decision.get("requires_human_approval", False)

        action = {
            "type": "action_taken",
            "zone": zone,
            "risk_level": risk,
            "action": risk,
            "requires_human_approval": requires_approval,
            "reasoning": decision.get("reasoning", ""),
            "confidence": decision.get("confidence", 0.0),
            "source": source,
            "timestamp": time.time(),
        }

        log.info("Zone %s: executing %s (source=%s, approval_needed=%s)",
                 zone, risk, source, requires_approval)

        try:
            await self.http.post("/broadcast", json=action, timeout=2.0)
        except Exception:
            pass

        # Also broadcast risk_decision so frontend zone dots update
        risk_event = {
            "type": "risk_decision",
            "zone": zone,
            "risk_level": risk,
            "confidence": decision.get("confidence", 0.0),
            "reasoning": decision.get("reasoning", ""),
            "recommended_actions": decision.get("recommended_actions", []),
            "source": source,
            "timestamp": time.time(),
        }
        try:
            await self.http.post("/broadcast", json=risk_event, timeout=2.0)
        except Exception:
            pass

        await self.redis.publish(f"action:{zone}", json.dumps(action))

    # -----------------------------------------------------------------------
    # Cloud state machine
    # -----------------------------------------------------------------------

    def _on_cloud_success(self, zone: str, readings: dict, decision: dict):
        was_offline = self.cloud_state != CloudState.CONNECTED
        self.cloud_state = CloudState.CONNECTED
        self.cloud_last_success = time.time()
        self.cloud_failure_streak = 0

        snapshot = SensorSnapshot(zone=zone, readings=readings)
        cached = CachedDecision(
            zone=zone,
            fingerprint=snapshot.signal_fingerprint(),
            risk_level=decision.get("risk_level", "WATCH"),
            confidence=decision.get("confidence", 0.5),
            reasoning=decision.get("reasoning", ""),
            recommended_actions=decision.get("recommended_actions", []),
            requires_human_approval=decision.get("requires_human_approval", False),
        )
        self.decision_cache.append(cached)
        if len(self.decision_cache) > CACHE_SIZE:
            self.decision_cache.pop(0)

        if was_offline and self.outage_decisions:
            asyncio.create_task(self._sync_outage_decisions())

    def _on_cloud_failure(self):
        self.cloud_failure_streak += 1
        elapsed = time.time() - self.cloud_last_success

        if elapsed > EXTENDED_AFTER_SEC:
            new_state = CloudState.EXTENDED
        elif elapsed > OFFLINE_AFTER_SEC:
            new_state = CloudState.OFFLINE
        elif self.cloud_failure_streak >= 3 or elapsed > DEGRADED_AFTER_SEC:
            new_state = CloudState.DEGRADED
        else:
            new_state = self.cloud_state

        if new_state != self.cloud_state:
            log.warning("Cloud state: %s → %s (offline %.0fs, streak=%d)",
                        self.cloud_state.value, new_state.value,
                        elapsed, self.cloud_failure_streak)
            self.cloud_state = new_state
            asyncio.create_task(self._broadcast_cloud_state())

    async def _broadcast_cloud_state(self):
        try:
            await self.http.post("/degradation_status", json={
                "cloud_state": self.cloud_state.value,
                "cloud_available": self.cloud_state == CloudState.CONNECTED,
                "offline_seconds": round(time.time() - self.cloud_last_success),
            }, timeout=2.0)
        except Exception:
            await self.redis.publish("system:status", json.dumps({
                "type": "degradation_status",
                "cloud_state": self.cloud_state.value,
                "cloud_available": False,
            }))

    async def _cloud_state_watchdog(self):
        while True:
            await asyncio.sleep(5)
            if self.cloud_state != CloudState.CONNECTED:
                elapsed = time.time() - self.cloud_last_success
                if elapsed > EXTENDED_AFTER_SEC and self.cloud_state != CloudState.EXTENDED:
                    self.cloud_state = CloudState.EXTENDED
                    await self._broadcast_cloud_state()
                elif OFFLINE_AFTER_SEC < elapsed <= EXTENDED_AFTER_SEC and self.cloud_state == CloudState.DEGRADED:
                    self.cloud_state = CloudState.OFFLINE
                    await self._broadcast_cloud_state()

    async def _sync_outage_decisions(self):
        if not self.outage_decisions:
            return
        log.info("Cloud reconnected. Syncing %d outage decisions to coordinator.",
                 len(self.outage_decisions))
        try:
            response = await self.http.post("/catchup", json={
                "decisions": self.outage_decisions
            }, timeout=10.0)
            if response.status_code == 200:
                log.info("Catch-up sync acknowledged.")
                self.outage_decisions.clear()
        except Exception as e:
            log.warning("Catch-up sync error: %s", e)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _conservatism_bias(self) -> float:
        return {
            CloudState.CONNECTED: 0.0,
            CloudState.DEGRADED:  0.0,
            CloudState.OFFLINE:   0.05,
            CloudState.EXTENDED:  0.15,
        }.get(self.cloud_state, 0.0)

    def _score_to_risk(self, weighted_score: float) -> tuple:
        if weighted_score >= 0.70:
            return "EMERGENCY", min(weighted_score, 0.95)
        elif weighted_score >= 0.45:
            return "WARNING", weighted_score * 0.90
        else:
            return "WATCH", weighted_score * 0.85

    def _default_actions(self, risk_level: str) -> list:
        return {
            "WATCH":     ["monitor drainage", "alert maintenance crew"],
            "WARNING":   ["close flood barriers", "notify emergency services", "evacuation advisory"],
            "EMERGENCY": ["mandatory evacuation", "emergency services deployed", "all barriers closed"],
        }.get(risk_level, [])


if __name__ == "__main__":
    agent = EdgeAgent()
    asyncio.run(agent.start())
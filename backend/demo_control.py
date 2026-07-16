"""
demo_control.py — Live scenario and cloud state control for FloodGuard AI demo.

Commands:
    normal | light_rain | heavy_storm | flash_flood   — switch sensor scenario
    cloud off                                          — real cloud outage (drives edge_agent's state machine)
    cloud on                                           — restore cloud
    cloud degrade                                      — simulate slow/flaky cloud (real timeout, not scripted)
    status                                             — show current cloud state
    weights                                            — show current adaptive cache-replay signal weights
    quit

IMPORTANT: cloud on/off/degrade now call the coordinator's REST endpoints
(/cloud/on, /cloud/off, /cloud/degrade) directly -- the same endpoints the
frontend's DemoControlPanel uses. An earlier version of this script instead
published to the Redis "demo:control" channel, which only reached the
sensor agents' own (now-removed) local fallback logic and never touched
the coordinator or edge_agent at all -- so "cloud off" here didn't actually
put the edge agent's real 4-state degradation machine into DEGRADED/OFFLINE/
EXTENDED like the docstring claimed; it silently drove a completely
different, now-deleted code path instead.
"""

import asyncio
import json
import os

import httpx
import redis.asyncio as redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://localhost:8000")
VALID_SCENARIOS = {"normal", "light_rain", "heavy_storm", "flash_flood"}


async def main():
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    http = httpx.AsyncClient(base_url=COORDINATOR_URL, timeout=5.0)

    print("=" * 50)
    print("FloodGuard Demo Control")
    print("=" * 50)
    print("Scenarios : normal | light_rain | heavy_storm | flash_flood")
    print("Cloud     : cloud off | cloud on | cloud degrade")
    print("Info      : status | weights")
    print("Exit      : quit")
    print()

    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, input, "> ")
        line = line.strip().lower()

        if line == "quit":
            break

        elif line in VALID_SCENARIOS:
            # Scenario changes still go over Redis -- sensor agents are pure
            # data producers and this is the only thing they need to react to.
            await redis_client.publish(
                "demo:control",
                json.dumps({"action": "set_scenario", "scenario": line}),
            )
            print(f"  Scenario switched to: {line}")

        elif line == "cloud off":
            try:
                await http.post("/cloud/off")
                print("  Cloud disabled on the coordinator. The edge agent's next")
                print("  /analyze call will fail for real -- watch its logs for")
                print("  DEGRADED -> OFFLINE -> EXTENDED transitions at 10s/30s/90s.")
            except httpx.RequestError as e:
                print(f"  Could not reach coordinator: {e}")

        elif line == "cloud on":
            try:
                await http.post("/cloud/on")
                print("  Cloud restored. Edge agent will sync outage decisions, then return to CONNECTED.")
            except httpx.RequestError as e:
                print(f"  Could not reach coordinator: {e}")

        elif line == "cloud degrade":
            try:
                await http.post("/cloud/degrade")
                print("  Coordinator will now sleep ~18s before responding to /analyze --")
                print("  longer than the edge agent's 16s timeout, so it hits a REAL")
                print("  timeout and transitions into DEGRADED (cache replay). Switch")
                print("  back with: cloud on")
            except httpx.RequestError as e:
                print(f"  Could not reach coordinator: {e}")

        elif line == "status":
            try:
                health = (await http.get("/health")).json()
                state = await redis_client.get("edge:cloud_state")
                print(f"  Coordinator cloud_enabled : {health.get('cloud_enabled')}")
                print(f"  Edge agent cloud_state    : {state or 'unknown (edge agent not running / no state yet)'}")
            except httpx.RequestError as e:
                print(f"  Could not reach coordinator: {e}")

        elif line == "weights":
            raw = await redis_client.get("edge:signal_weights")
            if raw:
                weights = json.loads(raw)
                print("  Adaptive cache-replay signal weights (nudged by real outage feedback):")
                for sensor, w in sorted(weights.items(), key=lambda kv: -kv[1]):
                    print(f"    {sensor:16s} {w:.4f}")
            else:
                print("  No adaptive weights recorded yet (edge agent hasn't been through an")
                print("  outage + reconnect cycle since last restart) -- using config defaults.")

        else:
            print(f"  Unknown command: {line!r}")
            print("  Try: normal | light_rain | heavy_storm | flash_flood | cloud off | cloud on | cloud degrade | status | weights | quit")

    await http.aclose()


if __name__ == "__main__":
    asyncio.run(main())

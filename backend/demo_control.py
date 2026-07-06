"""
demo_control.py — Live scenario and cloud state control for FloodGuard AI demo.

Commands:
    normal | light_rain | heavy_storm | flash_flood   — switch sensor scenario
    cloud off                                          — simulate cloud going offline
    cloud on                                           — restore cloud
    cloud degrade                                      — simulate slow/flaky cloud (streak of timeouts)
    status                                             — show current cloud state from edge agent
    quit
"""

import asyncio
import json
import os

import redis.asyncio as redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
VALID_SCENARIOS = {"normal", "light_rain", "heavy_storm", "flash_flood"}


async def main():
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)

    print("=" * 50)
    print("FloodGuard Demo Control")
    print("=" * 50)
    print("Scenarios : normal | light_rain | heavy_storm | flash_flood")
    print("Cloud     : cloud off | cloud on | cloud degrade")
    print("Info      : status")
    print("Exit      : quit")
    print()

    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, input, "> ")
        line = line.strip().lower()

        if line == "quit":
            break

        elif line in VALID_SCENARIOS:
            await redis_client.publish(
                "demo:control",
                json.dumps({"action": "set_scenario", "scenario": line}),
            )
            print(f"  Scenario switched to: {line}")

        elif line == "cloud off":
            # Publishes to demo:control (sensors read this) AND hits coordinator REST
            await redis_client.publish(
                "demo:control",
                json.dumps({"action": "set_cloud", "available": False}),
            )
            print("  Cloud marked offline. Edge agent will enter DEGRADED → OFFLINE → EXTENDED states.")
            print("  Watch edge_agent logs for state transitions at 10s / 30s / 90s.")

        elif line == "cloud on":
            await redis_client.publish(
                "demo:control",
                json.dumps({"action": "set_cloud", "available": True}),
            )
            print("  Cloud restored. Edge agent will sync outage decisions, then return to CONNECTED.")

        elif line == "cloud degrade":
            # Simulates flaky cloud — edge agent will hit timeout streak → DEGRADED state
            await redis_client.publish(
                "demo:control",
                json.dumps({"action": "set_cloud_degraded", "available": True, "latency_ms": 4000}),
            )
            print("  Cloud set to simulate high latency (4s). Edge agent will hit timeout streak → DEGRADED.")
            print("  Cache replay will activate. Switch back with: cloud on")

        elif line == "status":
            # Read current edge agent state from Redis
            state = await redis_client.get("edge:cloud_state")
            outage_count = await redis_client.llen("edge:outage_decisions")
            cache_count = await redis_client.llen("edge:decision_cache")
            print(f"  Cloud state     : {state or 'unknown'}")
            print(f"  Outage decisions: {outage_count}")
            print(f"  Cached decisions: {cache_count}")

        else:
            print(f"  Unknown command: {line!r}")
            print("  Try: normal | light_rain | heavy_storm | flash_flood | cloud off | cloud on | cloud degrade | status | quit")


if __name__ == "__main__":
    asyncio.run(main())

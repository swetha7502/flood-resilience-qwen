"""
Simple interactive CLI to control the live demo scenario and network state,
without having to hand-type redis-cli pubsub commands.

Run this in a separate terminal while run_agents.py is running.

Usage:
    python demo_control.py

Then type commands like:
    normal
    light_rain
    heavy_storm
    flash_flood
    cloud off
    cloud on
    quit
"""

import asyncio
import json
import os
import urllib.request

import redis.asyncio as redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://localhost:8000")
VALID_SCENARIOS = {"normal", "light_rain", "heavy_storm", "flash_flood"}


def http_post(url: str):
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[control] HTTP error: {e}")
        return None


async def main():
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)

    print("Demo control ready. Commands:")
    print("  normal | light_rain | heavy_storm | flash_flood")
    print("  cloud on | cloud off")
    print("  quit")

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
            print(f"[control] scenario -> {line}")

        elif line == "cloud off":
            result = http_post(f"{COORDINATOR_URL}/cloud/off")
            if result:
                print(f"[control] cloud -> OFF (coordinator + agents notified)")

        elif line == "cloud on":
            result = http_post(f"{COORDINATOR_URL}/cloud/on")
            if result:
                print(f"[control] cloud -> ON (coordinator + agents notified)")

        else:
            print(f"Unknown command: {line}")


if __name__ == "__main__":
    asyncio.run(main())
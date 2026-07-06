"""
Entry point for the edge sensor simulation layer.

Run this to start all sensor agents for all zones. Each agent runs as an
independent asyncio task and publishes to its own Redis channel, exactly
mirroring how independent physical edge devices would behave.

Usage:
    python run_agents.py

While running, use redis-cli in another terminal to watch raw readings:
    redis-cli psubscribe "sensor:*"

To control the demo scenario live, this script also listens on a control
channel "demo:control" for messages like:
    {"action": "set_scenario", "scenario": "heavy_storm"}
    {"action": "set_cloud", "available": false}

This lets Person B's frontend (or a simple CLI) drive the demo without
restarting any process.
"""

import asyncio
import json
import os
import sys

import redis.asyncio as redis

sys.path.insert(0, os.path.dirname(__file__))
from agents.sensor_agent import SensorAgent  # noqa: E402

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
SCENARIOS_PATH = os.path.join(os.path.dirname(__file__), "config", "scenarios.json")


async def control_listener(redis_client, agents: list[SensorAgent]):
    """Listens for live scenario/network control messages and applies them
    to every agent at once, so the whole neighborhood reacts together."""
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("demo:control")

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            cmd = json.loads(message["data"])
        except json.JSONDecodeError:
            continue

        if cmd.get("action") == "set_scenario":
            scenario = cmd["scenario"]
            for agent in agents:
                agent.set_scenario(scenario)
            print(f"[control] scenario -> {scenario}")

        elif cmd.get("action") == "set_cloud":
            available = cmd["available"]
            for agent in agents:
                agent.set_cloud_available(available)
            print(f"[control] cloud_available -> {available}")


async def main():
    with open(SCENARIOS_PATH) as f:
        scenarios = json.load(f)

    redis_client = redis.from_url(REDIS_URL, decode_responses=True)

    agents: list[SensorAgent] = []
    for zone, zone_cfg in scenarios["zones"].items():
        for sensor_type in zone_cfg["sensors"]:
            agents.append(SensorAgent(sensor_type, zone, redis_client, scenarios))

    print(f"Starting {len(agents)} sensor agents across {len(scenarios['zones'])} zones...")
    for agent in agents:
        print(f"  - zone {agent.zone}: {agent.sensor_type}")

    tasks = [asyncio.create_task(agent.run()) for agent in agents]
    tasks.append(asyncio.create_task(control_listener(redis_client, agents)))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())

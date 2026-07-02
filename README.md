# Flood resilience backend — Person A's half

This covers the edge sensor simulation, Redis messaging, and (coming Day 2+)
the Qwen-powered coordinator. See the full project plan markdown file for
overall context and the work split with Person B.

## Day 1 setup

1. Install Redis locally (not included in this repo):
   - macOS: `brew install redis && brew services start redis`
   - Ubuntu/Debian: `sudo apt install redis-server && sudo systemctl start redis`
   - Or run via Docker: `docker run -d -p 6379:6379 redis:7-alpine`

2. Set up the Python environment:
   ```
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. Run the sensor agents:
   ```
   python run_agents.py
   ```
   You should see all agents listed at startup, then nothing else printed --
   that's expected, they're publishing silently to Redis.

4. In a second terminal, watch raw sensor data stream in:
   ```
   redis-cli psubscribe "sensor:*"
   ```
   You should see JSON readings appear every ~2 seconds per sensor.

5. In a third terminal, drive the demo scenario live:
   ```
   python demo_control.py
   ```
   Try typing `heavy_storm` and watch the values in terminal 2 change.
   Try `cloud off` then wait ~60s with a scenario active above watch
   threshold -- you should see a local escalation event appear on the
   `action:<zone>` channel (subscribe to `action:*` in a 4th terminal to see
   these: `redis-cli psubscribe "action:*"`).

## What's built so far (Day 1)

- 7 independent sensor agents across 3 zones (A, B, C), each its own asyncio task
- Scenario-driven value generation (normal / light_rain / heavy_storm / flash_flood)
- Per-sensor threshold flagging
- Local fallback rule: when cloud is marked unavailable and a sensor stays
  flagged past 60 seconds, it self-escalates to a WARNING decision on its
  own, with no coordinator or Qwen involvement -- this is the graceful
  degradation proof point for Track 5.
- Live demo control channel so scenarios/network state can be changed
  without restarting agents (important for a smooth live demo on Day 5)

## Coming next (Day 2)

- FastAPI coordinator service that subscribes to all sensor channels
- Multi-signal co-occurrence detection
- Qwen3.7-Max API integration for risk fusion + reasoning
- Per-zone history stored in Redis, fed back into future Qwen calls

## Zone layout

- **Zone A** — upper neighborhood, set back from river. Sensors: rainfall, soil_saturation.
- **Zone B** — riverside, highest historical flood risk. Sensors: river_level, drain_flow, soil_saturation.
- **Zone C** — low-lying central block, drain-dependent. Sensors: rainfall, drain_flow.

This zone split matters for the demo: Zone B should be the one that
visibly floods first and worst, since it has the most sensors and the
highest-risk profile -- that's your visual story on the neighborhood map.

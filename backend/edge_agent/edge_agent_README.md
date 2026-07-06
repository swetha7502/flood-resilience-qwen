# Edge Agent — FloodGuard AI

## What this is

`edge_agent.py` is the **local intelligence layer** that sits between sensors and the cloud.

Before Day 4, the coordinator handled everything — sensors published to Redis, coordinator subscribed, called Qwen, executed actions. That's a cloud-only architecture. The edge agent restructure makes this genuinely EdgeAgent (Track 5).

```
[Sensors] → Redis pub/sub
                ↓
        [edge_agent.py]          ← runs locally / on-device
          - pre-filter noise
          - route to cloud or local rules
          - execute actions
          - manage degradation state
                ↓ HTTP (when cloud reachable)
        [coordinator.py]         ← Alibaba Cloud ECS
          - Qwen API calls
          - Redis zone memory
          - WebSocket to frontend
```

The key inversion: **the edge agent is the orchestrator**. The coordinator is a cloud service it calls.

---

## 4-State Degradation Machine

| State | When | Behavior |
|---|---|---|
| `CONNECTED` | Normal | All decisions via Qwen |
| `DEGRADED` | Cloud timeouts start (3+ failures or 10s) | Try cloud with 3s timeout; on failure, replay nearest cached decision |
| `OFFLINE` | 30s without successful cloud call | Weighted local rules (river_level weighted highest), 5% threshold bias |
| `EXTENDED` | 90s without successful cloud call | Any 2+ signals → WARNING minimum; 15% threshold bias; no approval gate |

Transitions are **one-way downward** (connected → degraded → offline → extended) until cloud recovers.

On recovery: edge agent sends a **catch-up batch** to coordinator so Redis zone memory isn't stale before the next Qwen call.

---

## How to run

**Terminal 1 — sensors:**
```
python run_agents.py
```

**Terminal 2 — edge agent (new):**
```
cd edge_agent
python edge_agent.py
```

**Terminal 3 — coordinator:**
```
cd coordinator
uvicorn coordinator:app --reload --port 8000
```

**Terminal 4 — demo control:**
```
python demo_control.py
```

---

## Demo sequence for judges

1. Start all 4 terminals
2. `heavy_storm` in demo_control → watch Qwen calls firing (CONNECTED)
3. `cloud off` → watch logs:
   - ~10s: `CONNECTED → DEGRADED` (cache replay active)
   - ~30s: `DEGRADED → OFFLINE` (weighted local rules)
   - ~90s: `OFFLINE → EXTENDED` (maximum conservatism)
4. `cloud on` → edge agent syncs outage decisions, returns to CONNECTED

---

## Signal weights

Derived from scenario data — river_level is the strongest flood predictor:

| Sensor | Weight |
|---|---|
| river_level | 0.35 |
| rainfall | 0.30 |
| soil_saturation | 0.25 |
| drain_flow | 0.10 |

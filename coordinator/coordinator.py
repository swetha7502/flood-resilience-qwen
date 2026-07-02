"""
FastAPI Coordinator Service — the cloud reasoning brain.

This service:
  1. Subscribes to all sensor Redis channels
  2. Detects when 2+ sensors in the same zone are flagged simultaneously
  3. Calls Qwen3.7-Max (or qwen-plus) to fuse signals into a risk decision
  4. Stores every decision in Redis as zone history (memory layer)
  5. Feeds last 5 historical events back into future Qwen calls
  6. Applies 3-tier action logic (WATCH / WARNING / EMERGENCY)
  7. Broadcasts all decisions + actions to a WebSocket channel for the frontend
  8. Detects when it can't reach Qwen and tells agents to enter local mode
"""

import asyncio
import json
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Optional

import redis.asyncio as redis
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI

load_dotenv()

# ─── Config ────────────────────────────────────────────────────────────────────

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")
QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen-plus")  # swap to qwen3.7-max for final demo

SCENARIOS_PATH = os.path.join(os.path.dirname(__file__), "..","config", "scenarios.json")

# How many flagged sensors in one zone must co-occur to trigger a Qwen call
COOCCURRENCE_THRESHOLD = 2

# How many historical events to feed back into each Qwen call (memory layer)
HISTORY_WINDOW = 5

# Seconds between repeated Qwen calls for the same zone (debounce)
QWEN_DEBOUNCE_SECONDS = 15

# ─── Globals ───────────────────────────────────────────────────────────────────

redis_client: Optional[redis.Redis] = None
qwen_client: Optional[AsyncOpenAI] = None
scenarios: dict = {}

# Tracks currently flagged sensors per zone: {zone: {sensor_type: timestamp}}
flagged_sensors: dict = defaultdict(dict)

# Tracks last Qwen call time per zone to avoid hammering the API
last_qwen_call: dict = {}

# Master cloud toggle — when False, coordinator skips Qwen and runs in degraded mode
# This is what demo_control.py's "cloud off" command controls
cloud_available: bool = True

# Connected WebSocket clients (Person B's frontend connects here)
ws_clients: list[WebSocket] = []

# ─── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, qwen_client, scenarios

    with open(SCENARIOS_PATH) as f:
        scenarios = json.load(f)

    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    qwen_client = AsyncOpenAI(
        api_key=DASHSCOPE_API_KEY,
        base_url=QWEN_BASE_URL,
    )

    # Start background sensor subscriber
    asyncio.create_task(sensor_subscriber())
    print("[coordinator] started — subscribed to all sensor channels")
    yield
    await redis_client.aclose()

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── WebSocket broadcast ────────────────────────────────────────────────────────

async def broadcast(message: dict):
    """Push a message to all connected frontend WebSocket clients."""
    if not ws_clients:
        return
    data = json.dumps(message)
    disconnected = []
    for ws in ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        ws_clients.remove(ws)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.append(websocket)
    print(f"[ws] frontend connected ({len(ws_clients)} total)")
    try:
        while True:
            await websocket.receive_text()  # keep connection alive
    except WebSocketDisconnect:
        ws_clients.remove(websocket)
        print("[ws] frontend disconnected")

# ─── Memory layer ───────────────────────────────────────────────────────────────

async def store_decision(zone: str, decision: dict):
    """Append a Qwen decision to this zone's history list in Redis."""
    key = f"history:{zone}"
    await redis_client.rpush(key, json.dumps(decision))
    # Keep only the last 20 events per zone
    await redis_client.ltrim(key, -20, -1)

async def get_zone_history(zone: str) -> list[dict]:
    """Retrieve the last HISTORY_WINDOW decisions for this zone."""
    key = f"history:{zone}"
    raw = await redis_client.lrange(key, -HISTORY_WINDOW, -1)
    return [json.loads(r) for r in raw]

# ─── Qwen reasoning call ────────────────────────────────────────────────────────

async def call_qwen(zone: str, readings: dict) -> Optional[dict]:
    """
    The core AI call. Sends current multi-sensor readings + zone history
    to Qwen and gets back a structured risk classification.

    Returns a dict with: risk_level, reasoning, confidence, recommended_actions
    Returns None if the call fails (triggers degraded mode).
    """
    history = await get_zone_history(zone)
    history_text = ""
    if history:
        history_text = "\n".join([
            f"- [{h['timestamp']}] {h['risk_level']}: {h['reasoning'][:100]}"
            for h in history[-HISTORY_WINDOW:]
        ])
    else:
        history_text = "No prior events recorded for this zone."

    readings_text = "\n".join([
        f"- {sensor}: {data['value']} {data['unit']} (flagged: {data['flagged']})"
        for sensor, data in readings.items()
    ])

    prompt = f"""You are a flood risk reasoning system for Zone {zone}.

Current sensor readings:
{readings_text}

Recent history for Zone {zone}:
{history_text}

Based on these multi-sensor readings and historical context, classify the current flood risk.
Consider correlations between sensors — multiple sensors flagging simultaneously is more
serious than any single reading alone.

Respond ONLY with valid JSON, no other text:
{{
  "risk_level": "normal" | "watch" | "warning" | "emergency",
  "reasoning": "2-3 sentence explanation referencing specific sensor values and patterns",
  "confidence": 0.0 to 1.0,
  "recommended_actions": ["action1", "action2"]
}}"""

    try:
        response = await qwen_client.chat.completions.create(
            model=QWEN_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a flood risk reasoning system. Always respond with "
                        "valid JSON only. No markdown, no explanation outside the JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=300,
        )
        raw = response.choices[0].message.content.strip()
        # Strip markdown fences if model adds them despite instructions
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[qwen] call failed for zone {zone}: {e}")
        return None

# ─── Action layer ───────────────────────────────────────────────────────────────

async def apply_action(zone: str, decision: dict, source: str):
    """
    3-tier action logic:
      WATCH     -> auto-close valve, log, no human needed
      WARNING   -> push human checkpoint to frontend
      EMERGENCY -> autonomous broadcast, log why no human asked
    """
    risk = decision.get("risk_level", "normal")
    now = time.time()

    base_event = {
        "type": "risk_decision",
        "zone": zone,
        "timestamp": now,
        "payload": {
            "risk_level": risk,
            "reasoning": decision.get("reasoning", ""),
            "confidence": decision.get("confidence", 0.0),
            "source": source,
            "recommended_actions": decision.get("recommended_actions", []),
        },
    }

    # Always broadcast the decision to the frontend
    await broadcast(base_event)

    # Store in zone history (memory layer)
    await store_decision(zone, {
        "timestamp": now,
        "risk_level": risk,
        "reasoning": decision.get("reasoning", ""),
        "source": source,
    })

    # Publish to Redis action channel (agents listen here for degradation signals)
    await redis_client.publish(f"action:{zone}", json.dumps(base_event))

    if risk == "watch":
        action = {
            "type": "action_taken",
            "zone": zone,
            "timestamp": now,
            "payload": {
                "action": "valve_closed",
                "requires_human_approval": False,
                "note": "Automatic protective action at WATCH level.",
            },
        }
        await broadcast(action)
        print(f"[action] Zone {zone}: WATCH — valve closed automatically")

    elif risk == "warning":
        checkpoint = {
            "type": "action_taken",
            "zone": zone,
            "timestamp": now,
            "payload": {
                "action": "human_checkpoint_raised",
                "requires_human_approval": True,
                "message": (
                    f"Zone {zone} is at WARNING level. "
                    f"Qwen recommends: {', '.join(decision.get('recommended_actions', []))}. "
                    f"Approve to broadcast neighborhood alert?"
                ),
            },
        }
        await broadcast(checkpoint)
        print(f"[action] Zone {zone}: WARNING — human checkpoint pushed to frontend")

    elif risk == "emergency":
        action = {
            "type": "action_taken",
            "zone": zone,
            "timestamp": now,
            "payload": {
                "action": "emergency_broadcast",
                "requires_human_approval": False,
                "note": (
                    "EMERGENCY level reached. Autonomous broadcast sent without "
                    "human approval — conditions too severe to wait."
                ),
            },
        }
        await broadcast(action)
        print(f"[action] Zone {zone}: EMERGENCY — autonomous broadcast, no human needed")

# ─── Sensor subscriber ──────────────────────────────────────────────────────────

async def sensor_subscriber():
    """
    Background task: subscribes to all sensor channels and maintains
    the flagged_sensors dict. When 2+ sensors in a zone are flagged
    simultaneously, triggers a Qwen call (with debounce).
    """
    pubsub = redis_client.pubsub()
    await pubsub.psubscribe("sensor:*")
    print("[coordinator] subscribed to sensor:*")

    async for message in pubsub.listen():
        if message["type"] != "pmessage":
            continue

        try:
            reading = json.loads(message["data"])
        except json.JSONDecodeError:
            continue

        zone = reading["zone"]
        sensor = reading["sensor"]
        now = time.time()

        # Update flagged state for this sensor
        if reading["flagged"]:
            flagged_sensors[zone][sensor] = reading
        else:
            flagged_sensors[zone].pop(sensor, None)

        # Broadcast raw sensor reading to frontend
        await broadcast({
            "type": "sensor_reading",
            "zone": zone,
            "timestamp": now,
            "payload": {
                "sensor": sensor,
                "value": reading["value"],
                "unit": reading["unit"],
                "flagged": reading["flagged"],
            },
        })

        # Check co-occurrence: 2+ sensors flagged in this zone
        currently_flagged = flagged_sensors[zone]
        if len(currently_flagged) >= COOCCURRENCE_THRESHOLD:
            # Debounce: don't call Qwen again if we called recently
            last_call = last_qwen_call.get(zone, 0)
            if now - last_call < QWEN_DEBOUNCE_SECONDS:
                continue

            last_qwen_call[zone] = now

            # If cloud is toggled off, skip Qwen and go straight to degraded mode
            if not cloud_available:
                print(f"[coordinator] Zone {zone}: cloud OFF — skipping Qwen, degraded mode active")
                await broadcast({
                    "type": "degradation_status",
                    "zone": zone,
                    "timestamp": now,
                    "payload": {"cloud_available": False},
                })
                continue

            print(f"[coordinator] Zone {zone}: {len(currently_flagged)} sensors flagged — calling Qwen")

            # Notify frontend that a Qwen call is happening
            await broadcast({
                "type": "qwen_call_started",
                "zone": zone,
                "timestamp": now,
                "payload": {"flagged_sensors": list(currently_flagged.keys())},
            })

            decision = await call_qwen(zone, currently_flagged)

            if decision:
                await apply_action(zone, decision, source="qwen")
                # Tell agents cloud is available
                await redis_client.publish(
                    "demo:control",
                    json.dumps({"action": "set_cloud", "available": True}),
                )
            else:
                # Qwen call failed — tell agents to enter local mode
                print(f"[coordinator] Qwen unreachable — signalling degraded mode")
                await redis_client.publish(
                    "demo:control",
                    json.dumps({"action": "set_cloud", "available": False}),
                )
                await broadcast({
                    "type": "degradation_status",
                    "zone": zone,
                    "timestamp": now,
                    "payload": {"cloud_available": False},
                })

# ─── REST endpoints ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "model": QWEN_MODEL, "zones": list(scenarios.get("zones", {}).keys()), "cloud_available": cloud_available}

@app.post("/cloud/{state}")
async def toggle_cloud(state: str):
    """Toggle cloud/Qwen availability for demo purposes.
    POST /cloud/off  — coordinator stops calling Qwen, degraded mode active
    POST /cloud/on   — coordinator resumes Qwen calls
    """
    global cloud_available
    cloud_available = (state == "on")
    await broadcast({
        "type": "degradation_status",
        "zone": "all",
        "timestamp": time.time(),
        "payload": {"cloud_available": cloud_available},
    })
    # Also tell sensor agents
    await redis_client.publish(
        "demo:control",
        json.dumps({"action": "set_cloud", "available": cloud_available}),
    )
    print(f"[coordinator] cloud_available -> {cloud_available}")
    return {"cloud_available": cloud_available}

@app.post("/approve/{zone}")
async def approve_checkpoint(zone: str):
    """Called by the frontend when a human approves a WARNING checkpoint."""
    now = time.time()
    action = {
        "type": "action_taken",
        "zone": zone,
        "timestamp": now,
        "payload": {
            "action": "alert_broadcast_approved",
            "requires_human_approval": False,
            "note": f"Human approved neighborhood alert for Zone {zone}.",
        },
    }
    await broadcast(action)
    await redis_client.publish(f"action:{zone}", json.dumps(action))
    return {"approved": True, "zone": zone}

@app.get("/history/{zone}")
async def zone_history(zone: str):
    """Returns the stored decision history for a zone — useful for debugging."""
    history = await get_zone_history(zone)
    return {"zone": zone, "history": history}
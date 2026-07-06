"""
coordinator.py — Cloud Coordinator Service (Day 4 version)
==========================================================
Now a pure cloud service. The edge agent is the orchestrator.
Responsibilities:
  - Receive /analyze/{zone} calls from edge agent → call Qwen → return decision
  - Maintain Redis zone memory (last 5 decisions per zone)
  - Broadcast all events to frontend via WebSocket
  - Accept /catchup batch from edge agent after cloud recovery
  - Relay degradation status to frontend
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

import redis.asyncio as redis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO, format="[coordinator] %(message)s")
log = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
from dotenv import load_dotenv
load_dotenv()
QWEN_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

app = FastAPI(title="FloodGuard Cloud Coordinator")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

redis_client: Optional[redis.Redis] = None
qwen_client: Optional[AsyncOpenAI] = None
connected_websockets: list[WebSocket] = []
cloud_enabled = True  # toggled by /cloud/on and /cloud/off (demo control)

THRESHOLDS = {
    "rainfall":        {"watch": 20,  "warning": 40,  "emergency": 60},
    "river_level":     {"watch": 1.5, "warning": 2.5, "emergency": 3.5},
    "soil_saturation": {"watch": 60,  "warning": 75,  "emergency": 90},
    "drain_flow":      {"watch": 70,  "warning": 85,  "emergency": 95},
}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    global redis_client, qwen_client
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    qwen_client = AsyncOpenAI(api_key=QWEN_API_KEY, base_url=QWEN_BASE_URL)
    log.info("Coordinator ready.")


# ---------------------------------------------------------------------------
# WebSocket — frontend connection
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_websockets.append(ws)
    log.info("Frontend connected (%d total)", len(connected_websockets))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        connected_websockets.remove(ws)
        log.info("Frontend disconnected (%d total)", len(connected_websockets))

async def _broadcast(message: dict):
    dead = []
    for ws in connected_websockets:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connected_websockets.remove(ws)


# ---------------------------------------------------------------------------
# Core: analyze a zone (called by edge agent)
# ---------------------------------------------------------------------------

@app.post("/analyze/{zone}")
async def analyze_zone(zone: str, readings: dict):
    """
    Edge agent calls this when 2+ sensors are flagged.
    Returns a Qwen risk decision or {"error": "..."}.
    """
    if not cloud_enabled:
        return {"error": "cloud_disabled"}

    flagged = {k: v for k, v in readings.items() if _is_flagged(k, v)}
    history = await _get_zone_history(zone)

    await _broadcast({"type": "qwen_call_started", "zone": zone, "flagged": flagged})

    prompt = _build_prompt(zone, readings, flagged, history)
    try:
        response = await asyncio.wait_for(
            qwen_client.chat.completions.create(
                model="qwen-plus",
                messages=[
                    {"role": "system", "content": "You are a flood risk AI. Respond ONLY with valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            ),
            timeout=8.0,
        )
        raw = response.choices[0].message.content.strip()
        decision = json.loads(raw.replace("```json", "").replace("```", ""))
    except asyncio.TimeoutError:
        log.warning("Zone %s: Qwen timed out", zone)
        return {"error": "qwen_timeout"}
    except Exception as e:
        log.warning("Zone %s: Qwen error: %s", zone, e)
        return {"error": str(e)}

    risk = decision.get("risk_level", "WATCH")
    decision.update({
        "zone": zone,
        "timestamp": time.time(),
        "source": "cloud",
        "requires_human_approval": risk == "WARNING",  # only WARNING needs human approval
    })

    await _store_decision(zone, decision)
    await _broadcast({"type": "risk_decision", "zone": zone, **decision})
    log.info("Zone %s: Qwen → %s (confidence=%.2f)", zone,
             decision.get("risk_level"), decision.get("confidence", 0))
    return decision


# ---------------------------------------------------------------------------
# Broadcast relay — edge agent forwards action events here for WS delivery
# ---------------------------------------------------------------------------

@app.post("/broadcast")
async def broadcast_event(payload: dict):
    """Edge agent calls this to push any event to frontend via WebSocket."""
    await _broadcast(payload)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Catch-up sync — called by edge agent on cloud recovery
# ---------------------------------------------------------------------------

@app.post("/catchup")
async def catchup(payload: dict):
    """
    Receives all local decisions made during outage.
    Writes them into Redis zone memory so next Qwen call has accurate history.
    """
    decisions = payload.get("decisions", [])
    log.info("Catch-up sync: received %d outage decisions", len(decisions))

    for decision in decisions:
        zone = decision.get("zone")
        if zone:
            await _store_decision(zone, decision)
            await _broadcast({
                "type": "risk_decision",
                **decision,
                "catchup": True,  # frontend can visually distinguish these
            })

    log.info("Catch-up sync complete.")
    return {"ok": True, "synced": len(decisions)}


# ---------------------------------------------------------------------------
# Degradation status relay
# ---------------------------------------------------------------------------

@app.post("/degradation_status")
async def degradation_status(payload: dict):
    """Edge agent reports cloud state changes; we relay to frontend."""
    await _broadcast({"type": "degradation_status", **payload})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Demo controls (unchanged from Day 3)
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "cloud_enabled": cloud_enabled}

@app.post("/cloud/off")
async def cloud_off():
    global cloud_enabled
    cloud_enabled = False
    await redis_client.publish("demo:control", json.dumps({"action": "set_cloud", "available": False}))
    await _broadcast({"type": "degradation_status", "cloud_available": False, "cloud_state": "offline"})
    log.info("Cloud disabled via demo control")
    return {"cloud_enabled": False}

@app.post("/cloud/on")
async def cloud_on():
    global cloud_enabled
    cloud_enabled = True
    await redis_client.publish("demo:control", json.dumps({"action": "set_cloud", "available": True}))
    await _broadcast({"type": "degradation_status", "cloud_available": True, "cloud_state": "connected"})
    log.info("Cloud enabled via demo control")
    return {"cloud_enabled": True}

@app.post("/approve/{zone}")
async def approve_action(zone: str):
    action = {
        "type": "action_taken", "zone": zone,
        "action": "WARNING_APPROVED", "requires_human_approval": False,
        "reasoning": "Human operator approved the WARNING action.",
        "timestamp": time.time(),
    }
    await _broadcast(action)
    await redis_client.publish(f"action:{zone}", json.dumps(action))
    return {"approved": True, "zone": zone}

@app.get("/history/{zone}")
async def zone_history(zone: str):
    history = await _get_zone_history(zone)
    return {"zone": zone, "history": history}


# ---------------------------------------------------------------------------
# Redis memory helpers
# ---------------------------------------------------------------------------

async def _get_zone_history(zone: str) -> list:
    raw = await redis_client.lrange(f"history:{zone}", 0, 4)
    return [json.loads(r) for r in raw]

async def _store_decision(zone: str, decision: dict):
    await redis_client.lpush(f"history:{zone}", json.dumps(decision))
    await redis_client.ltrim(f"history:{zone}", 0, 4)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_flagged(sensor: str, value: float) -> bool:
    return value >= THRESHOLDS.get(sensor, {}).get("watch", float("inf"))

def _build_prompt(zone, readings, flagged, history):
    history_text = "\n".join([
        f"  - [{h.get('timestamp','')}] risk={h.get('risk_level','?')} "
        f"confidence={h.get('confidence','?')} source={h.get('source','cloud')}: {h.get('reasoning','')}"
        for h in history
    ]) or "  None yet."

    return f"""Zone {zone} flood risk assessment.

Current sensor readings:
{json.dumps(readings, indent=2)}

Flagged sensors (at or above WATCH threshold):
{json.dumps(flagged, indent=2)}

Recent decision history for Zone {zone} (includes any local fallback decisions):
{history_text}

Respond ONLY with JSON in this exact format:
{{
  "risk_level": "WATCH|WARNING|EMERGENCY",
  "confidence": 0.0-1.0,
  "reasoning": "one sentence explanation",
  "recommended_actions": ["action1", "action2"],
  "requires_human_approval": true|false
}}"""

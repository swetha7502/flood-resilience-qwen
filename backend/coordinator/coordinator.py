"""
coordinator.py — Cloud Coordinator Service
===========================================
Pure cloud service. The edge agent is the orchestrator.
Responsibilities:
  - Receive /analyze/{zone} calls from edge agent → call Qwen → return decision
  - Maintain Redis zone memory (last 5 decisions per zone, for Qwen prompt context)
  - Maintain a durable SQLite audit log of every decision (see history_store.py)
  - Broadcast all events to every connected frontend, across any number of
    coordinator replicas, via Redis pub/sub (see _broadcast / _redis_broadcast_listener)
  - Accept /catchup batch from edge agent after cloud recovery
  - Relay degradation status to frontend
"""

import asyncio
import json
import logging
import os
import sys
import time
from typing import Optional

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.loader import THRESHOLDS, ZONE_LOCATIONS  # noqa: E402
import history_store  # noqa: E402
import mcp_audit_client  # noqa: E402
# THRESHOLDS comes from config/loader.py, the same module edge_agent.py and
# sensor_agent.py use -- previously this was a hardcoded, independently
# drifting copy. ZONE_LOCATIONS backs the get_regional_weather tool below.

logging.basicConfig(level=logging.INFO, format="[coordinator] %(message)s")
log = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
from dotenv import load_dotenv
load_dotenv()
QWEN_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
BROADCAST_CHANNEL = "coordinator:broadcast"

app = FastAPI(title="FloodGuard Cloud Coordinator")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

redis_client: Optional[redis.Redis] = None
qwen_client: Optional[AsyncOpenAI] = None
weather_client: Optional[httpx.AsyncClient] = None
connected_websockets: list[WebSocket] = []
cloud_enabled = True  # toggled by /cloud/on and /cloud/off (demo control)
artificial_delay_sec = 0.0  # set by /cloud/degrade to simulate a flaky/slow link


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    global redis_client, qwen_client, weather_client
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    qwen_client = AsyncOpenAI(api_key=QWEN_API_KEY, base_url=QWEN_BASE_URL)
    weather_client = httpx.AsyncClient(timeout=3.0)
    await history_store.init_db()
    asyncio.create_task(_redis_broadcast_listener())
    log.info("Coordinator ready.")


# ---------------------------------------------------------------------------
# WebSocket — frontend connection
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_websockets.append(ws)
    log.info("Frontend connected (%d total on this instance)", len(connected_websockets))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        connected_websockets.remove(ws)
        log.info("Frontend disconnected (%d total on this instance)", len(connected_websockets))


async def _broadcast(message: dict):
    """
    Publishes to Redis rather than writing directly to connected_websockets.
    This used to iterate connected_websockets in-process, which only ever
    reached frontends connected to this exact coordinator process -- fine
    for one instance, but silently wrong the moment you run a second
    coordinator replica behind a load balancer: a decision made by the
    instance that happened to receive the /analyze call would never reach
    a frontend that happened to be connected to the other instance. Every
    replica subscribes to BROADCAST_CHANNEL (see _redis_broadcast_listener)
    and fans out to its own local sockets, so this now works correctly
    regardless of how many coordinator instances are running.
    """
    await redis_client.publish(BROADCAST_CHANNEL, json.dumps(message))


async def _redis_broadcast_listener():
    """Background task: relays BROADCAST_CHANNEL messages to this instance's
    own locally-connected websockets. Runs for the lifetime of the process."""
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(BROADCAST_CHANNEL)
    log.info("Subscribed to %s for cross-instance broadcast relay", BROADCAST_CHANNEL)
    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            payload = json.loads(message["data"])
        except json.JSONDecodeError:
            continue
        dead = []
        for ws in connected_websockets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            connected_websockets.remove(ws)


# ---------------------------------------------------------------------------
# Core: analyze a zone (called by edge agent)
# ---------------------------------------------------------------------------

# Tool schema Qwen must fill in. Forcing tool_choice means the API itself
# guarantees the argument payload is well-formed JSON matching this schema --
# we're no longer just asking the model nicely to "respond only with JSON"
# and hoping it doesn't wrap the answer in prose or markdown fences.
RISK_ASSESSMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_flood_risk_assessment",
        "description": "Submit the fused multi-signal flood risk assessment for this zone. This must be your final call.",
        "parameters": {
            "type": "object",
            "properties": {
                "risk_level": {"type": "string", "enum": ["WATCH", "WARNING", "EMERGENCY"]},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "reasoning": {"type": "string", "description": "One sentence explanation citing the driving signals."},
                "recommended_actions": {"type": "array", "items": {"type": "string"}},
                "requires_human_approval": {"type": "boolean"},
            },
            "required": ["risk_level", "confidence", "reasoning", "recommended_actions", "requires_human_approval"],
        },
    },
}

# Second tool: lets Qwen cross-check the simulated sensor readings against
# real current conditions at each zone's real-world reference location
# before committing to a risk level. This is genuine multi-hop tool use
# (Qwen decides whether to call it, we execute it, feed the result back,
# then Qwen makes its final call) rather than the backend pre-fetching
# data and stuffing it into the prompt -- the latter would just be prompt
# engineering, not the model actually using a tool.
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_regional_weather",
        "description": (
            "Fetch REAL current precipitation and short-term forecast data for the "
            "real-world location this zone's simulated sensors are modeled after. "
            "Use this at most once, before your final assessment, to sanity-check "
            "whether the simulated readings are consistent with genuine weather "
            "conditions right now -- e.g. an EMERGENCY-tier reading during a real "
            "clear-sky day at the reference location is still possible (this is a "
            "simulation) but worth noting in your reasoning."
        ),
        "parameters": {
            "type": "object",
            "properties": {"zone": {"type": "string", "enum": list(ZONE_LOCATIONS.keys())}},
            "required": ["zone"],
        },
    },
}

MCP_AUDIT_TIMEOUT_SEC = 4.0       # optional MCP audit-trend enrichment, bounded separately
ANALYZE_TOTAL_TIMEOUT_SEC = 9.0   # covers weather lookup + up to 2 Qwen calls;
                                   # edge_agent.py's ANALYZE_CALL_TIMEOUT_SEC (16s) must
                                   # exceed MCP_AUDIT_TIMEOUT_SEC + ANALYZE_TOTAL_TIMEOUT_SEC
                                   # (4 + 9 = 13s worst case) with real margin


async def _fetch_regional_weather(zone: str) -> dict:
    """
    Calls Open-Meteo (free, no API key) for the zone's configured reference
    location. Deliberately fails soft: if the location is unconfigured, the
    API is unreachable, or it's slow, we return a small "unavailable" dict
    instead of raising -- a demo/monitoring feature reaching out to a
    third-party API must never be able to take down risk assessment itself.
    That would make the system LESS robust in the name of adding
    sophistication, which defeats the point on an EdgeAgent/graceful-
    degradation track.
    """
    loc = ZONE_LOCATIONS.get(zone)
    if not loc or "lat" not in loc:
        return {"available": False, "reason": "no reference location configured for this zone"}
    try:
        resp = await weather_client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": loc["lat"],
                "longitude": loc["lon"],
                "current": "precipitation,rain,wind_speed_10m",
                "hourly": "precipitation",
                "forecast_days": 1,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        current = data.get("current", {})
        return {
            "available": True,
            "location_label": loc.get("label", zone),
            "current_precipitation_mm": current.get("precipitation"),
            "current_rain_mm": current.get("rain"),
            "current_wind_speed_10m": current.get("wind_speed_10m"),
        }
    except Exception as e:
        log.warning("Zone %s: weather lookup failed (%s) -- continuing without it", zone, e)
        return {"available": False, "reason": f"weather service error: {e}"}


@app.post("/analyze/{zone}")
async def analyze_zone(zone: str, readings: dict):
    """
    Edge agent calls this when 2+ sensors are flagged (this threshold-gated
    invocation is deliberate cost control: Qwen is called once per
    multi-signal co-occurrence event, not once per sensor reading every
    2 seconds -- most of the sensor stream never reaches this endpoint
    at all, see edge_agent.py's _evaluate_zone).

    Returns a Qwen risk decision or {"error": "..."}.

    Runs a real multi-hop tool-calling loop: Qwen may call
    get_regional_weather at most once to cross-check simulated readings
    against genuine current conditions, then must call
    submit_flood_risk_assessment with its final answer. The whole loop is
    bounded by ANALYZE_TOTAL_TIMEOUT_SEC regardless of how many rounds it
    takes, so a stalled tool round can't hang the request indefinitely.
    """
    if not cloud_enabled:
        return {"error": "cloud_disabled"}

    if artificial_delay_sec > 0:
        # /cloud/degrade sets this to simulate a flaky/high-latency link.
        # Sleeping here (rather than faking a state) means the edge agent's
        # own httpx timeout fires for real, exercising the actual DEGRADED
        # code path instead of a scripted stand-in for it.
        await asyncio.sleep(artificial_delay_sec)

    flagged = {k: v for k, v in readings.items() if _is_flagged(k, v)}
    history = await _get_zone_history(zone)

    await _broadcast({"type": "qwen_call_started", "zone": zone, "flagged": flagged})

    # Optional: real MCP round trip (DashScope -> our own audit-log MCP
    # server) for a natural-language trend summary. Bounded separately
    # from the main tool loop below and added to ANALYZE_TOTAL_TIMEOUT_SEC's
    # budget explicitly -- this is a genuine extra network hop, not free
    # latency, and pretending otherwise would have silently made the
    # edge-agent timeout margin calculated earlier too tight.
    audit_trend = None
    try:
        audit_trend = await asyncio.wait_for(
            mcp_audit_client.get_audit_trend_summary(zone), timeout=MCP_AUDIT_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        log.info("Zone %s: MCP audit-trend lookup timed out -- continuing without it", zone)

    prompt = _build_prompt(zone, readings, flagged, history, audit_trend)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a flood risk fusion AI. You may call get_regional_weather "
                "at most once to cross-check simulated readings against real "
                "conditions, then you MUST call submit_flood_risk_assessment with "
                "your final answer. Never answer in plain text."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    tools = [WEATHER_TOOL, RISK_ASSESSMENT_TOOL]

    try:
        decision = await asyncio.wait_for(
            _run_tool_loop(zone, messages, tools), timeout=ANALYZE_TOTAL_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        log.warning("Zone %s: Qwen tool loop timed out", zone)
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


async def _run_tool_loop(zone: str, messages: list, tools: list, max_rounds: int = 3) -> dict:
    """
    Executes the actual multi-hop exchange with Qwen. Round budget is
    small and fixed (not "keep going until it stops calling tools") on
    purpose: an LLM with a forced tool available can in principle loop
    forever if it keeps re-requesting the same tool, and this endpoint
    sits on a critical path an edge agent is actively waiting on.
    """
    weather_calls_used = 0
    for round_num in range(max_rounds):
        force_final = round_num == max_rounds - 1 or weather_calls_used >= 1
        response = await qwen_client.chat.completions.create(
            model="qwen-plus",
            messages=messages,
            tools=tools,
            tool_choice=(
                {"type": "function", "function": {"name": "submit_flood_risk_assessment"}}
                if force_final
                else "auto"
            ),
            temperature=0.1,
        )
        message = response.choices[0].message

        if not message.tool_calls:
            # Model ignored the forced/offered tool_choice (rare, but
            # possible) -- fall back to parsing message content once
            # rather than looping or silently returning nothing.
            return json.loads(message.content.strip().replace("```json", "").replace("```", ""))

        tool_call = message.tool_calls[0]

        if tool_call.function.name == "submit_flood_risk_assessment":
            return json.loads(tool_call.function.arguments)

        if tool_call.function.name == "get_regional_weather" and weather_calls_used == 0:
            weather_calls_used += 1
            weather_data = await _fetch_regional_weather(zone)
            # Standard OpenAI-style tool-call round trip: echo the
            # assistant's tool call back, then supply the tool's result
            # as a "tool" role message, before asking again.
            messages.append({
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [{
                    "id": tool_call.id,
                    "type": "function",
                    "function": {"name": tool_call.function.name, "arguments": tool_call.function.arguments},
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(weather_data),
            })
            continue

        # Unknown/repeated tool call -- don't trust it, force a final answer.
        break

    # Exhausted rounds without a clean submit_flood_risk_assessment call.
    raise RuntimeError("Qwen did not return a final assessment within the tool-call round budget")


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
    """
    Edge agent reports cloud state changes here on every transition.
    We relay to the frontend AND persist to Redis so the CLI's `status`
    command (demo_control.py) has something real to read -- previously it
    read edge:cloud_state / edge:outage_decisions / edge:decision_cache,
    but nothing ever wrote those keys, so status always printed 'unknown'.
    """
    await redis_client.set("edge:cloud_state", payload.get("cloud_state", "unknown"))
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
    global cloud_enabled, artificial_delay_sec
    cloud_enabled = True
    artificial_delay_sec = 0.0
    await redis_client.publish("demo:control", json.dumps({"action": "set_cloud", "available": True}))
    await _broadcast({"type": "degradation_status", "cloud_available": True, "cloud_state": "connected"})
    log.info("Cloud enabled via demo control")
    return {"cloud_enabled": True}

@app.post("/cloud/degrade")
async def cloud_degrade():
    """
    Simulates a flaky/high-latency link rather than a hard outage: /analyze
    stays enabled but sleeps past the edge agent's ANALYZE_CALL_TIMEOUT_SEC
    (16s -- widened from the original 3s once the weather + MCP audit-trend
    lookups made a real healthy call take longer than that on its own), so
    the edge agent's own httpx timeout fires for real and its state machine
    transitions into DEGRADED through the same failure path a genuine
    network problem would take -- not a scripted/faked state. If you widen
    ANALYZE_CALL_TIMEOUT_SEC in edge_agent.py again, this delay needs to
    grow with it, or "cloud degrade" silently stops demonstrating anything.
    """
    global cloud_enabled, artificial_delay_sec
    cloud_enabled = True
    artificial_delay_sec = 18.0
    await _broadcast({"type": "degradation_status", "cloud_available": True, "cloud_state": "degraded"})
    log.info("Cloud set to simulate high latency (%.0fs) via demo control", artificial_delay_sec)
    return {"cloud_enabled": True, "artificial_delay_sec": artificial_delay_sec}

@app.post("/scenario/{scenario_name}")
async def set_scenario(scenario_name: str):
    """
    Lets the frontend's DemoControlPanel switch the active sensor scenario.

    This didn't exist before -- demo_control.py's CLI publishes directly to
    the "demo:control" Redis channel, which works fine for a backend-side
    Python process with a real Redis connection, but a browser has no way
    to do that. Without this HTTP endpoint, DemoControlPanel's scenario
    buttons had nothing to actually call.

    Publishes the same "demo:control" message run_agents.py's sensor agents
    already listen for (identical payload shape to demo_control.py), so
    behavior is identical whether the scenario was switched from the CLI or
    the browser. Also broadcasts "scenario_changed" over the websocket so
    the frontend can reset zone risk display state -- App.jsx already has
    a handler for this message type.
    """
    valid_scenarios = {"normal", "light_rain", "heavy_storm", "flash_flood"}
    if scenario_name not in valid_scenarios:
        raise HTTPException(status_code=400, detail=f"Unknown scenario '{scenario_name}'. Valid: {sorted(valid_scenarios)}")

    await redis_client.publish(
        "demo:control",
        json.dumps({"action": "set_scenario", "scenario": scenario_name}),
    )
    await _broadcast({"type": "scenario_changed", "scenario": scenario_name})
    log.info("Scenario switched to %s via demo control", scenario_name)
    return {"scenario": scenario_name}


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
    """Ephemeral rolling-5 window -- same data Qwen sees as prompt context."""
    history = await _get_zone_history(zone)
    return {"zone": zone, "history": history}

@app.get("/audit/{zone}")
async def zone_audit(zone: str, limit: int = 200):
    """
    Durable, unbounded (up to `limit`) audit trail for one zone -- this is
    what a real deployment would use for post-incident review, not the
    5-item Redis window above (which exists purely to keep Qwen's prompt
    small, not as a record of what actually happened).
    """
    records = await history_store.query_decisions(zone=zone, limit=limit)
    return {"zone": zone, "count": len(records), "decisions": records}

@app.get("/audit")
async def full_audit(limit: int = 200):
    """Durable audit trail across all zones, most recent first."""
    records = await history_store.query_decisions(zone=None, limit=limit)
    return {"count": len(records), "decisions": records}


# ---------------------------------------------------------------------------
# Redis memory helpers
# ---------------------------------------------------------------------------

async def _get_zone_history(zone: str) -> list:
    raw = await redis_client.lrange(f"history:{zone}", 0, 4)
    return [json.loads(r) for r in raw]

async def _store_decision(zone: str, decision: dict):
    # Fast, ephemeral, deliberately small -- feeds Qwen's next prompt for
    # this zone. Not a record of what happened, just recent context.
    await redis_client.lpush(f"history:{zone}", json.dumps(decision))
    await redis_client.ltrim(f"history:{zone}", 0, 4)
    # Durable, unbounded, queryable via /audit -- the actual record.
    await history_store.record_decision(decision)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_flagged(sensor: str, value: float) -> bool:
    return value >= THRESHOLDS.get(sensor, {}).get("watch", float("inf"))

def _build_prompt(zone, readings, flagged, history, audit_trend=None):
    history_text = "\n".join([
        f"  - [{h.get('timestamp','')}] risk={h.get('risk_level','?')} "
        f"confidence={h.get('confidence','?')} source={h.get('source','cloud')}: {h.get('reasoning','')}"
        for h in history
    ]) or "  None yet."

    audit_section = f"\nAudit-log trend (via MCP): {audit_trend}\n" if audit_trend else ""

    return f"""Zone {zone} flood risk assessment.

Current sensor readings:
{json.dumps(readings, indent=2)}

Flagged sensors (at or above WATCH threshold):
{json.dumps(flagged, indent=2)}

Recent decision history for Zone {zone} (includes any local fallback decisions):
{history_text}
{audit_section}
Respond ONLY with JSON in this exact format:
{{
  "risk_level": "WATCH|WARNING|EMERGENCY",
  "confidence": 0.0-1.0,
  "reasoning": "one sentence explanation",
  "recommended_actions": ["action1", "action2"],
  "requires_human_approval": true|false
}}"""
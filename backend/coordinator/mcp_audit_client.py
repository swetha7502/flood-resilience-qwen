"""
mcp_audit_client.py — calls DashScope's Responses API with our own custom
MCP server (mcp_server/audit_mcp_server.py) configured as a tool, so Qwen
can pull real audit-log trend data before assessing a zone's current risk.

WHY RAW HTTPX, NOT THE `openai` SDK:
  DashScope's MCP support is only available through the Responses API
  (a different endpoint from chat.completions), which needs a reasonably
  recent `openai` Python SDK version. The pinned version in this project,
  openai==1.54.4, predates that support -- and this codebase has a
  documented history of the `openai`/`httpx` version pairing being fragile
  (see requirements.txt comment on the `proxies` keyword crash). Rather
  than risk that again for one feature, this makes a plain HTTP POST to
  the documented REST contract using httpx, which is already a pinned
  dependency. No SDK version changes needed anywhere.

WHAT IS AND ISN'T VERIFIED HERE:
  Verified for real, in this environment: the MCP SERVER side
  (mcp_server/audit_mcp_server.py) -- started it, connected a real MCP
  client over SSE, listed its tools, and called them successfully against
  live audit-log data.

  NOT verified end-to-end: the actual DashScope <-> our-MCP-server round
  trip. That requires a real DASHSCOPE_API_KEY and network access to
  dashscope-intl.aliyuncs.com, neither of which is available in the
  environment this was written in. The request body below matches Alibaba
  Cloud's documented contract exactly (Model Studio: MCP, retrieved
  2026-07-16); the response parsing is written defensively (see
  _extract_output_text) because the exact raw JSON shape wasn't shown in
  that documentation -- only the SDK's derived `.output_text` convenience
  property was. Test this specific piece once real API credits are
  available, the same way the weather tool needed real-environment
  testing.

DEPLOYMENT REQUIREMENT: MCP_SERVER_PUBLIC_URL must be a URL DashScope's
infrastructure can actually reach over the internet (e.g. the same
Alibaba Cloud host the coordinator runs on, on a different port/path) --
NOT localhost. DashScope calls the MCP server directly; it doesn't route
through this backend.
"""

import json
import logging
import os

import httpx

log = logging.getLogger(__name__)

# Different base_url than the chat.completions calls in coordinator.py --
# MCP support is specifically under this Responses API path.
DASHSCOPE_RESPONSES_URL = (
    "https://dashscope-intl.aliyuncs.com/api/v2/apps/protocols/compatible-mode/v1/responses"
)
# Per Alibaba's documented supported-models list for MCP: qwen-plus (used
# elsewhere in this codebase for the main structured assessment call) is
# NOT on that list -- only qwen3.5-plus / qwen3.5-flash / open-source
# variants are. Deliberately a different model string for this one call.
MCP_MODEL = "qwen3.5-plus"

MCP_SERVER_PUBLIC_URL = os.getenv("MCP_SERVER_PUBLIC_URL", "")  # e.g. https://your-host/mcp/sse
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")


def _extract_output_text(data: dict) -> str | None:
    """
    Best-effort extraction of the model's text answer from a Responses-API
    JSON body. Written defensively because the exact raw JSON shape wasn't
    confirmed by direct testing (see module docstring) -- tries the
    documented OpenAI-style Responses schema first, falls back to a couple
    of plausible alternates, and returns None (never raises) if nothing
    matches, so a schema surprise degrades to "skip this enrichment"
    rather than crashing analyze_zone.
    """
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    try:
        for item in data.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") in ("output_text", "text"):
                        return content.get("text")
    except (AttributeError, TypeError):
        pass
    if isinstance(data.get("text"), str):
        return data["text"]
    return None


async def get_audit_trend_summary(zone: str, timeout: float = 6.0) -> str | None:
    """
    Asks Qwen (via DashScope's Responses API + our custom MCP server) for
    a natural-language summary of this zone's recent audit trend. Returns
    None on ANY failure -- missing config, network error, unexpected
    response shape -- so this is purely additive: analyze_zone works
    identically whether this succeeds, is skipped, or fails.
    """
    if not MCP_SERVER_PUBLIC_URL or not DASHSCOPE_API_KEY:
        return None  # not configured for this deployment -- skip silently

    mcp_tool = {
        "type": "mcp",
        "server_protocol": "sse",
        "server_label": "floodguard_audit_log",
        "server_description": (
            "FloodGuard's own audit log of past flood risk decisions. "
            "get_zone_audit_summary(zone) gives recent risk-level trend "
            "and decision sources for one zone."
        ),
        "server_url": MCP_SERVER_PUBLIC_URL,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                DASHSCOPE_RESPONSES_URL,
                headers={
                    "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MCP_MODEL,
                    "input": (
                        f"Use get_zone_audit_summary for zone {zone} and summarize "
                        f"the recent risk trend in 1-2 sentences."
                    ),
                    "tools": [mcp_tool],
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        log.warning("Zone %s: MCP audit-trend lookup failed (%s) -- continuing without it", zone, e)
        return None

    text = _extract_output_text(data)
    if text is None:
        log.warning(
            "Zone %s: MCP audit-trend response had an unrecognized shape -- "
            "continuing without it. Raw keys: %s", zone, list(data.keys())
        )
    return text

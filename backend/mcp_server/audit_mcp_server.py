"""
audit_mcp_server.py — a real, custom MCP (Model Context Protocol) server
for FloodGuard AI, exposing the project's own durable audit log
(coordinator/history_store.py) as a tool Qwen can call via DashScope's
Responses API.

Why this is a SEPARATE process from coordinator.py, not a module inside it:
  The `mcp` SDK pulls in a newer starlette than fastapi==0.115.5 (pinned in
  the main backend/requirements.txt) allows -- installing both in one
  environment produces a real version conflict. Running this as its own
  small process with its own requirements (see mcp_server/requirements.txt)
  sidesteps that entirely, and is also just good separation of concerns:
  this is a read-only reporting service over the audit log, not part of
  the risk-assessment critical path.

Transport: SSE only, because that's the only transport DashScope's MCP
integration currently supports (per Alibaba Cloud Model Studio's docs).

IMPORTANT DEPLOYMENT NOTE: DashScope's servers need to actually reach this
over the internet to use it as an MCP tool -- server_url in the coordinator
config must be a real public URL (e.g. the same Alibaba Cloud host the
coordinator runs on, on a different port/path), NOT localhost/127.0.0.1.
This can only be verified once actually deployed with a real DASHSCOPE_API_KEY;
see coordinator/mcp_weather_client.py's module docstring for what has and
hasn't been tested end-to-end.
"""

import os
import sys

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "coordinator"))
import history_store  # noqa: E402 -- reuses the same SQLite audit log coordinator.py writes to

mcp = FastMCP("floodguard-audit-log", host="0.0.0.0", port=int(os.getenv("MCP_SERVER_PORT", "8100")))


@mcp.tool()
async def get_zone_audit_summary(zone: str, lookback: int = 20) -> str:
    """
    Summarizes the most recent durable flood-risk decisions recorded for a
    zone (A, B, or C): counts by risk level, the most recent decision, and
    how many were cloud (Qwen) vs. local edge-agent fallback decisions.
    Use this to understand a zone's recent trend before assessing new
    readings -- e.g. a zone that's had several EMERGENCY decisions in the
    last hour warrants more caution than one with a clean recent history.
    """
    records = await history_store.query_decisions(zone=zone, limit=lookback)
    if not records:
        return f"No audit history recorded yet for zone {zone}."

    counts: dict = {}
    sources: dict = {}
    for r in records:
        risk = r.get("risk_level", "UNKNOWN")
        counts[risk] = counts.get(risk, 0) + 1
        src = r.get("source", "unknown")
        sources[src] = sources.get(src, 0) + 1

    most_recent = records[0]
    lines = [
        f"Zone {zone} -- last {len(records)} recorded decisions:",
        f"  Risk level counts: {counts}",
        f"  Decision sources: {sources}",
        f"  Most recent: {most_recent.get('risk_level')} "
        f"(confidence {most_recent.get('confidence')}, source={most_recent.get('source')}) "
        f"-- \"{most_recent.get('reasoning', '')}\"",
    ]
    return "\n".join(lines)


@mcp.tool()
async def get_full_audit_summary(lookback: int = 50) -> str:
    """Same as get_zone_audit_summary but across all zones -- use this for
    a system-wide trend check rather than a single zone."""
    records = await history_store.query_decisions(zone=None, limit=lookback)
    if not records:
        return "No audit history recorded yet for any zone."

    by_zone: dict = {}
    for r in records:
        by_zone.setdefault(r.get("zone", "?"), []).append(r.get("risk_level", "UNKNOWN"))

    lines = ["System-wide recent decision counts by zone:"]
    for zone, risks in by_zone.items():
        counts = {}
        for r in risks:
            counts[r] = counts.get(r, 0) + 1
        lines.append(f"  Zone {zone}: {counts}")
    return "\n".join(lines)


if __name__ == "__main__":
    port = int(os.getenv("MCP_SERVER_PORT", "8100"))
    print(f"Starting FloodGuard audit-log MCP server (SSE) on port {port}")
    print("This must be reachable at a PUBLIC URL for DashScope to use it as an MCP tool")
    print("(see this file's module docstring).")
    mcp.run(transport="sse")

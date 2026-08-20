#!/usr/bin/env python3
"""A real (if fake-data) MCP stdio server, used to exercise Descant's prober.

The sandbox has no MCP servers attached, and mocking the protocol would prove
nothing — the whole point of the prober is that it speaks real JSON-RPC to a real
process. So this is a genuine MCP server: it implements initialize, tools/list
and tools/call over stdio. Only the weather data is invented.

Deliberately verbose schemas, because that is the thing being measured: this
server costs ~1.5k tokens of context on every turn purely in tool definitions,
which is what makes the skill-conversion trade worth showing.

    python3 fixtures/mcp/weather_server.py     # speaks MCP on stdin/stdout
"""

import json
import sys

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "get_current_weather",
        "description": (
            "Get the current weather conditions for a named location. Returns "
            "temperature, humidity, wind speed and direction, barometric pressure, "
            "and a short human-readable summary of conditions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City name, optionally with region and country, e.g. 'Austin, TX, USA'.",
                },
                "units": {
                    "type": "string",
                    "enum": ["metric", "imperial", "standard"],
                    "default": "metric",
                    "description": "Unit system for temperature and wind speed.",
                },
                "include_air_quality": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include an air-quality index and primary pollutant.",
                },
            },
            "required": ["location"],
        },
    },
    {
        "name": "get_forecast",
        "description": (
            "Get a multi-day weather forecast for a location, with daily high and "
            "low temperatures, precipitation probability, and conditions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City name."},
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 14,
                    "default": 5,
                    "description": "Number of days to forecast, 1 to 14.",
                },
                "units": {
                    "type": "string",
                    "enum": ["metric", "imperial"],
                    "default": "metric",
                },
                "hourly": {
                    "type": "boolean",
                    "default": False,
                    "description": "Return hourly rather than daily granularity.",
                },
            },
            "required": ["location"],
        },
    },
    {
        "name": "search_locations",
        "description": (
            "Search for locations matching a query string, returning canonical "
            "names, coordinates and timezone identifiers for disambiguation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Partial place name."},
                "limit": {"type": "integer", "default": 5, "maximum": 25},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_severe_alerts",
        "description": (
            "List active severe-weather alerts for a region, including event type, "
            "severity, certainty, onset and expiry times, and the issuing office."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "region": {"type": "string", "description": "Region or state code."},
                "min_severity": {
                    "type": "string",
                    "enum": ["minor", "moderate", "severe", "extreme"],
                    "default": "moderate",
                },
            },
            "required": ["region"],
        },
    },
]

FAKE = {
    "get_current_weather": "18°C, partly cloudy, wind 11 km/h NW, humidity 64%, 1014 hPa.",
    "get_forecast": (
        "Mon 19°/9° sunny · Tue 21°/11° sunny · Wed 17°/10° showers (60%) · "
        "Thu 16°/8° cloudy · Fri 20°/10° clear"
    ),
    "search_locations": "Austin, TX, USA (30.27, -97.74, America/Chicago)",
    "get_severe_alerts": "No active alerts above moderate severity.",
}


def respond(msg_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method, msg_id = msg.get("method"), msg.get("id")
        if method == "initialize":
            respond(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "weather", "version": "1.0.0"},
            })
        elif method == "tools/list":
            respond(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = (msg.get("params") or {}).get("name", "")
            text = FAKE.get(name)
            if text is None:
                respond(msg_id, {
                    "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                    "isError": True,
                })
            else:
                respond(msg_id, {"content": [{"type": "text", "text": text}]})
        elif msg_id is not None:
            respond(msg_id, {})


if __name__ == "__main__":
    main()

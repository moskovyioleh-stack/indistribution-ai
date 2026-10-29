"""Owner-authenticated bridge to the existing read-only Yandex MCP server."""
import json
import os

import httpx

UPSTREAM_URL = "https://yandex-webmaster-mcp-production.up.railway.app/mcp"
ALLOWED_TOOLS = frozenset({
    "metrika_counters", "metrika_traffic", "metrika_sources",
    "metrika_goals", "metrika_pages", "webmaster_sites",
    "webmaster_search_queries", "webmaster_indexing", "webmaster_site_summary",
})
DOMAINS = frozenset({
    "indistribution.ru", "heinedasaw.indistribution.ru",
    "powders.indistribution.ru", "molot.website",
})


async def call_yandex(name: str, arguments: dict) -> dict:
    """Forward only named read operations; never expose credentials or upstream errors."""
    if name not in ALLOWED_TOOLS:
        raise ValueError("Yandex tool is not allowed")
    if "domain" in arguments and arguments["domain"] not in DOMAINS:
        raise ValueError("Site is not approved")
    if "counter_id" in arguments:
        counter_id = arguments["counter_id"]
        if isinstance(counter_id, bool) or not isinstance(counter_id, int) or counter_id <= 0:
            raise ValueError("counter_id must be a positive integer")
    # The upstream additionally verifies each counter's site against its allowlist.
    key = (os.environ.get("YANDEX_MCP_ACCESS_KEY", "").strip()
           or os.environ.get("MCP_ACCESS_KEY", "").strip())
    if not key:
        raise RuntimeError("Yandex bridge is not configured: set YANDEX_MCP_ACCESS_KEY")
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            response = await client.post(
                UPSTREAM_URL, json=payload,
                headers={"Authorization": "Bearer " + key,
                         "Accept": "application/json, text/event-stream"},
            )
        if response.status_code != 200:
            raise RuntimeError(f"Yandex MCP returned HTTP {response.status_code}")
        data = response.json()
    except httpx.HTTPError:
        raise RuntimeError("Yandex MCP connection failed or timed out") from None
    except ValueError:
        raise RuntimeError("Yandex MCP returned invalid JSON") from None
    if not isinstance(data, dict) or data.get("id") != 1 or data.get("jsonrpc") != "2.0":
        raise RuntimeError("Yandex MCP returned an invalid JSON-RPC envelope")
    if data.get("error"):
        raise RuntimeError("Yandex MCP rejected the request")
    result = data.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise RuntimeError("Yandex request failed; check upstream service logs")
    content = result.get("content")
    if (not isinstance(content, list) or len(content) != 1
            or not isinstance(content[0], dict) or content[0].get("type") != "text"):
        raise RuntimeError("Yandex MCP returned an unexpected result")
    try:
        parsed = json.loads(content[0]["text"])
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("Yandex MCP returned incomplete or invalid report JSON") from None
    if not isinstance(parsed, dict):
        raise RuntimeError("Yandex MCP returned an unexpected report")
    return parsed


def register_yandex_tools(mcp, owner_only):
    """Use the same owner authorization as existing Tilda and Avito tools."""
    tool = lambda: mcp.tool(auth=owner_only, annotations={"readOnlyHint": True})

    @tool()
    async def metrika_counters() -> dict:
        """List accessible Metrika counters for the four approved company sites."""
        return await call_yandex("metrika_counters", {})

    @tool()
    async def metrika_traffic(counter_id: int, date1: str = "30daysAgo", date2: str = "yesterday") -> dict:
        """Read visits, visitors and pageviews. Dates: YYYY-MM-DD or default last 30 completed days."""
        return await call_yandex("metrika_traffic", dict(counter_id=counter_id, date1=date1, date2=date2))

    @tool()
    async def metrika_sources(counter_id: int, date1: str = "30daysAgo", date2: str = "yesterday") -> dict:
        """Read traffic sources, visits and visitors for an approved counter."""
        return await call_yandex("metrika_sources", dict(counter_id=counter_id, date1=date1, date2=date2))

    @tool()
    async def metrika_pages(counter_id: int, date1: str = "30daysAgo", date2: str = "yesterday") -> dict:
        """Read page URLs and pageview counts for an approved counter."""
        return await call_yandex("metrika_pages", dict(counter_id=counter_id, date1=date1, date2=date2))

    @tool()
    async def metrika_goals(counter_id: int) -> dict:
        """List configured goals. This tool does not return achievements or conversion rates."""
        return await call_yandex("metrika_goals", dict(counter_id=counter_id))

    @tool()
    async def webmaster_sites() -> dict:
        """List accessible Yandex Webmaster sites, read only."""
        return await call_yandex("webmaster_sites", {})

    @tool()
    async def webmaster_search_queries(domain: str) -> dict:
        """Read search queries and search performance for an approved company domain."""
        return await call_yandex("webmaster_search_queries", dict(domain=domain))

    @tool()
    async def webmaster_indexing(domain: str) -> dict:
        """Read indexing samples, events and recrawl quota. Does not submit URLs."""
        return await call_yandex("webmaster_indexing", dict(domain=domain))

    @tool()
    async def webmaster_site_summary(domain: str) -> dict:
        """Read an approved company's site summary from Yandex Webmaster."""
        return await call_yandex("webmaster_site_summary", dict(domain=domain))

"""Restricted, in-memory relay between ChatGPT MCP and a Windows Playwright worker.

One Railway replica only. Queue is not persistent across deploys.
"""
import asyncio
import hmac
import os
import secrets
import time
from starlette.responses import JSONResponse

JOBS = {}
GUARD = asyncio.Lock()
TTL = 1800

def _authorized(request):
    expected = os.getenv("LOCAL_AGENT_TOKEN", "").strip()
    header = request.headers.get("authorization", "")
    return bool(expected and len(expected) >= 32 and header.startswith("Bearer ") and
                hmac.compare_digest(header[7:], expected))

async def _validate_payload(payload, allowed_project, tilda):
    project_id, page_id = str(payload.get("project_id","")), str(payload.get("page_id",""))
    project = await allowed_project(project_id)
    if not project:
        raise ValueError("Project not allowed")
    pages = await tilda("getpageslist", projectid=project_id)
    if page_id not in {str(p.get("id")) for p in pages}:
        raise ValueError("Page not in project")
    return project_id, page_id

# Stable action vocabulary: adding an implementation does not change the MCP schema.
# Status is deliberately explicit; an advertised action is NOT a claim of working Tilda UI automation.
READ_ACTIONS = ("inspect", "blocks", "field_probe")
FUTURE_ACTIONS = {
    "page_tree": "Site structure and navigation hierarchy",
    "block_add": "Insert a Tilda block into a draft",
    "block_remove": "Remove a selected block from a draft",
    "block_move": "Reorder a selected block in a draft",
    "block_duplicate": "Duplicate a selected block",
    "block_settings": "Edit a block's configuration",
    "text_replace": "Replace exact text in an approved record",
    "text_style": "Adjust typography, alignment and color",
    "image_replace": "Replace an image and its alt text",
    "image_crop": "Adjust image crop and focal point",
    "video_set": "Configure video/media",
    "design_tokens": "Configure global colors, fonts, spacing",
    "responsive_settings": "Review/edit desktop, tablet, mobile settings",
    "page_seo": "Update SEO title, description and indexing options",
    "site_settings": "Configure site options",
    "menu_edit": "Edit navigation items",
    "form_settings": "Configure forms and recipients",
    "custom_css": "Review or modify custom CSS safely",
    "preview_draft": "Preview current unpublished changes",
    "save_draft": "Save a verified change without publishing",
    "undo": "Revert a previously applied draft change",
}
DANGEROUS_ACTIONS = ("publish", "deploy", "unpublish", "delete_project", "delete_site")

def action_catalog():
    return {
        "api_version": "1.0",
        "implemented_read_only": list(READ_ACTIONS) + ["result", "capabilities", "plan", "tilda_mcp_get_projects", "tilda_mcp_tools"],
        "planned_not_implemented": FUTURE_ACTIONS,
        "permanently_disabled": list(DANGEROUS_ACTIONS),
        "rules": {
            "publication": "never",
            "writes": "disabled until each operation is separately implemented and tested",
            "scope": "GitHub owner + approved Tilda project/page + local agent allowlist",
            "draft_writes": "require exact target, optimistic source check, and explicit SAVE_DRAFT_ONLY confirmation",
            "no_raw_scripts_or_arbitrary_browser_navigation": True,
        },
    }

async def official_tilda_projects():
    """Query only get_projects using the private Railway secret; never expose it."""
    from fastmcp import Client
    token = os.getenv("TILDA_MCP_TOKEN", "").strip()
    if not token:
        return {"status": "not_configured"}
    try:
        from fastmcp.client.transports import StreamableHttpTransport
        transport = StreamableHttpTransport(
            url="https://tilda.ru/api/mcp/",
            headers={"Authorization": "Bearer " + token},
        )
        async with Client(transport) as client:
            tools = await client.list_tools()
            if "get_projects" not in [t.name for t in tools]:
                return {"status": "missing_tool", "tool_names": [t.name for t in tools][:60]}
            response = await client.call_tool("get_projects", {})
            return {"status": "ok", "result": str(response)[:10000]}
    except Exception:
        return {"status": "upstream_error", "message": "Tilda MCP connection failed; check Railway logs without exposing secrets."}

def register_bridge(mcp, owner_only, allowed_project, tilda):
    @mcp.tool(auth=owner_only)
    async def editor_action(action: str, payload: dict) -> dict:
        """Stable full-site editor interface (schema v1). Call capabilities first to
        discover all domains: content, architecture, design, media, SEO, forms,
        responsive layout, settings, preview, and draft workflow.
        Implemented: inspect, blocks, field_probe, result, capabilities, plan.
        Unimplemented mutations return explicit not_implemented, never fake success.
        Publishing and destructive site actions are forbidden.
        Pass project_id and page_id for page-bound actions; record_id for field_probe.
        """
        if action == "tilda_mcp_tools":
            from fastmcp import Client
            from fastmcp.client.transports import StreamableHttpTransport
            token = os.getenv("TILDA_MCP_TOKEN", "").strip()
            if not token:
                return {"status": "not_configured"}
            try:
                async with Client(StreamableHttpTransport(
                    url="https://tilda.ru/api/mcp/",
                    headers={"Authorization": "Bearer " + token},
                )) as upstream:
                    inventory = await upstream.list_tools()
                return {"status": "ok", "tools": [
                    {"name": tool.name,
                     "description": (tool.description or "")[:450],
                     "input_schema": tool.inputSchema}
                    for tool in inventory
                ]}
            except Exception:
                return {"status": "upstream_error", "message": "Unable to list official Tilda MCP tools"}
        if action == "tilda_mcp_get_projects":
            return await official_tilda_projects()
        if action == "capabilities":
            return action_catalog()
        if action in DANGEROUS_ACTIONS:
            return {"status": "forbidden", "action": action, "reason": "Publication or destructive site action is disabled"}
        if action == "plan":
            operations = payload.get("operations", [])
            if not isinstance(operations, list) or len(operations) > 40:
                return {"error": "operations must be a list of at most 40 items"}
            planned = []
            for item in operations:
                if not isinstance(item, dict):
                    return {"error": "Each operation must be an object"}
                name = str(item.get("action", ""))
                if name not in FUTURE_ACTIONS and name not in READ_ACTIONS:
                    return {"error": "Unknown action in plan", "action": name}
                if name in DANGEROUS_ACTIONS:
                    return {"error": "Forbidden action", "action": name}
                planned.append({"action": name,
                                "implementation": "available_read_only" if name in READ_ACTIONS else "not_implemented",
                                "requires_draft_confirmation": name in FUTURE_ACTIONS})
            return {"status": "plan_only", "no_changes": True, "operations": planned}
        if action in FUTURE_ACTIONS:
            return {"status": "not_implemented", "action": action,
                    "description": FUTURE_ACTIONS[action],
                    "message": "Reserved stable interface only. No Tilda changes were made."}
        if action == "result":
            job_id = str(payload.get("job_id", ""))
            if not job_id:
                return {"error": "job_id is required"}
            async with GUARD:
                j = JOBS.get(job_id)
                return ({k:v for k,v in j.items() if k != "created"}
                        if j else {"error": "Job not found or expired"})
        if action not in ("inspect", "blocks", "field_probe"):
            return {"error": "Unsupported editor action", "supported_actions": ["inspect", "blocks", "field_probe", "result"]}
        if len(os.getenv("LOCAL_AGENT_TOKEN", "").strip()) < 32:
            return {"error": "Local agent is not configured"}
        try:
            project_id, page_id = await _validate_payload(payload, allowed_project, tilda)
        except (ValueError, KeyError) as exc:
            return {"error": str(exc)}
        record_id = str(payload.get("record_id", ""))
        if action == "field_probe":
            if not record_id.isdecimal() or len(record_id) > 15:
                return {"error": "field_probe requires a numeric record_id"}
        job_id = secrets.token_urlsafe(18)
        async with GUARD:
            now = time.time()
            for k in list(JOBS):
                if now - JOBS[k]["created"] > TTL:
                    del JOBS[k]
            JOBS[job_id] = {
                "id": job_id, "created": now, "type": action,
                "project_id": project_id, "page_id": page_id,
                "record_id": record_id if action == "field_probe" else "",
                "status": "queued", "result": None
            }
        return {"job_id": job_id, "status": "queued", "action": action,
                "note": "Windows worker must be running. Read-only."}

    @mcp.tool(auth=owner_only)
    async def local_editor_request(project_id: str, page_id: str) -> dict:
        """Queue a read-only local Playwright inspection for an approved Tilda page."""
        if not os.getenv("LOCAL_AGENT_TOKEN", "").strip():
            return {"error": "Local agent token is not configured on Railway"}
        await _validate_payload({"project_id":project_id,"page_id":page_id}, allowed_project, tilda)
        job_id = secrets.token_urlsafe(18)
        async with GUARD:
            for k in list(JOBS):
                if time.time() - JOBS[k]["created"] > TTL:
                    del JOBS[k]
            JOBS[job_id] = {
                "id":job_id,"created":time.time(),"type":"inspect",
                "project_id":project_id,"page_id":page_id,
                "status":"queued","result":None
            }
        return {"job_id":job_id,"status":"queued","action":"inspect","note":"The Windows worker must be running."}

    @mcp.tool(auth=owner_only)
    async def local_editor_blocks(project_id: str, page_id: str) -> dict:
        """Queue read-only discovery of visible text blocks in the local Tilda editor."""
        if not os.getenv("LOCAL_AGENT_TOKEN", "").strip():
            return {"error": "Local agent is not configured"}
        await _validate_payload({"project_id": project_id, "page_id": page_id}, allowed_project, tilda)
        job_id = secrets.token_urlsafe(18)
        async with GUARD:
            for k in list(JOBS):
                if time.time() - JOBS[k]["created"] > TTL:
                    del JOBS[k]
            JOBS[job_id] = {
                "id":job_id, "created":time.time(), "type":"blocks",
                "project_id":project_id,"page_id":page_id,
                "status":"queued","result":None
            }
        return {"job_id":job_id,"status":"queued","action":"blocks","note":"Read-only; no page changes"}

    @mcp.tool(auth=owner_only)
    async def local_editor_result(job_id: str) -> dict:
        """Check result of an inspection requested via local_editor_request."""
        async with GUARD:
            j = JOBS.get(job_id)
            if not j:
                return {"error": "Job not found or expired"}
            return {k:v for k,v in j.items() if k not in ("created",)}

    @mcp.custom_route("/agent/next",methods=["GET"])
    async def agent_next(request):
        if not _authorized(request):
            return JSONResponse({"error":"unauthorized"},status_code=401)
        async with GUARD:
            for j in JOBS.values():
                if j["status"] == "queued":
                    j["status"] = "running"
                    return JSONResponse({"job":{"id":j["id"],"type":j["type"],
                        "project_id":j["project_id"],"page_id":j["page_id"],
                        "record_id":j.get("record_id","")}})
        return JSONResponse({"job":None})

    @mcp.custom_route("/agent/result",methods=["POST"])
    async def agent_result(request):
        if not _authorized(request):
            return JSONResponse({"error":"unauthorized"},status_code=401)
        data = await request.json()
        job_id = str(data.get("job_id",""))
        result = data.get("result")
        if not isinstance(result,dict):
            return JSONResponse({"error":"invalid result"},status_code=400)
        async with GUARD:
            j = JOBS.get(job_id)
            if not j or j["status"] != "running":
                return JSONResponse({"error":"job not running"},status_code=409)
            j["result"] = {k:v for k,v in result.items() if k in ("status","page_title","editor_open","editable_elements","reason","blocks","block_count","frame_count","fields")}
            j["status"] = "complete"
        return JSONResponse({"ok":True})

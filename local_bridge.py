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

def register_bridge(mcp, owner_only, allowed_project, tilda):
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
                        "project_id":j["project_id"],"page_id":j["page_id"]}})
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
            j["result"] = {k:v for k,v in result.items() if k in ("status","page_title","editor_open","editable_elements","reason","blocks","block_count","frame_count")}
            j["status"] = "complete"
        return JSONResponse({"ok":True})

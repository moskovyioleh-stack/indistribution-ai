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
        "implemented_read_only": list(READ_ACTIONS) + ["result", "capabilities", "plan", "tilda_mcp_get_projects", "tilda_mcp_tools", "tilda_mcp_read", "tilda_page_structure"],
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
        if action == "tilda_save_draft_html":
            # Store a whole responsive page in T123 HTML block on a NEVER-published page.
            import json
            from fastmcp import Client
            from fastmcp.client.transports import StreamableHttpTransport
            if payload.get("confirmation") != "SAVE_DRAFT_ONLY":
                return {"status": "confirmation_required"}
            try:
                pid = int(payload.get("project_id", 0))
                pageid = int(payload.get("page_id", 0))
                rid = int(payload.get("record_id", 0))
                code = payload.get("html", "")
            except (ValueError, TypeError):
                return {"status": "invalid_request"}
            if (pid not in {12112685, 23291206, 14178377, 30763609}
                or min(pageid, rid) <= 0 or not isinstance(code, str)
                or not (100 <= len(code) <= 150000)):
                return {"status": "forbidden"}
            # HTML + CSS allowed; active JS and outside dependencies are excluded.
            lowered = code.lower()
            if any(bad in lowered for bad in (
                "<script", "javascript:", "<iframe", "onerror=", "onload=",
                "<form", "fetch(", "xmlhttprequest", "document.cookie")):
                return {"status": "forbidden", "reason": "Active content disallowed"}
            token = os.getenv("TILDA_MCP_TOKEN", "").strip()
            if not token:
                return {"status": "not_configured"}
            try:
                async with Client(StreamableHttpTransport(
                    url="https://tilda.ru/api/mcp/",
                    headers={"Authorization": "Bearer " + token},
                )) as upstream:
                    pages = await upstream.call_tool("get_pages", {"projectid": pid})
                    listing = json.loads(next(p.text for p in pages.content if getattr(p, "type", "") == "text"))
                    if not any(str(p.get("id")) == str(pageid)
                               and str(p.get("projectid")) == str(pid)
                               and not p.get("published") and not p.get("trash")
                               for p in listing):
                        return {"status": "forbidden", "reason": "Only unpublished pages"}
                    records = await upstream.call_tool("get_page_records",
                        {"projectid": pid, "pageid": pageid})
                    items = json.loads(next(p.text for p in records.content if getattr(p, "type", "") == "text"))
                    if not any(str(p.get("recordid")) == str(rid)
                               and str(p.get("tplid")) == "131" for p in items):
                        return {"status": "forbidden", "reason": "Only T123 HTML block"}
                    current = await upstream.call_tool("edit_record", {
                        "projectid": pid, "pageid": pageid,
                        "recordid": rid, "tab": "content"})
                    existing = json.loads(next(p.text for p in current.content if getattr(p, "type", "") == "text"))
                    old = existing.get("record", {}).get("code", "")
                    if old and payload.get("expected_old_html") != old:
                        return {"status": "precondition_failed", "reason": "Block has changed"}
                    saved = await upstream.call_tool("update_record", {
                        "projectid": pid, "pageid": pageid, "recordid": rid,
                        "tab": "content", "fields": {"code": code},
                        "only_field": "code"})
                    if getattr(saved, "is_error", False):
                        return {"status": "upstream_error", "stage": "save"}
                    check = await upstream.call_tool("edit_record", {
                        "projectid": pid, "pageid": pageid,
                        "recordid": rid, "tab": "content"})
                    check_data = json.loads(next(p.text for p in check.content if getattr(p, "type", "") == "text"))
                    actual = check_data.get("record", {}).get("code", "")
                    return {"status": "saved_verified" if actual == code else "verification_failed",
                            "page_id": pageid, "record_id": rid,
                            "saved_length": len(actual), "published": False}
            except Exception:
                return {"status": "upstream_error", "stage": "draft_html"}
        if action == "tilda_template_search":
            from fastmcp import Client
            from fastmcp.client.transports import StreamableHttpTransport
            query = str(payload.get("query", ""))[:60]
            token = os.getenv("TILDA_MCP_TOKEN", "").strip()
            if not token:
                return {"status": "not_configured"}
            try:
                async with Client(StreamableHttpTransport(
                    url="https://tilda.ru/api/mcp/",
                    headers={"Authorization": "Bearer " + token},
                )) as upstream:
                    result = await upstream.call_tool("tpls_list", {"query": query})
                    if getattr(result, "is_error", False):
                        return {"status": "upstream_error"}
                    parts = [p.text for p in result.content if getattr(p, "type", "") == "text"]
                    return {"status": "ok", "templates": parts[:1]}
            except Exception:
                return {"status": "upstream_error"}

        if action == "tilda_remove_draft_block":
            import json
            from fastmcp import Client
            from fastmcp.client.transports import StreamableHttpTransport
            if payload.get("confirmation") != "SAVE_DRAFT_ONLY":
                return {"status": "confirmation_required"}
            try:
                pid = int(payload.get("project_id", 0))
                pageid = int(payload.get("page_id", 0))
                recordid = int(payload.get("record_id", 0))
            except (TypeError, ValueError):
                return {"status": "invalid_request"}
            if pid not in {12112685, 23291206, 14178377, 30763609}:
                return {"status": "forbidden"}
            token = os.getenv("TILDA_MCP_TOKEN", "").strip()
            if not token:
                return {"status": "not_configured"}
            try:
                async with Client(StreamableHttpTransport(
                    url="https://tilda.ru/api/mcp/",
                    headers={"Authorization": "Bearer " + token},
                )) as upstream:
                    pages = await upstream.call_tool("get_pages", {"projectid": pid})
                    listing = json.loads(next(p.text for p in pages.content if getattr(p, "type", "") == "text"))
                    if not any(str(p.get("id")) == str(pageid) and
                               str(p.get("projectid")) == str(pid) and
                               not p.get("published") and not p.get("trash")
                               for p in listing):
                        return {"status": "forbidden", "reason": "Not an unpublished page"}
                    records = await upstream.call_tool("get_page_records", {"projectid": pid, "pageid": pageid})
                    items = json.loads(next(p.text for p in records.content if getattr(p, "type", "") == "text"))
                    if not any(str(p.get("recordid")) == str(recordid) for p in items):
                        return {"status": "forbidden", "reason": "Record not found"}
                    result = await upstream.call_tool("delete_record",
                        {"projectid": pid, "pageid": pageid, "recordid": recordid})
                    if getattr(result, "is_error", False):
                        return {"status": "upstream_error"}
                    return {"status": "removed_from_draft", "response": [
                        p.text for p in result.content if getattr(p, "type", "") == "text"]}
            except Exception:
                return {"status": "upstream_error"}
        if action == "tilda_create_draft_page":
            # Explicitly creates an unpublished page, never publish.
            import json
            from fastmcp import Client
            from fastmcp.client.transports import StreamableHttpTransport
            if payload.get("confirmation") != "SAVE_DRAFT_ONLY":
                return {"status": "confirmation_required"}
            try:
                pid = int(payload.get("project_id", 0))
            except (ValueError, TypeError):
                return {"status": "invalid_request"}
            if pid not in {12112685, 23291206, 14178377, 30763609}:
                return {"status": "forbidden"}
            token = os.getenv("TILDA_MCP_TOKEN", "").strip()
            if not token:
                return {"status": "not_configured"}
            try:
                async with Client(StreamableHttpTransport(
                    url="https://tilda.ru/api/mcp/",
                    headers={"Authorization": "Bearer " + token},
                )) as upstream:
                    result = await upstream.call_tool("add_blank_page", {"projectid": pid})
                    if getattr(result, "is_error", False):
                        return {"status": "upstream_error", "stage": "create_page"}
                    parts = [p.text for p in result.content if getattr(p, "type", "") == "text"]
                    return {"status": "created_unpublished", "project_id": pid,
                            "response": parts[:2], "published": False}
            except Exception:
                return {"status": "upstream_error", "stage": "create_page"}

        if action == "tilda_add_draft_block":
            import json
            from fastmcp import Client
            from fastmcp.client.transports import StreamableHttpTransport
            if payload.get("confirmation") != "SAVE_DRAFT_ONLY":
                return {"status": "confirmation_required"}
            try:
                pid = int(payload.get("project_id", 0))
                pageid = int(payload.get("page_id", 0))
                tplid = int(payload.get("tplid", 0))
            except (ValueError, TypeError):
                return {"status": "invalid_request"}
            if pid not in {12112685, 23291206, 14178377, 30763609} or min(pageid, tplid) <= 0:
                return {"status": "forbidden"}
            token = os.getenv("TILDA_MCP_TOKEN", "").strip()
            if not token:
                return {"status": "not_configured"}
            try:
                async with Client(StreamableHttpTransport(
                    url="https://tilda.ru/api/mcp/",
                    headers={"Authorization": "Bearer " + token},
                )) as upstream:
                    pages = await upstream.call_tool("get_pages", {"projectid": pid})
                    if getattr(pages, "is_error", False):
                        return {"status": "upstream_error", "stage": "pages"}
                    listings = [p.text for p in pages.content if getattr(p, "type", "") == "text"]
                    data = json.loads(listings[0]) if len(listings) == 1 else []
                    if not isinstance(data, list) or not any(
                        isinstance(p, dict) and str(p.get("id")) == str(pageid)
                        and str(p.get("projectid")) == str(pid)
                        and not p.get("trash") and not p.get("published")
                        for p in data
                    ):
                        return {"status": "forbidden", "reason": "Only unpublished pages may receive new blocks"}
                    result = await upstream.call_tool("add_record", {
                        "projectid": pid, "pageid": pageid, "tplid": tplid})
                    if getattr(result, "is_error", False):
                        return {"status": "upstream_error", "stage": "add_record"}
                    parts = [p.text for p in result.content if getattr(p, "type", "") == "text"]
                    return {"status": "block_added", "response": parts[:2], "published": False}
            except Exception:
                return {"status": "upstream_error", "stage": "add_record"}
        if action == "tilda_call":
            # Stable, scoped gateway for official Tilda MCP, without one Railway
            # function per upstream tool. Mutations require explicit draft consent.
            import json
            import re
            from fastmcp import Client
            from fastmcp.client.transports import StreamableHttpTransport
            method = str(payload.get("tool", ""))
            args = payload.get("arguments", {})
            if not isinstance(args, dict) or not isinstance(method, str):
                return {"status": "invalid_request"}
            project_ids = {12112685, 23291206, 14178377, 30763609}
            write_methods = {
                "update_record", "update_page_settings",
            }
            read_methods = {
                "get_projects", "get_pages", "get_page_records", "edit_record",
                "get_page_settings", "get_record_html", "get_head_code",
                "get_project_css_code", "get_robots_txt", "get_favicons",
                "get_https_www_redirect_settings", "get_form_integrations",
            }
            # No dynamic write whitelist: Tilda may add publishing methods later.
            if method not in (read_methods | write_methods):
                return {"status": "forbidden", "reason": "Method not allowlisted", "tool": method}
            is_write = method in write_methods
            if is_write and payload.get("confirmation") != "SAVE_DRAFT_ONLY":
                return {"status": "confirmation_required", "confirmation": "SAVE_DRAFT_ONLY"}
            if method != "get_projects":
                try:
                    pid = int(args["projectid"])
                except (KeyError, ValueError, TypeError):
                    return {"status": "invalid_request", "reason": "projectid required"}
                if pid not in project_ids:
                    return {"status": "forbidden", "reason": "Project not approved"}
            page_methods = {
                "get_page_records", "edit_record", "get_page_settings",
                "get_record_html", "update_record", "add_record",
                "update_page_settings", "update_page_head_code",
            }
            if method in page_methods:
                try:
                    pageid = int(args["pageid"])
                except (KeyError, ValueError, TypeError):
                    return {"status": "invalid_request", "reason": "pageid required"}
                if pageid <= 0:
                    return {"status": "invalid_request"}
            if is_write:
                if method == "update_record":
                    if (args.get("tab") not in ("content", "settings")
                        or not isinstance(args.get("fields"), dict)
                        or not isinstance(args.get("recordid"), int)):
                        return {"status": "invalid_request", "reason": "recordid, fields and tab required"}
                    if not args.get("only_field") or set(args["fields"]) != {args["only_field"]}:
                        return {"status": "invalid_request", "reason": "Single-field updates only"}
                if method == "update_page_settings":
                    safe_settings = {"pageid", "projectid", "date", "meta_title",
                                     "meta_descr", "meta_keywords", "link_canonical",
                                     "fb_title", "fb_descr"}
                    if set(args) - safe_settings:
                        return {"status": "forbidden", "reason": "Only safe SEO fields are permitted"}
            token = os.getenv("TILDA_MCP_TOKEN", "").strip()
            if not token:
                return {"status": "not_configured"}
            try:
                async with Client(StreamableHttpTransport(
                    url="https://tilda.ru/api/mcp/",
                    headers={"Authorization": "Bearer " + token},
                )) as upstream:
                    if method in page_methods:
                        pages_result = await upstream.call_tool("get_pages", {"projectid": pid})
                        if getattr(pages_result, "is_error", False):
                            return {"status": "upstream_error", "stage": "get_pages"}
                        parts = [p.text for p in pages_result.content
                                 if getattr(p, "type", "") == "text"]
                        page_list = json.loads(parts[0]) if len(parts) == 1 else []
                        if not isinstance(page_list, list) or not any(
                            isinstance(p, dict) and str(p.get("id")) == str(pageid)
                            and str(p.get("projectid")) == str(pid) and not p.get("trash")
                            for p in page_list
                        ):
                            return {"status": "forbidden", "reason": "Page not approved"}
                    if method == "update_record":
                        records = await upstream.call_tool(
                            "get_page_records", {"projectid": pid, "pageid": pageid})
                        pieces = [p.text for p in records.content
                                  if getattr(p, "type", "") == "text"]
                        record_list = json.loads(pieces[0]) if len(pieces) == 1 else []
                        if not isinstance(record_list, list) or not any(
                            isinstance(r, dict)
                            and str(r.get("recordid")) == str(args["recordid"])
                            for r in record_list
                        ):
                            return {"status": "forbidden", "reason": "Record not approved"}
                        if not payload.get("expected_old_value"):
                            return {"status": "precondition_required",
                                    "reason": "Provide expected_old_value for a targeted field"}
                        current = await upstream.call_tool("edit_record", {
                            "projectid": pid, "pageid": pageid,
                            "recordid": args["recordid"], "tab": args["tab"]})
                        # Avoid mutating on an unknown editor response structure.
                        current_texts = [p.text for p in current.content if getattr(p, "type", "") == "text"]
                        try:
                            parsed = json.loads(current_texts[0])
                        except (ValueError, IndexError, TypeError):
                            return {"status": "precondition_failed", "reason": "Unable to parse current block fields"}
                        expected = payload["expected_old_value"]
                        field = args["only_field"]
                        record = parsed.get("record") if isinstance(parsed, dict) else None
                        if (not isinstance(record, dict) or field not in record
                            or record[field] != expected):
                            return {"status": "precondition_failed",
                                    "reason": "Expected field value differs; no changes"}
                        # For list-backed cards, require preserved structure and a
                        # single textual change. A list field is the entire array.
                        if field == "list":
                            try:
                                before = json.loads(expected)
                                after = json.loads(args["fields"][field])
                            except (ValueError, TypeError):
                                return {"status": "invalid_request", "reason": "Invalid list JSON"}
                            if (not isinstance(before, list) or not isinstance(after, list)
                                or len(before) != len(after)):
                                return {"status": "forbidden", "reason": "List structure changed"}
                            deltas = 0
                            for before_item, after_item in zip(before, after):
                                if (not isinstance(before_item, dict)
                                    or not isinstance(after_item, dict)
                                    or set(before_item) != set(after_item)):
                                    return {"status": "forbidden", "reason": "List keys changed"}
                                for item_key in before_item:
                                    if before_item[item_key] != after_item[item_key]:
                                        if item_key not in {"li_title", "li_descr", "li_img", "li_imgalt"}:
                                            return {"status": "forbidden", "reason": "Protected list metadata changed"}
                                        deltas += 1
                            if deltas != 1:
                                return {"status": "forbidden", "reason": "Exactly one card field may change"}
                    result = await upstream.call_tool(method, args)
                    if getattr(result, "is_error", False):
                        return {"status": "upstream_error", "stage": method}
                    parts = [p.text for p in result.content if getattr(p, "type", "") == "text"]
                    # Do not label a successful API response as published.
                    return {"status": "upstream_accepted", "tool": method,
                            "published_by_agent": False, "response": parts[:4]}
            except Exception:
                return {"status": "upstream_error", "message": "Official Tilda MCP request failed"}
        if action == "tilda_record_fields":
            import json
            from fastmcp import Client
            from fastmcp.client.transports import StreamableHttpTransport
            try:
                pid = int(payload.get("project_id", 0))
                pageid = int(payload.get("page_id", 0))
                rid = int(payload.get("record_id", 0))
            except (ValueError, TypeError):
                return {"status": "invalid_request"}
            if pid not in {12112685, 23291206, 14178377, 30763609} or min(pageid, rid) <= 0:
                return {"status": "forbidden"}
            token = os.getenv("TILDA_MCP_TOKEN", "").strip()
            if not token:
                return {"status": "not_configured"}
            try:
                async with Client(StreamableHttpTransport(
                    url="https://tilda.ru/api/mcp/",
                    headers={"Authorization": "Bearer " + token},
                )) as upstream:
                    pages = await upstream.call_tool("get_pages", {"projectid": pid})
                    page_data = json.loads(next(p.text for p in pages.content if getattr(p, "type", "") == "text"))
                    if not isinstance(page_data, list) or not any(
                        isinstance(p, dict) and str(p.get("id")) == str(pageid)
                        and str(p.get("projectid")) == str(pid) and not p.get("trash")
                        for p in page_data
                    ):
                        return {"status": "forbidden", "reason": "Page not approved"}
                    records = await upstream.call_tool("get_page_records", {"projectid": pid, "pageid": pageid})
                    record_data = json.loads(next(p.text for p in records.content if getattr(p, "type", "") == "text"))
                    if not isinstance(record_data, list) or not any(
                        isinstance(r, dict) and str(r.get("recordid")) == str(rid)
                        for r in record_data
                    ):
                        return {"status": "forbidden", "reason": "Record not on page"}
                    result = await upstream.call_tool("edit_record", {
                        "projectid": pid, "pageid": pageid, "recordid": rid, "tab": "content"
                    })
                    if getattr(result, "is_error", False):
                        return {"status": "upstream_error", "stage": "edit_record"}
                    contents = [p.text for p in result.content if getattr(p, "type", "") == "text"]
                    return {"status": "ok", "record_id": rid, "content": contents[:3]}
            except Exception:
                return {"status": "upstream_error", "message": "Unable to retrieve record fields"}
        if action == "tilda_page_structure":
            import json
            from fastmcp import Client
            from fastmcp.client.transports import StreamableHttpTransport
            try:
                project_id = int(payload.get("project_id", 0))
                page_id = int(payload.get("page_id", 0))
            except (ValueError, TypeError):
                return {"status": "invalid_request"}
            if project_id not in {12112685, 23291206, 14178377, 30763609} or page_id <= 0:
                return {"status": "forbidden"}
            token = os.getenv("TILDA_MCP_TOKEN", "").strip()
            if not token:
                return {"status": "not_configured"}
            try:
                async with Client(StreamableHttpTransport(
                    url="https://tilda.ru/api/mcp/",
                    headers={"Authorization": "Bearer " + token},
                )) as upstream:
                    pages = await upstream.call_tool("get_pages", {"projectid": project_id})
                    if getattr(pages, "is_error", False):
                        return {"status": "upstream_error", "stage": "pages"}
                    parts = [p.text for p in pages.content if getattr(p, "type", "") == "text"]
                    listing = json.loads(parts[0]) if len(parts) == 1 else []
                    if not isinstance(listing, list) or not any(
                        isinstance(p, dict) and str(p.get("id")) == str(page_id)
                        and str(p.get("projectid")) == str(project_id) and not p.get("trash")
                        for p in listing
                    ):
                        return {"status": "forbidden", "reason": "Page not in project"}
                    records = await upstream.call_tool(
                        "get_page_records", {"projectid": project_id, "pageid": page_id}
                    )
                    if getattr(records, "is_error", False):
                        return {"status": "upstream_error", "stage": "records"}
                    items = [p.text for p in records.content if getattr(p, "type", "") == "text"]
                    data = json.loads(items[0]) if len(items) == 1 else None
                    if not isinstance(data, (list, dict)):
                        return {"status": "invalid_upstream_response"}
                    return {"status": "ok", "project_id": project_id, "page_id": page_id,
                            "records": data}
            except Exception:
                return {"status": "upstream_error", "message": "Cannot read page structure"}
        if action == "tilda_mcp_read":
            # Fixed method allowlist: do not forward arbitrary MCP tool names.
            tool_name = str(payload.get("tool", ""))
            required = {
                "get_projects": (),
                "get_pages": ("projectid",),
                "get_page_records": ("projectid", "pageid"),
                "get_page_settings": ("projectid", "pageid"),
            }
            if tool_name not in required:
                return {"status": "forbidden", "message": "Read method is not allowlisted"}
            args = payload.get("arguments", {})
            if not isinstance(args, dict):
                return {"error": "arguments must be an object"}
            if any(k not in args for k in required[tool_name]):
                return {"error": "Required argument missing"}
            permitted = {12112685, 23291206, 14178377, 30763609}
            if tool_name != "get_projects":
                if "projectid" in args:
                    try:
                        if int(args["projectid"]) not in permitted:
                            return {"status": "forbidden", "message": "Project not allowlisted"}
                    except (ValueError, TypeError):
                        return {"error": "Invalid projectid"}
                elif tool_name == "get_page_records":
                    # Page ownership cannot be verified without a project ID.
                    return {"error": "For get_page_records include projectid for access validation"}
            if tool_name == "get_page_records":
                try:
                    project_id = int(args["projectid"])
                    page_id = int(args["pageid"])
                except (ValueError, TypeError, KeyError):
                    return {"error": "Invalid page or project ID"}
                if project_id not in permitted:
                    return {"status": "forbidden"}
                # Resolve page membership via the upstream get_pages before reading.
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
                    if tool_name == "get_page_records":
                        import json
                        pages_result = await upstream.call_tool("get_pages", {"projectid": project_id})
                        if getattr(pages_result, "is_error", False):
                            return {"status": "upstream_error", "message": "Failed to verify page membership"}
                        page_parts = [p.text for p in pages_result.content if getattr(p, "type", "") == "text"]
                        try:
                            page_items = json.loads(page_parts[0]) if len(page_parts) == 1 else []
                        except (ValueError, TypeError):
                            return {"status": "validation_error", "message": "Cannot parse Tilda page list"}
                        if not isinstance(page_items, list) or not any(
                            isinstance(item, dict)
                            and str(item.get("id")) == str(page_id)
                            and str(item.get("projectid")) == str(project_id)
                            and not item.get("trash")
                            for item in page_items
                        ):
                            return {"status": "forbidden", "message": "Page is not an active page in approved project"}
                    upstream_args = {k: v for k, v in args.items() if k in required[tool_name]}
                    response = await upstream.call_tool(tool_name, upstream_args)
                    return {"status": "ok", "tool": tool_name, "result": str(response)[:14000]}
            except Exception:
                return {"status": "upstream_error", "message": "Official Tilda MCP read request failed"}
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

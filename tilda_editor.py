"""Conservative browser editor for an explicitly approved Tilda page.

Requires TILDA_STORAGE_STATE_B64 (base64 JSON Playwright storage state)
supplied securely as a Railway secret. No login credentials are accepted.
Selectors are deliberately conservative: if the editor differs, stop.
"""
import asyncio
import base64
import json
import os
from contextlib import asynccontextmanager
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

EDIT_DOMAIN = "https://tilda.cc"
# UI selectors may require adjustment after inspecting the actual editor.
SAVE_LABELS = ("Сохранить", "Save")
WRITE_LOCK = asyncio.Lock()


def _storage_state():
    encoded = os.getenv("TILDA_STORAGE_STATE_B64", "").strip()
    if not encoded:
        raise RuntimeError("Tilda browser session is not configured: set TILDA_STORAGE_STATE_B64 as a private Railway variable")
    try:
        state = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Invalid browser storage state format") from exc
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise RuntimeError("Invalid Playwright storage state")
    return state


@asynccontextmanager
async def editor_page(page_id: str):
    # Tilda editor uses its own first-party host. Never accept arbitrary URLs.
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = await browser.new_context(storage_state=_storage_state(), viewport={"width": 1440, "height": 900})
            try:
                page = await context.new_page()
                await page.goto(f"{EDIT_DOMAIN}/page/?pageid={page_id}", wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(1500)
                if not page.url.startswith(EDIT_DOMAIN + "/"):
                    raise RuntimeError("Tilda editor redirected to an unexpected host")
                if "/login" in page.url or "signin" in page.url:
                    raise RuntimeError("Tilda session expired. Reauthorize securely and update TILDA_STORAGE_STATE_B64")
                yield page
            finally:
                await context.close()
        finally:
            await browser.close()


def register_editor_tools(mcp, owner_only, allowed_project, tilda):
    async def validate(project_id: str, page_id: str):
        project = await allowed_project(project_id)
        if not project:
            raise ValueError("Project not approved")
        pages = await tilda("getpageslist", projectid=project_id)
        if str(page_id) not in {str(p.get("id")) for p in pages}:
            raise ValueError("Page not part of approved project")
        return project

    @mcp.tool(auth=owner_only)
    async def editor_inspect(project_id: str, page_id: str) -> dict:
        """Inspect an approved page inside the authenticated Tilda editor (no changes)."""
        project = await validate(project_id, page_id)
        async with WRITE_LOCK:
            async with editor_page(page_id) as page:
                return {
                    "project": project["domain"], "page_id": page_id,
                    "editor_url": page.url,
                    "visible_title": await page.title(),
                    "editable_elements": await page.locator('[contenteditable="true"]:visible').count(),
                    "note": "Read-only browser inspection. Tilda login state must be configured."
                }

    @mcp.tool(auth=owner_only)
    async def editor_replace_text_draft(project_id: str, page_id: str, old_text: str, new_text: str, confirmation: str) -> dict:
        """Replace one exact text element in Tilda editor and save ONLY an unpublished draft.

        Requires confirmation='SAVE_DRAFT_ONLY'. Stops if text/editor selectors are ambiguous.
        NEVER publishes, deploys or opens a publish action.
        """
        if confirmation != "SAVE_DRAFT_ONLY":
            raise ValueError("Explicit SAVE_DRAFT_ONLY confirmation required")
        if not old_text.strip() or not new_text.strip() or old_text == new_text:
            raise ValueError("Provide two distinct nonempty text values")
        if len(old_text) > 1500 or len(new_text) > 1500:
            raise ValueError("Text exceeds single-block editing limits")
        project = await validate(project_id, page_id)
        async with WRITE_LOCK:
            async with editor_page(page_id) as page:
                target = page.get_by_text(old_text, exact=True)
                count = await target.count()
                if count != 1:
                    return {"status": "stopped", "reason": f"Exact text matches: {count}; expected exactly one", "changed": False}
                await target.dblclick(timeout=10000)
                editable = page.locator('[contenteditable="true"]:visible')
                if await editable.count() != 1:
                    return {"status": "stopped", "reason": "No unique active rich-text editor after double click", "changed": False}
                await editable.fill(new_text)
                save_candidates = []
                for label in SAVE_LABELS:
                    save_candidates.extend([page.get_by_role("button", name=label, exact=True)])
                matches = [loc for loc in save_candidates if await loc.count() == 1 and await loc.is_visible()]
                if len(matches) != 1:
                    return {"status": "requires_manual_save", "changed_in_editor": True, "reason": "Save button not uniquely identified. Publishing was not attempted; unsaved changes may be discarded when browser closes."}
                await matches[0].click(timeout=10000)
                await page.wait_for_timeout(1000)
                return {
                    "status": "save_clicked_unverified", "project": project["domain"], "page_id": page_id,
                    "old_text": old_text, "new_text": new_text,
                    "published": False,
                    "warning": "UI save was clicked. Verify draft in the Tilda editor; no publish action was taken."
                }

"""Windows-side worker. Uses existing local Playwright session; never uploads cookies.

Set LOCAL_AGENT_URL and LOCAL_AGENT_TOKEN in your own Windows environment.
Only inspects allowlisted Tilda editor pages; never writes/publishes.
"""
import asyncio
import os
from pathlib import Path
import httpx
from playwright.async_api import async_playwright

BASE = os.getenv("LOCAL_AGENT_URL", "").rstrip("/")
TOKEN = os.getenv("LOCAL_AGENT_TOKEN", "")
SESSION = Path.home() / "tilda-session.json"
APPROVED = {
    ("14178377", "296498609"),  # Initial molot.website page.
}

async def inspect(job):
    pair = (str(job["project_id"]), str(job["page_id"]))
    if pair not in APPROVED:
        return {"status":"rejected","reason":"Page not locally approved"}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        try:
            context = await browser.new_context(storage_state=str(SESSION))
            try:
                page = await context.new_page()
                await page.goto(
                    f"https://tilda.ru/page/?pageid={pair[1]}&projectid={pair[0]}",
                    wait_until="domcontentloaded", timeout=45000
                )
                if not page.url.startswith("https://tilda.ru/page/"):
                    return {"status":"error","reason":"Editor redirected; authorize again locally", "editor_open":False}
                return {
                    "status":"ok", "page_title":(await page.title())[:200],
                    "editor_open":True,
                    "editable_elements":await page.locator('[contenteditable="true"]:visible').count()
                }
            finally:
                await context.close()
        finally:
            await browser.close()

async def main():
    if not BASE.startswith("https://") or len(TOKEN) < 32 or not SESSION.is_file():
        raise SystemExit("Set LOCAL_AGENT_URL (https), LOCAL_AGENT_TOKEN (32+ chars), and create ~/tilda-session.json")
    headers = {"Authorization":f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(headers=headers,timeout=60) as client:
        print("Tilda worker started. Read-only inspection only. Stop with Ctrl+C.")
        while True:
            try:
                r = await client.get(BASE + "/agent/next")
                r.raise_for_status()
                job = r.json().get("job")
                if job:
                    try:
                        result = await inspect(job) if job["type"] == "inspect" else {"status":"rejected","reason":"Unsupported command"}
                    except Exception as exc:
                        result = {"status":"error","reason":type(exc).__name__}
                    response = await client.post(BASE + "/agent/result",json={"job_id":job["id"],"result":result})
                    response.raise_for_status()
                    print("Processed job",job["id"],result["status"])
            except httpx.HTTPStatusError as exc:
                print("Server error HTTP",exc.response.status_code)
            except (httpx.ConnectError,httpx.TimeoutException):
                print("Network unavailable; retrying")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())

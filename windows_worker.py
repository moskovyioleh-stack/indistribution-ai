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
                await page.wait_for_timeout(2000)
                blocks = []
                if job["type"] == "blocks":
                    # Inspect Tilda editor record wrappers, not only rendered h1/p tags.
                    # Return bounded metadata; never send HTML, links, cookies or form values.
                    for frame_index, frame in enumerate(page.frames[:12]):
                        try:
                            found = await frame.evaluate("""() => {
                              const nodes = [...document.querySelectorAll(
                                '[data-record-type],.record,.t-rec,[id^="rec"]'
                              )];
                              const out = [];
                              const seen = new Set();
                              for (const el of nodes) {
                                if (out.length >= 30) break;
                                const rect = el.getBoundingClientRect();
                                if (rect.width < 1 || rect.height < 1) continue;
                                const id = (el.getAttribute('data-record-id') || el.id || '').slice(0,80);
                                const type = (el.getAttribute('data-record-type') ||
                                  el.getAttribute('data-block-type') || '').slice(0,80);
                                const raw = (el.innerText || '').replace(/\\s+/g,' ').trim();
                                const text = raw.slice(0,180);
                                if (!id && !type && !text) continue;
                                const key = id + ':' + type + ':' + text.slice(0,60);
                                if (seen.has(key)) continue;
                                seen.add(key);
                                out.push({record_id:id,record_type:type,
                                  tag:el.tagName.toLowerCase(),text});
                              }
                              return out;
                            }""")
                            for item in found:
                                if len(blocks) >= 30:
                                    break
                                blocks.append({"frame":frame_index, **item})
                        except Exception:
                            continue
                    # Counts are diagnostics, not page data. No cookie, URL query, HTML or secrets.
                    counts = {}
                    selectors = {
                        "body": "body", "record_nodes": "[data-record-type],.record,.t-rec",
                        "tilda_blocks": "[id^=rec],[class*=block],[class*=record]",
                        "iframes": "iframe", "editable": "[contenteditable=true]",
                        "buttons": "button", "text_nodes": "h1,h2,h3,p",
                        "loading": "[class*=loading],[class*=spinner]"
                    }
                    for label, selector in selectors.items():
                        try:
                            counts[label] = await page.locator(selector).count()
                        except Exception:
                            counts[label] = -1
                    try:
                        body_text_length = len(await page.locator("body").inner_text(timeout=3000))
                    except Exception:
                        body_text_length = -1
                    diagnostic = ("dom_counts=" + ",".join(f"{k}:{v}" for k,v in counts.items())
                                  + f"; body_text_length={body_text_length}"
                                  + f"; browser_frames={len(page.frames)}")
                    return {"status":"ok","page_title":(await page.title())[:200],
                            "editor_open":True,"frame_count":len(page.frames),
                            "block_count":len(blocks),"blocks":blocks,
                            "reason":diagnostic[:700]}
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
                        result = await inspect(job) if job["type"] in ("inspect","blocks") else {"status":"rejected","reason":"Unsupported command"}
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

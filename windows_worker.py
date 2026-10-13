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
                if job["type"] == "field_probe":
                    record_id = str(job.get("record_id",""))
                    if not record_id.isdecimal() or len(record_id) > 15:
                        return {"status":"rejected","reason":"Invalid record_id"}
                    # Tilda keeps content fields in its block editor, not in the preview.
                    # Opening the uniquely identified "Content" control is navigation only;
                    # no Save, Publish, form submission or text mutation is permitted here.
                    record = page.locator("#record" + record_id)
                    if await record.count() != 1:
                        record = page.locator("#rec" + record_id)
                    content_buttons = record.get_by_text("Контент", exact=True)
                    if await content_buttons.count() != 1:
                        content_buttons = record.get_by_text("Content", exact=True)
                    content_panel = "not_opened"
                    if await content_buttons.count() == 1 and await content_buttons.is_visible():
                        await content_buttons.click(timeout=5000)
                        await page.wait_for_timeout(700)
                        content_panel = "opened"
                    # Inspect only field metadata; never capture credential fields or full HTML.
                    panel_fields = await page.evaluate("""() => {
                      return [...document.querySelectorAll('input,textarea,[contenteditable="true"]')]
                        .filter(el => { const r=el.getBoundingClientRect();
                          return r.width>0 && r.height>0 && el.type !== 'password'; })
                        .slice(0,45).map(el => ({
                          tag:el.tagName.toLowerCase(),
                          name:(el.getAttribute('name')||'').slice(0,70),
                          placeholder:(el.getAttribute('placeholder')||'').slice(0,80),
                          type:(el.getAttribute('type')||'').slice(0,25)
                        }));
                    }""")
                    # No clicks other than opening the content panel.
                    probe = await page.evaluate("""(recordId) => {
                      const parent = document.getElementById('record' + recordId)
                        || document.getElementById('rec' + recordId);
                      if (!parent) return {error:'Record not found'};
                      const nodes = [...parent.querySelectorAll(
                        '[data-field],[data-tilda-field],[contenteditable],input,textarea,button,[role=button]'
                      )].slice(0,60);
                      const fields = nodes.map(el => {
                        const rect = el.getBoundingClientRect();
                        return {
                          tag:el.tagName.toLowerCase(),
                          field:(el.getAttribute('data-field')||el.getAttribute('data-tilda-field')||'').slice(0,65),
                          role:(el.getAttribute('role')||'').slice(0,30),
                          editable:el.getAttribute('contenteditable') === 'true',
                          visible:rect.width > 0 && rect.height > 0,
                          label:el.tagName.toLowerCase() === 'button'
                            ? (el.innerText||'').replace(/\\s+/g,' ').trim().slice(0,70) : ''
                        };
                      });
                      return {fields,record_text:(parent.innerText||'').replace(/\\s+/g,' ').trim().slice(0,240)};
                    }""", record_id)
                    if "error" in probe:
                        return {"status":"error","reason":probe["error"]}
                    return {"status":"ok","page_title":(await page.title())[:200],
                            "editor_open":True,"fields":probe["fields"][:60] + panel_fields[:40],
                            "reason":"Content panel " + content_panel + "; record text: " + probe["record_text"][:240]}
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
                        result = await inspect(job) if job["type"] in ("inspect","blocks","field_probe") else {"status":"rejected","reason":"Unsupported command"}
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

"""Read-only browser verification for the four approved public Tilda sites."""
import asyncio
import re
from urllib.parse import quote
from fastmcp.utilities.types import Image
from playwright.async_api import async_playwright

VALID_ALIAS = re.compile(r"^[a-zA-Z0-9_./-]{1,120}$")
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile": {"width": 390, "height": 844},
}
ALLOWED_HOSTS = {
    "indistribution.ru", "heinedasaw.indistribution.ru",
    "powders.indistribution.ru", "molot.website",
}


async def _resolve_url(project_id, page_id, allowed_project, tilda):
    project = await allowed_project(str(project_id))
    if not project or project["domain"] not in ALLOWED_HOSTS:
        raise ValueError("Project not approved")
    pages = await tilda("getpageslist", projectid=str(project_id))
    match = next((item for item in pages if str(item.get("id")) == str(page_id)), None)
    if match is None:
        raise ValueError("Page does not belong to project")
    # Tilda's home page can have empty alias while its internal url points
    # to page<ID>.html. If it is the only published content page, use root.
    live = [p for p in pages if p.get("published") and not p.get("trash")
            and str(p.get("id")) != "73934705"]
    if (not match.get("alias") and len(live) == 1
        and str(live[0].get("id")) == str(page_id)):
        return "https://" + project["domain"] + "/"
    alias = str(match.get("alias") or "").strip("/")
    if not alias:
        alias = str(match.get("url") or "").strip("/")
    if not alias:
        # Tilda stores the homepage with an empty alias. Resolve it only
        # when it is the sole active published content page for the project.
        live = [p for p in pages if p.get("published") and not p.get("trash")
                and str(p.get("id")) != "73934705"]
        if len(live) == 1 and str(live[0].get("id")) == str(page_id):
            return "https://" + project["domain"] + "/"
        raise ValueError("Homepage cannot be resolved unambiguously")
    if not VALID_ALIAS.fullmatch(alias) or alias.startswith("/") or ".." in alias:
        raise ValueError("Invalid page path")
    return "https://" + project["domain"] + "/" + alias


async def _browse(url, device, screenshot=False):
    if device not in VIEWPORTS:
        return {"error": "device must be desktop or mobile"}
    viewport = VIEWPORTS[device]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = await browser.new_context(
                viewport=viewport, device_scale_factor=1,
                is_mobile=(device == "mobile"),
                ignore_https_errors=False,
            )
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)[:250]))
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=22000)
                await page.wait_for_timeout(1000)
                # Trigger lazy image loading by scrolling through the document.
                await page.evaluate("""async () => {
                    const height = document.documentElement.scrollHeight;
                    for (let y = 0; y < height; y += 650) {
                        window.scrollTo(0, y);
                        await new Promise(resolve => setTimeout(resolve, 120));
                    }
                    window.scrollTo(0, 0);
                    await Promise.all(Array.from(document.images).map(img =>
                        img.complete ? Promise.resolve() :
                        Promise.race([
                            new Promise(resolve => { img.addEventListener('load', resolve, {once:true});
                                                     img.addEventListener('error', resolve, {once:true}); }),
                            new Promise(resolve => setTimeout(resolve, 3000)),
                        ])
                    ));
                }""")
            except Exception as exc:
                return {"status": "failed", "url": url, "device": device,
                        "error": type(exc).__name__, "detail": str(exc)[:350]}
            if screenshot:
                data = await page.screenshot(type="jpeg", quality=65,
                                             full_page=True, animations="disabled")
                return Image(data=data, format="jpeg")
            metrics = await page.evaluate("""() => ({
              title: document.title,
              faviconLinks: Array.from(document.querySelectorAll('link[rel~="icon"]')).map(n=>({href:n.href,type:n.type,sizes:n.sizes.value})),
              h1: Array.from(document.querySelectorAll('h1')).map(n=>n.innerText).slice(0,8),
              imageCount: document.images.length,
              brokenImages: Array.from(document.images).filter(i => !i.complete || i.naturalWidth === 0).slice(0,16).map(i => i.currentSrc || i.src),
              horizontalOverflow: document.documentElement.scrollWidth > innerWidth+2,
              documentWidth: document.documentElement.scrollWidth,
              viewportWidth: innerWidth,
              contentLength: document.body.innerText.length,
            })""")
            icon_checks = []
            for icon in metrics.get("faviconLinks", [])[:8]:
                try:
                    icon_response = await context.request.get(icon["href"], timeout=10000)
                    icon_checks.append({"url":icon["href"],"status":icon_response.status,
                                        "contentType":icon_response.headers.get("content-type","")})
                except Exception as exc:
                    icon_checks.append({"url":icon["href"],"error":str(exc)[:180]})
            return {"status": "ok", "url": url, "device": device,
                    "http_status": response.status if response else None,
                    "final_url": page.url, "metrics": metrics,
                    "javascript_errors": errors[:10], "faviconRequests": icon_checks}
        finally:
            await browser.close()


def register_browser_tools(mcp, owner_only, allowed_project, tilda):
    @mcp.tool(auth=owner_only)
    async def browser_audit(project_id: str, page_id: str, device: str = "desktop") -> dict:
        """Open an approved public Tilda page in real Chromium, inspect errors, images and responsive overflow. Does not edit or publish."""
        try:
            url = await _resolve_url(project_id, page_id, allowed_project, tilda)
            return await asyncio.wait_for(_browse(url, device), timeout=45)
        except ValueError as exc:
            return {"status": "forbidden", "reason": str(exc)}
        except Exception as exc:
            return {"status": "failed", "error": type(exc).__name__}

    @mcp.tool(auth=owner_only)
    async def browser_screenshot(project_id: str, page_id: str, device: str = "desktop"):
        """Return full-page JPEG screenshot from real Chromium for an approved public Tilda page. Never changes site."""
        try:
            url = await _resolve_url(project_id, page_id, allowed_project, tilda)
            return await asyncio.wait_for(_browse(url, device, screenshot=True), timeout=55)
        except ValueError as exc:
            return {"status": "forbidden", "reason": str(exc)}
        except Exception as exc:
            return {"status": "failed", "error": type(exc).__name__}

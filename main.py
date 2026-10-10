"""Read-only MCP server for four Tilda Business projects."""
import os
import hmac
import re
import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

DOMAINS = {"indistribution.ru", "heinedasaw.indistribution.ru", "powders.indistribution.ru", "molot.website"}
API = "https://api.tildacdn.info/v1"
mcp = FastMCP("INDISTRIBUTION AI", stateless_http=True, json_response=True)

async def tilda(method, **kwargs):
    if method not in {"getprojectslist", "getprojectinfo", "getpageslist", "getpagefull"}:
        raise ValueError("Read-only operations only")
    public = os.environ.get("TILDA_PUBLIC_KEY", "").strip()
    secret = os.environ.get("TILDA_SECRET_KEY", "").strip()
    if not public or not secret:
        raise RuntimeError("Tilda credentials are not configured")
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{API}/{method}/", params={"publickey": public, "secretkey": secret, **kwargs})
        response.raise_for_status()
        data = response.json()
    if data.get("status") != "FOUND":
        raise ValueError(f"Tilda API status: {data.get('status')}")
    return data.get("result")

async def projects():
    items = await tilda("getprojectslist")
    output = []
    for item in items:
        info = await tilda("getprojectinfo", projectid=item["id"])
        domain = str(info.get("customdomain") or "").lower().strip().removeprefix("https://").removeprefix("http://").split("/")[0]
        if domain in DOMAINS:
            output.append({"id": str(item["id"]), "name": info.get("title"), "domain": domain})
    return output

async def allowed_project(project_id):
    return next((p for p in await projects() if p["id"] == str(project_id)), None)

@mcp.tool()
async def list_projects() -> dict:
    """List the approved four Tilda sites and project IDs."""
    return {"projects": await projects()}

@mcp.tool()
async def list_pages(project_id: str) -> dict:
    """List pages in an approved project (project_id from list_projects)."""
    project = await allowed_project(project_id)
    if not project:
        return {"error": "Project not approved"}
    pages = await tilda("getpageslist", projectid=project_id)
    return {"project": project, "pages": [{"id": str(p.get("id")), "title": p.get("title"), "alias": p.get("alias"), "published": p.get("published")} for p in pages]}

@mcp.tool()
async def audit_page(project_id: str, page_id: str) -> dict:
    """Read HTML of one project page; return SEO observations. Never changes pages."""
    project = await allowed_project(project_id)
    if not project:
        return {"error": "Project not approved"}
    pages = await tilda("getpageslist", projectid=project_id)
    if str(page_id) not in {str(page.get("id")) for page in pages}:
        return {"error": "Page is not part of the approved project"}
    record = await tilda("getpagefull", pageid=page_id)
    html = record.get("html") or ""
    soup = BeautifulSoup(html, "html.parser")
    desc = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    canonical = soup.find("link", attrs={"rel": "canonical"})
    images = soup.find_all("img")
    return {
        "domain": project["domain"], "project_id": project_id, "page_id": page_id,
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "meta_description": desc.get("content", "") if desc else "",
        "canonical": canonical.get("href", "") if canonical else "",
        "h1": [h.get_text(" ", strip=True) for h in soup.find_all("h1")][:20],
        "h2": [h.get_text(" ", strip=True) for h in soup.find_all("h2")][:20],
        "images": len(images),
        "images_without_alt": sum(not i.get("alt", "").strip() for i in images),
        "note": "Static HTML audit only; browser rendering and indexation require other checks"
    }

@mcp.custom_route("/healthz", methods=["GET"])
async def health(request):
    return JSONResponse({"status": "ok", "service": "indistribution-ai", "read_only": True})

class BearerGuard:
    def __init__(self, app):
        self.app = app
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
            token = os.getenv("MCP_ACCESS_TOKEN", "")
            headers = dict(scope.get("headers", []))
            provided = headers.get(b"authorization", b"").decode("utf8", "ignore")
            if len(token) < 32 or not hmac.compare_digest(provided, "Bearer " + token):
                await JSONResponse({"error": "Unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)

app = BearerGuard(mcp.streamable_http_app())

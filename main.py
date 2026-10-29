"""GitHub OAuth, owner-only, read-only MCP connector to four Tilda websites."""
import os
import re
import httpx
from bs4 import BeautifulSoup
from fastmcp import FastMCP
from fastmcp.server.auth import AuthContext
from fastmcp.server.auth.providers.github import GitHubProvider
from starlette.responses import JSONResponse
import avito_api
from yandex_tools import register_yandex_tools

DOMAINS = {"indistribution.ru", "heinedasaw.indistribution.ru", "powders.indistribution.ru", "molot.website"}
API = "https://api.tildacdn.info/v1"
BASE_URL = os.environ.get("OAUTH_BASE_URL", "").strip().rstrip("/")
CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
ALLOWED_LOGIN = os.environ.get("GITHUB_ALLOWED_LOGIN", "moskovyioleh-stack").strip().lower()

if not (BASE_URL.startswith("https://") and CLIENT_ID and CLIENT_SECRET and ALLOWED_LOGIN):
    raise RuntimeError("GitHub OAuth configuration is missing; refusing to start an unprotected MCP server")

auth = GitHubProvider(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, base_url=BASE_URL)
mcp = FastMCP("INDISTRIBUTION AI", auth=auth)

def owner_only(ctx: AuthContext) -> bool:
    """Deny data access if GitHub identity cannot be verified as the expected owner."""
    if not ctx.token:
        return False
    claims = ctx.token.claims or {}
    return str(claims.get("login", "")).lower() == ALLOWED_LOGIN

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

@mcp.tool(auth=owner_only)
async def list_projects() -> dict:
    """List the approved four Tilda sites and project IDs."""
    return {"projects": await projects()}

@mcp.tool(auth=owner_only)
async def list_pages(project_id: str) -> dict:
    """List pages in an approved project (project_id from list_projects)."""
    project = await allowed_project(project_id)
    if not project:
        return {"error": "Project not approved"}
    pages = await tilda("getpageslist", projectid=project_id)
    return {"project": project, "pages": [{"id": str(p.get("id")), "title": p.get("title"), "alias": p.get("alias"), "published": p.get("published")} for p in pages]}

@mcp.tool(auth=owner_only)
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



@mcp.tool(auth=owner_only)
async def avito_status() -> dict:
    """Check Avito API authentication and return connected account."""
    return {"configured": True, "account": await avito_api.get_account()}

@mcp.tool(auth=owner_only)
async def avito_get_account() -> dict:
    """Return connected Avito account."""
    return await avito_api.get_account()

@mcp.tool(auth=owner_only)
async def avito_list_items(status: str = "active", page: int = 1, per_page: int = 100) -> dict:
    """List Avito advertisements."""
    return await avito_api.list_items(status=status, page=page, per_page=per_page)

@mcp.tool(auth=owner_only)
async def avito_list_chats(unread_only: bool = False, limit: int = 100, offset: int = 0) -> dict:
    """List Avito Messenger chats."""
    account = await avito_api.get_account()
    return await avito_api.list_chats(int(account["id"]), unread_only=unread_only, limit=limit, offset=offset)

@mcp.tool(auth=owner_only)
async def avito_list_messages(chat_id: str, limit: int = 100, offset: int = 0) -> dict:
    """List messages in an Avito chat."""
    account = await avito_api.get_account()
    return await avito_api.list_messages(int(account["id"]), chat_id, limit=limit, offset=offset)

@mcp.tool(auth=owner_only)
async def avito_send_message(chat_id: str, text: str) -> dict:
    """Send a text message in Avito Messenger."""
    if not text.strip():
        raise ValueError("Message text is empty")
    account = await avito_api.get_account()
    return await avito_api.send_message(int(account["id"]), chat_id, text)

@mcp.tool(auth=owner_only)
async def avito_mark_chat_read(chat_id: str) -> dict:
    """Mark an Avito chat as read."""
    account = await avito_api.get_account()
    return await avito_api.mark_chat_read(int(account["id"]), chat_id)

@mcp.tool(auth=owner_only)
async def avito_update_price(item_id: int, price: int) -> dict:
    """Update Avito item price."""
    if price < 0:
        raise ValueError("Price must be non-negative")
    account = await avito_api.get_account()
    return await avito_api.update_price(int(account["id"]), item_id, price)

@mcp.tool(auth=owner_only)
async def avito_get_stats(item_ids: list[int], date_from: str, date_to: str) -> dict:
    """Get Avito listing statistics."""
    account = await avito_api.get_account()
    return await avito_api.get_stats(int(account["id"]), item_ids, date_from, date_to)



@mcp.tool(auth=owner_only)
async def avito_get_item(item_id: int) -> dict:
    """Get one Avito advertisement."""
    account = await avito_api.get_account()
    return await avito_api.get_item(int(account["id"]), item_id)

@mcp.tool(auth=owner_only)
async def avito_archive_item(item_id: int) -> dict:
    """Archive an Avito advertisement."""
    account = await avito_api.get_account()
    return await avito_api.archive_item(int(account["id"]), item_id)

@mcp.tool(auth=owner_only)
async def avito_get_shallow_stats() -> dict:
    """Get lightweight Avito statistics."""
    account = await avito_api.get_account()
    return await avito_api.get_shallow_stats(int(account["id"]))

@mcp.tool(auth=owner_only)
async def avito_autoload_reports(page: int = 1, per_page: int = 100) -> dict:
    """Get Avito autoload reports."""
    account = await avito_api.get_account()
    return await avito_api.autoload_reports(int(account["id"]), page, per_page)

@mcp.tool(auth=owner_only)
async def avito_autoload_last_report() -> dict:
    """Get latest Avito autoload report."""
    account = await avito_api.get_account()
    return await avito_api.autoload_last_report(int(account["id"]))

@mcp.tool(auth=owner_only)
async def avito_generate_powder_listing(
    alloy: str,
    fraction: str,
    application: str,
    manufacturer: str = "TIJO",
    in_stock: bool = True,
) -> dict:
    """Generate an Avito draft for a powder product. Does not publish."""
    stock = "В наличии." if in_stock else "Под заказ."
    title = f"Металлический порошок {alloy} {fraction}".strip()[:50].rstrip()
    description = (
        f"{stock} Металлический порошок {alloy}, фракция {fraction}. "
        f"Применение: {application}. Производитель: {manufacturer}. "
        "Компания «Промдистрибуция» поставляет порошки для 3D-печати, "
        "лазерной наплавки, HVOF/HVAF, плазменного и газопламенного напыления, "
        "MIM/MBJ, HIP, порошковой металлургии и пайки. "
        "Напишите марку, фракцию, технологию и объем - подготовим технические данные и предложение."
    )
    return {"title": title, "description": description, "draft_only": True}

@mcp.tool(auth=owner_only)
async def avito_optimize_listing(title: str, description: str, application: str = "") -> dict:
    """Analyze a powder listing and suggest conversion improvements."""
    text = f"{title} {description}".lower()
    issues = []
    if "в наличии" not in text:
        issues.append("Добавить наличие в первые строки")
    if "мкм" not in text and "фракц" not in text:
        issues.append("Добавить фракционный состав")
    if not any(k in text for k in ["slm","dmls","hvof","hvaf","наплав","напыл","mim","mbj","hip","припо"]):
        issues.append("Указать технологию применения")
    if "напишите" not in text and "сообщен" not in text:
        issues.append("Добавить конкретный CTA")
    return {
        "score": max(0, 100 - 20 * len(issues)),
        "issues": issues,
        "recommended_cta": "Напишите марку материала, фракцию, технологию применения и требуемый объем.",
        "application": application,
    }

@mcp.custom_route("/avito-health", methods=["GET", "POST"])
async def avito_health(request):
    """Read-only Avito connectivity check; returns no credentials."""
    try:
        account = await avito_api.get_account()
        print("AVITO_ACCOUNT_OK", flush=True)
        return JSONResponse({"status": "ok", "avito": True, "account_id": account.get("id")})
    except Exception as exc:
        return JSONResponse({"status": "error", "avito": False, "detail": str(exc)[:500]}, status_code=502)

@mcp.custom_route("/healthz", methods=["GET"])
async def health(request):
    return JSONResponse({"status": "ok", "service": "indistribution-ai", "tilda_read_only": True, "avito_configured": bool(os.getenv("AVITO_CLIENT_ID") and os.getenv("AVITO_CLIENT_SECRET"))})

register_yandex_tools(mcp, owner_only)

app = mcp.http_app(path="/mcp", stateless_http=True)


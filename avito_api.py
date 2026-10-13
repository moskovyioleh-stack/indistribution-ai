"""Avito API client for INDISTRIBUTION AI."""
import os
import httpx

AVITO_API = "https://api.avito.ru"

def _credentials():
    client_id = os.getenv("AVITO_CLIENT_ID", "").strip()
    client_secret = os.getenv("AVITO_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("Avito credentials are not configured")
    return client_id, client_secret

async def get_token() -> str:
    client_id, client_secret = _credentials()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{AVITO_API}/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        data = r.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Avito token response missing access_token")
    return token

async def request(method: str, path: str, *, params=None, json=None):
    token = await get_token()
    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.request(
            method,
            f"{AVITO_API}{path}",
            params=params,
            json=json,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Avito API {r.status_code}: {r.text[:1200]}")
    if not r.content:
        return {"ok": True}
    return r.json()

async def get_account():
    return await request("GET", "/core/v1/accounts/self")

async def list_items(status: str = "active", page: int = 1, per_page: int = 100):
    return await request("GET", "/core/v1/items", params={"status": status, "page": page, "per_page": per_page})

async def list_chats(user_id: int, unread_only: bool = False, limit: int = 100, offset: int = 0):
    return await request("GET", f"/messenger/v2/accounts/{user_id}/chats", params={
        "unread_only": str(unread_only).lower(), "limit": limit, "offset": offset
    })

async def list_messages(user_id: int, chat_id: str, limit: int = 100, offset: int = 0):
    return await request("GET", f"/messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/", params={
        "limit": limit, "offset": offset
    })

async def send_message(user_id: int, chat_id: str, text: str):
    return await request("POST", f"/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages", json={
        "message": {"text": text}
    })

async def mark_chat_read(user_id: int, chat_id: str):
    return await request("POST", f"/messenger/v1/accounts/{user_id}/chats/{chat_id}/read")

async def update_price(user_id: int, item_id: int, price: int):
    return await request("PUT", f"/core/v1/accounts/{user_id}/items/{item_id}/price", json={"price": price})

async def get_stats(user_id: int, item_ids: list[int], date_from: str, date_to: str):
    return await request("POST", f"/stats/v1/accounts/{user_id}/items", json={
        "dateFrom": date_from, "dateTo": date_to, "itemIds": item_ids
    })

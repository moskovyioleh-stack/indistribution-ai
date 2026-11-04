"""Scoped Tilda Feeds gateway. Page publication/deletion remain disabled."""
import asyncio
import hashlib
import html
import json
import re
from tilda_contracts import CheckedClient, ContractError, result_data, exact, safe_failure_metadata

FEED_READS = {
    "feeds_get_feeds", "feeds_get_feed", "feeds_get_parts", "feeds_get_posts",
    "feeds_get_post", "feeds_get_feed_pages", "feeds_get_project_pages",
}
FEED_WRITES = {"feeds_add_post", "feeds_update_post", "feeds_publish_post"}
FEED_LOCK = asyncio.Lock()
UID = re.compile(r"^[A-Za-z0-9]{1,40}$")
FLAGS = {"nosearch", "nofollow", "disableamp", "disablerss", "pinned", "disablecomments"}
PATCH_FIELDS = {"title", "descr", "text", "image", "imagealt", "thumb", "thumbalt",
                "seo_title", "seo_descr", "fb_title", "fb_descr", "postalias"} | FLAGS
SAVE_FIELDS = PATCH_FIELDS | {
    "mediatype", "mediadata", "authorname", "authorurl", "authorimg", "directlink",
    "directtarget", "disablecomments", "priority", "pinned", "date", "sort", "parts",
    "seo_keywords", "fb_image", "nosearch", "nofollow", "disableamp", "disablerss",
}


def version(post):
    return hashlib.sha256(json.dumps(post, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def active(post):
    if "active" not in post or post["active"] not in ("", "y", "n", None):
        raise ContractError("Unknown publication state")
    return post["active"] == "y"


def post_object(data, uid):
    if not isinstance(data, dict) or "title" not in data or "active" not in data:
        raise ContractError("Unknown post response")
    # Accept only the requested post; do not infer scope from arbitrary nested data.
    for key in ("uid", "postuid"):
        if key in data and str(data[key]) != uid:
            raise ContractError("Post identity mismatch")
    return data


def canonical(post, key):
    value = post.get(key)
    if key in FLAGS and value in ("n", "", None):
        return ""
    if key in {"text", "descr"} and isinstance(value, str):
        return html.unescape(value)
    if key == "postalias" and value is None:
        return ""
    return value


def update_arguments(pid, uid, post, patch, custom_patch=None):
    custom_patch = {} if custom_patch is None else custom_patch
    if not isinstance(custom_patch, dict):
        raise ContractError("Custom field patch must be an object")
    if not isinstance(patch, dict) or (not patch and not custom_patch) or set(patch) - PATCH_FIELDS:
        raise ContractError("Unsupported patch fields")
    if any(not isinstance(value, str) for value in patch.values()):
        raise ContractError("Text patch values required")
    if any(patch[key] not in {"", "n", "y"} for key in FLAGS & patch.keys()):
        raise ContractError("Flags require n, y or empty")
    if "title" in patch and not patch["title"].strip():
        raise ContractError("Nonempty title required")
    if any(key not in post for key in patch):
        raise ContractError("Patch field absent from editor response")
    if "text" in patch:
        blocks = json.loads(patch["text"])
        if not isinstance(blocks, list) or any(not isinstance(b, dict) or not b.get("ty") for b in blocks):
            raise ContractError("Explicit editor block array required")
    args = {key: canonical(post, key) for key in SAVE_FIELDS
            if key in post and not (key == "sort" and post[key] is None)}
    # The standard editor's sections may be an array rather than write-form CSV.
    if isinstance(args.get("parts"), list):
        if any(not str(x).isdigit() for x in args["parts"]):
            raise ContractError("Unknown post sections")
        args["parts"] = ",".join(str(x) for x in args["parts"])
    args.update({key: canonical(patch, key) for key in patch})
    args.update(projectid=pid, postuid=uid, visibility="")
    fields = post.get("fields") or []
    if fields:
        custom = {}
        kinds = {}
        for field in fields:
            lid, kind = str(field.get("lid", "")), field.get("li_type")
            if kind == "title":
                continue
            if (not lid.isdigit() or kind not in {"in", "ta", "formated", "image", "blockText"}
                    or lid not in post):
                raise ContractError("Unsupported or missing custom field")
            kinds[lid] = kind
            custom[lid] = post[lid]
            if kind == "blockText":
                # Rendered HTML is never sent as the block editor's backing JSON.
                backing = patch.get("text", canonical(post, "text"))
                if backing:
                    blocks = json.loads(backing)
                    if not isinstance(blocks, list):
                        raise ContractError("Missing blockText backing JSON")
                custom[lid] = backing or "[]"
        if set(custom_patch) - set(custom):
            raise ContractError("Unknown custom field lid")
        for lid, value in custom_patch.items():
            kind = kinds[lid]
            if kind in {"in", "ta", "formated"} and not isinstance(value, str):
                raise ContractError("Custom text value must be a string")
            if kind == "image" and (not isinstance(value, dict) or set(value) != {"url", "alt"}
                    or any(not isinstance(x, str) for x in value.values())):
                raise ContractError("Image requires URL and alt")
            if kind == "blockText" and value != patch.get("text"):
                raise ContractError("blockText must match explicit text JSON")
            custom[lid] = value
        args["custom_fields"] = custom
    elif custom_patch:
        raise ContractError("Standard feed has no custom fields")
    return args


async def dispatch(upstream, method, payload, approved):
    """Validate scope before every read; never retry a write or toggle."""
    args = payload.get("arguments", {})
    if not isinstance(args, dict) or type(args.get("projectid")) is not int:
        return {"status": "invalid_request", "reason": "Integer projectid required"}
    pid = args["projectid"]
    if pid not in approved or method not in FEED_READS | FEED_WRITES:
        return {"status": "forbidden", "reason": "Project or feed method not approved"}
    if method in FEED_WRITES:
        required = "PUBLISH_FEED_POST" if method == "feeds_publish_post" else "SAVE_DRAFT_ONLY"
        if payload.get("confirmation") != required:
            return {"status": "confirmation_required", "confirmation": required}
    postuid = args.get("postuid")
    if method in {"feeds_get_post", "feeds_update_post", "feeds_publish_post"}:
        if not isinstance(postuid, str) or not UID.fullmatch(postuid):
            return {"status": "invalid_request", "reason": "Valid postuid required"}
    if method == "feeds_add_post" and (
        set(args) != {"projectid", "feeduid", "title"}
        or not isinstance(args["feeduid"], str) or not UID.fullmatch(args["feeduid"])
        or not isinstance(args["title"], str) or not 1 <= len(args["title"].strip()) <= 200
    ):
        return {"status": "invalid_request", "reason": "projectid, feeduid and title required"}
    if method in {"feeds_update_post", "feeds_publish_post"} and set(args) != {"projectid", "postuid"}:
        return {"status": "invalid_request", "reason": "Use payload.patch and expected_post_version"}
    if method in {"feeds_update_post", "feeds_publish_post"} and not isinstance(payload.get("expected_post_version"), str):
        return {"status": "precondition_failed", "reason": "Read post and provide expected_post_version"}
    write_attempted = False
    try:
        projects = result_data(await upstream.call_tool("get_projects", {}))
        if not isinstance(projects, list) or not any(isinstance(p, dict) and str(p.get("id")) == str(pid) for p in projects):
            return {"status": "forbidden", "reason": "Project unavailable to Tilda account"}
        if method in FEED_READS:
            data = result_data(await upstream.call_tool(method, args))
            out = {"status": "ok", "tool": method, "project_id": pid, "data": data}
            if method == "feeds_get_post":
                post = post_object(data, postuid)
                out["post_version"] = version(post)
            return out
        if method == "feeds_add_post":
            # get_feed verifies that the caller's explicit feed belongs to this project.
            result_data(await upstream.call_tool("feeds_get_feed", {"projectid": pid, "feeduid": args["feeduid"]}))
            write_attempted = True
            created = result_data(await upstream.call_tool(method, args))
            uid = created.get("uid") if isinstance(created, dict) else None
            if not isinstance(uid, str) or not UID.fullmatch(uid):
                return {"status": "write_unconfirmed", "data": created, "write_performed": True}
            checked = post_object(result_data(await upstream.call_tool("feeds_get_post", {
                "projectid": pid, "postuid": uid})), uid)
            return {"status": "draft_created_verified" if not active(checked)
                    and checked["title"] == args["title"] else "verification_mismatch",
                    "project_id": pid, "postuid": uid, "post_version": version(checked),
                    "published": active(checked), "write_performed": True}
        async with FEED_LOCK:
            post = post_object(result_data(await upstream.call_tool("feeds_get_post", {
                "projectid": pid, "postuid": postuid})), postuid)
            if version(post) != payload["expected_post_version"]:
                return {"status": "precondition_failed", "reason": "Post changed; read again"}
            if active(post):
                if method == "feeds_publish_post":
                    return {"status": "published_verified", "changed": False, "write_performed": False}
                return {"status": "forbidden", "reason": "Draft editing requires an unpublished post"}
            if method == "feeds_publish_post":
                write_attempted = True
                result_data(await upstream.call_tool("feeds_toggle_post_visibility", {
                    "projectid": pid, "postuid": postuid}))
                checked = post_object(result_data(await upstream.call_tool("feeds_get_post", {
                    "projectid": pid, "postuid": postuid})), postuid)
                return {"status": "published_verified" if active(checked) else "verification_mismatch",
                        "project_id": pid, "postuid": postuid, "post_version": version(checked),
                        "published": active(checked), "write_performed": True}
            patch = payload.get("patch", {})
            custom_patch = payload.get("custom_fields_patch", {})
            save_args = update_arguments(pid, postuid, post, patch, custom_patch)
            if (all(exact(canonical(post, k), canonical(patch, k)) for k in patch)
                    and all(exact(post.get(k), v) for k, v in custom_patch.items())):
                return {"status": "draft_verified", "changed": False, "write_performed": False,
                        "post_version": version(post), "published": False}
            write_attempted = True
            result_data(await upstream.call_tool("feeds_update_post", save_args))
            checked = post_object(result_data(await upstream.call_tool("feeds_get_post", {
                "projectid": pid, "postuid": postuid})), postuid)
            mismatch = [k for k, v in save_args.items() if k in SAVE_FIELDS
                        and not exact(canonical(checked, k), v)]
            for lid, value in save_args.get("custom_fields", {}).items():
                field = next(f for f in post["fields"] if str(f.get("lid")) == lid)
                if field.get("li_type") != "blockText" and not exact(checked.get(lid), value):
                    mismatch.append(lid)
            ok = not active(checked) and not mismatch
            return {"status": "draft_verified" if ok else "verification_mismatch",
                    "project_id": pid, "postuid": postuid, "post_version": version(checked),
                    "published": active(checked), "write_performed": True,
                    "mismatch_fields": mismatch}
    except Exception as exc:
        # Classify only fixed phrases; never return upstream text or credentials.
        message = str(exc).lower()
        category = "unclassified"
        for phrases, label in [
            (("not enabled", "not connected", "not activated", "не подключ", "не активир"), "module_not_enabled"),
            (("permission", "access denied", "forbidden", "not allowed", "доступ"), "access_denied"),
            (("not found", "no feeds", "не найден"), "not_found"),
            (("too many", "rate limit", "429"), "rate_limit"),
        ]:
            if any(phrase in message for phrase in phrases):
                category = label
                break
        return {"status": "write_unconfirmed" if write_attempted else "precondition_failed",
                "write_attempted": write_attempted, "stage": getattr(upstream, "last_tool", None),
                "error_category": category, **safe_failure_metadata(exc),
                "reason": "Feed operation stopped; do not retry writes before reading current state"}


async def feed_call(method, payload, approved):
    import os
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    token = os.getenv("TILDA_MCP_TOKEN", "").strip()
    if not token:
        return {"status": "not_configured"}
    try:
        async with Client(StreamableHttpTransport(url="https://tilda.ru/api/mcp/",
                headers={"Authorization": "Bearer " + token})) as raw:
            return await dispatch(CheckedClient(raw), method, payload, approved)
    except Exception as exc:
        return {"status": "upstream_error", **safe_failure_metadata(exc)}

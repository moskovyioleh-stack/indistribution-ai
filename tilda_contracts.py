"""Strict MCP result and editable-field contracts. No network or credentials."""
import json


class ContractError(ValueError):
    pass


def exact(a, b):
    """Python considers True == 1: optimistic checks must not."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(exact(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(exact(x, y) for x, y in zip(a, b))
    return a == b


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _error(data):
    if not isinstance(data, dict):
        return False
    return (data.get("isError") is True or data.get("is_error") is True
            or bool(data.get("error")) or bool(data.get("error_code"))
            or data.get("success") is False or data.get("ok") is False
            or str(data.get("status", "")).lower() in {
                "error", "failed", "failure", "forbidden", "invalid_request",
                "not_found", "upstream_error", "precondition_failed"}
            or any(_error(data.get(k)) for k in ("result", "response", "data")))


def result_data(result):
    """Inspect structured and text error envelopes; never print their contents."""
    if _error(result) or _get(result, "is_error") or _get(result, "isError"):
        raise ContractError("MCP returned an error")
    structured = _get(result, "structured_content")
    if structured is None:
        structured = _get(result, "structuredContent")
    if _error(structured):
        raise ContractError("MCP structured error")
    parts = []
    for item in _get(result, "content", []) or []:
        if _get(item, "type") == "text":
            text = _get(item, "text", "")
            if text.lstrip().lower().startswith(("error calling tool", "error:", "traceback")):
                raise ContractError("MCP text error")
            try:
                value = json.loads(text)
            except (ValueError, TypeError):
                value = text
            if _error(value):
                raise ContractError("MCP JSON error")
            parts.append(value)
    if structured is not None:
        return structured
    if len(parts) != 1:
        raise ContractError("Ambiguous MCP response")
    return parts[0]


class CheckedClient:
    """Existing route checks now also cover structured MCP failures."""
    def __init__(self, client):
        self.client = client

    async def call_tool(self, name, arguments):
        result = await self.client.call_tool(name, arguments)
        result_data(result)
        return result

    async def list_tools(self):
        return await self.client.list_tools()


def field_contract(parsed, target, recordid=None):
    if not isinstance(parsed, dict):
        raise ContractError("Unknown editor response")
    record, fields = parsed.get("record"), parsed.get("fields")
    if not isinstance(record, dict) or not isinstance(fields, dict):
        raise ContractError("Missing editable schema")
    candidates = []
    for key, control in fields.items():
        if not isinstance(key, str) or not isinstance(control, dict):
            raise ContractError("Malformed editable schema")
        send = control.get("send_key", key)
        if not isinstance(send, str) or not send:
            raise ContractError("Invalid send_key")
        if target in (key, send):
            candidates.append((key, send, control))
    if len(candidates) != 1:
        raise ContractError("Unknown or ambiguous editable field")
    key, send, control = candidates[0]
    if (not isinstance(control.get("type"), str) or not control["type"]
            or control.get("type") == "hidden" or control.get("readonly") or control.get("read_only")):
        raise ContractError("Field is not editable")
    storage = key
    # Retain the two existing form aliases, not a general arbitrary-key alias.
    if target == "forminputs" and recordid in (4796839901, 4871274701):
        storage = "list"
    values = [record[k] for k in dict.fromkeys((storage, send)) if k in record]
    if len(values) > 1 and not exact(values[0], values[1]):
        raise ContractError("Conflicting storage aliases")
    return (values[0] if values else ""), send


def check_new_value(parsed, target, value, recordid=None):
    _, send = field_contract(parsed, target, recordid)
    controls = [v for k, v in parsed["fields"].items()
                if target in (k, v.get("send_key", k))]
    options = controls[0].get("options")
    if isinstance(options, list) and controls[0].get("type") == "select":
        if not any(isinstance(o, dict) and exact(value, o.get("value")) for o in options):
            raise ContractError("Value outside editable options")
    return send


def require_expected(payload):
    if "expected_old_value" not in payload or payload["expected_old_value"] is None:
        raise ContractError("Explicit non-null expected_old_value required")
    return payload["expected_old_value"]


def validate_optional_read(method, args, inventory):
    """Only four explicitly pinned read methods, never dynamic write exposure."""
    shapes = {
        "get_project_settings": {"projectid"},
        "get_sitemap_xml": {"projectid"},
        "page_preview": {"projectid", "pageid"},
        "describe_tool": {"tool_name"},
    }
    if set(args) != shapes[method]:
        raise ContractError("Unexpected read arguments")
    names = {t.name for t in inventory}
    if method not in names:
        return False
    if method == "describe_tool":
        if not isinstance(args["tool_name"], str) or args["tool_name"] not in names:
            raise ContractError("Unknown described tool")
    else:
        for key in shapes[method]:
            if type(args[key]) is not int or args[key] <= 0:
                raise ContractError("Positive integer scope required")
    # Confirm actual required keys still agree with the pinned contract.
    tool = next(t for t in inventory if t.name == method)
    schema = getattr(tool, "inputSchema", None)
    if not isinstance(schema, dict):
        raise ContractError("Unknown upstream input schema")
    if not set(schema.get("required", [])) <= set(args):
        raise ContractError("Upstream requires different arguments")
    if not set(args) <= set(schema.get("properties", {})):
        raise ContractError("Upstream input schema differs")
    return True

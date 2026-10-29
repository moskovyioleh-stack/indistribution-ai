"""Isolated contract checks; no live requests, credentials, or external dependencies."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


class HTTPError(Exception):
    pass


class Client:
    response = None
    failure = None
    calls = []

    def __init__(self, **kwargs):
        self.options = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs, self.options))
        if self.failure:
            raise self.failure
        return self.response


fake_httpx = types.SimpleNamespace(AsyncClient=Client, HTTPError=HTTPError)
spec = importlib.util.spec_from_file_location("bridge_under_test", Path(__file__).with_name("yandex_tools.py"))
bridge = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"httpx": fake_httpx}):
    spec.loader.exec_module(bridge)


def response(result=None, status=200, envelope=None):
    if envelope is None:
        envelope = {"jsonrpc": "2.0", "id": 1, "result": result}
    return types.SimpleNamespace(status_code=status, json=lambda: envelope)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Client.calls = []
        Client.failure = None
        Client.response = response({"content": [{"type": "text", "text": '{"totals":[5,3,9]}'}]})
        self.environment = patch.dict(os.environ, {"YANDEX_MCP_ACCESS_KEY": "test-only-key"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def test_report_and_bounded_destination(self):
        args = dict(counter_id=111834069, date1="2026-09-29", date2="2026-10-28")
        report = await bridge.call_yandex("metrika_traffic", args)
        self.assertEqual(report["totals"], [5, 3, 9])
        url, request, options = Client.calls[0]
        self.assertEqual(url, bridge.UPSTREAM_URL)
        self.assertEqual(request["json"]["params"], {"name": "metrika_traffic", "arguments": args})
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-only-key")
        self.assertFalse(options["follow_redirects"])

    async def test_unknown_tool_domain_and_counter_rejected_before_network(self):
        for name, args in [("delete_counter", {}), ("webmaster_indexing", {"domain": "example.com"}),
                           ("metrika_pages", {"counter_id": True}), ("metrika_pages", {"counter_id": -1})]:
            with self.assertRaises(ValueError):
                await bridge.call_yandex(name, args)
        self.assertEqual(Client.calls, [])

    async def test_missing_configuration_fails_closed(self):
        with patch.dict(os.environ, {"YANDEX_MCP_ACCESS_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                await bridge.call_yandex("metrika_counters", {})
        self.assertEqual(Client.calls, [])

    async def test_upstream_failure_does_not_echo_secret_or_raw_error(self):
        for result in [{"isError": True, "content": [{"type": "text", "text": "test-only-key"}]},
                       {"content": [{"type": "text", "text": '{"truncated":'}]},
                       {"content": [None]}]:
            Client.response = response(result)
            with self.assertRaises(RuntimeError) as caught:
                await bridge.call_yandex("metrika_counters", {})
            self.assertNotIn("test-only-key", str(caught.exception))
        Client.failure = HTTPError("request contained test-only-key")
        with self.assertRaises(RuntimeError) as caught:
            await bridge.call_yandex("metrika_counters", {})
        self.assertNotIn("test-only-key", str(caught.exception))

    async def test_http_and_rpc_errors_fail(self):
        for status in [301, 401, 403, 500]:
            Client.response = response(status=status)
            with self.assertRaisesRegex(RuntimeError, str(status)):
                await bridge.call_yandex("metrika_counters", {})
        Client.response = response(envelope={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601}})
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            await bridge.call_yandex("metrika_counters", {})

    async def test_nine_tools_use_owner_auth_and_forward_correctly(self):
        class MCP:
            tools = {}
            def tool(self, **kwargs):
                def register(fn):
                    self.tools[fn.__name__] = (fn, kwargs)
                    return fn
                return register
        mcp = MCP()
        owner = object()
        bridge.register_yandex_tools(mcp, owner)
        self.assertEqual(set(mcp.tools), bridge.ALLOWED_TOOLS)
        for name, (fn, options) in mcp.tools.items():
            self.assertIs(options["auth"], owner)
            self.assertTrue(options["annotations"]["readOnlyHint"])
            if name in {"metrika_counters", "webmaster_sites"}:
                await fn()
            elif name.startswith("metrika_"):
                await fn(111834069)
            else:
                await fn("heinedasaw.indistribution.ru")
            self.assertEqual(Client.calls[-1][1]["json"]["params"]["name"], name)


if __name__ == "__main__":
    unittest.main()

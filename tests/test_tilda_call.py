"""Exercise the actual registered gateway with a fully mocked MCP upstream."""
import copy
import json
import os
import sys
import types
import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS

# The gateway only needs JSONResponse at import; no runtime packages/network are
# required for these unit tests. Deployment still uses the real requirements.
try:
    import starlette.responses
except ModuleNotFoundError:
    starlette = types.ModuleType("starlette")
    responses = types.ModuleType("starlette.responses")
    responses.JSONResponse = lambda *a, **kw: (a, kw)
    starlette.responses = responses
    sys.modules.update({"starlette": starlette, "starlette.responses": responses})

from local_bridge import register_bridge, action_catalog
from tilda_contracts import exact, result_data, ContractError, safe_failure_metadata


def response(value="OK", **kw):
    return NS(content=[NS(type="text", text=json.dumps(value, ensure_ascii=False))],
              is_error=kw.get("is_error", False),
              structured_content=kw.get("structured"))


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pid, self.pageid, self.rid = 30763609, 187658209, 3056787401
        self.record = {"id": str(self.rid), "title": "Old"}
        self.fields = {"title": {"type": "rich_text"},
                       "title_tag": {"type": "select", "options": [
                           {"value": ""}, {"value": "h1"}, {"value": "h2"}]}}
        self.page = {"id": str(self.pageid), "projectid": str(self.pid),
                     "date": "2026-08-14 13:18:00", "meta_title": "Old",
                     "meta_descr": "", "published": "1793295220"}
        self.calls = []
        self.foreign_project = self.foreign_page = self.foreign_record = False
        self.readback_error = self.readback_mismatch = self.write_timeout = False
        self.write_result = None
        self.write_count = 0
        self.missing_tools = set()
        self.override_edit = None
        self.tools = {
            "get_project_settings": {"projectid": {}},
            "get_sitemap_xml": {"projectid": {}},
            "page_preview": {"projectid": {}, "pageid": {}},
            "describe_tool": {"tool_name": {}},
            "update_record": {}, "update_page_settings": {},
        }
        owner = self

        class Client:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def list_tools(self):
                return [NS(name=k, inputSchema={"properties": v, "required": list(v)})
                        for k,v in owner.tools.items() if k not in owner.missing_tools]
            async def call_tool(self, name, arguments):
                return await owner.call(name, arguments)

        fake = types.ModuleType("fastmcp")
        fake.Client = Client
        transports = types.ModuleType("fastmcp.client.transports")
        transports.StreamableHttpTransport = lambda **kw: NS(**kw)
        self.modules = patch.dict(sys.modules, {"fastmcp": fake,
                        "fastmcp.client": types.ModuleType("fastmcp.client"),
                        "fastmcp.client.transports": transports})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        env = patch.dict(os.environ, {"TILDA_MCP_TOKEN": "mock-test-token"})
        env.start(); self.addCleanup(env.stop)

        class MCP:
            def tool(inner, **kw):
                def decorate(fn):
                    if fn.__name__ == "editor_action": self.action = fn
                    return fn
                return decorate
            def custom_route(inner, *a, **kw): return lambda f: f
        register_bridge(MCP(), lambda ctx: True, None, None)

    async def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if name == "get_projects":
            return response([] if self.foreign_project else
                            [{"id": str(p)} for p in [12112685,14178377,23291206,30763609]])
        if name == "get_pages":
            return response([] if self.foreign_page else [dict(
                id=str(self.pageid), projectid=str(args["projectid"]), trash="")])
        if name == "get_page_records":
            return response([] if self.foreign_record else [{"recordid": self.rid}])
        if name in ("edit_record", "get_page_settings"):
            if self.write_count and self.readback_error: raise RuntimeError("read failed")
            if name == "get_page_settings":
                data = copy.deepcopy(self.page)
                if self.write_count and self.readback_mismatch: data["meta_title"] = "Other"
                return response(data)
            if self.override_edit is not None: return self.override_edit
            data = {"record": copy.deepcopy(self.record), "fields": self.fields}
            if self.write_count and self.readback_mismatch:
                data["record"]["title"] = "Other"
            return response(data)
        if name in ("update_record", "update_page_settings", "add_blank_page", "add_record"):
            self.write_count += 1
            if self.write_timeout: raise TimeoutError("write uncertain")
            if self.write_result is not None: return self.write_result
            if name == "update_record":
                for key,val in args["fields"].items():
                    storage = next((k for k,c in self.fields.items()
                                    if c.get("send_key",k)==key), key)
                    if storage == "forminputs": storage = "list"
                    else: storage = self.fields.get(storage, {}).get("record_key", storage)
                    self.record[storage] = val
            elif name == "update_page_settings":
                self.page.update(args)
            return response(123 if name == "add_blank_page" else "OK")
        return response({"test_read": name})

    def payload(self, **kw):
        p = {"tool": "update_record", "arguments": {
            "projectid": self.pid, "pageid": self.pageid, "recordid": self.rid,
            "tab": "content", "fields": {"title": "New"}, "only_field": "title"},
             "expected_old_value": "Old", "confirmation": "SAVE_DRAFT_ONLY"}
        p.update(kw)
        return p

    async def run_call(self, p=None):
        return await self.action("tilda_call", self.payload() if p is None else p)

    async def assert_status(self, expected, p=None, writes=0):
        result = await self.run_call(p)
        self.assertEqual(result["status"], expected, result)
        self.assertEqual(self.write_count, writes)
        return result

    async def test_nonempty_verified(self):
        await self.assert_status("draft_verified", writes=1)
        self.assertEqual([n for n,a in self.calls].count("edit_record"), 2)

    async def test_explicit_empty_verified(self):
        self.record["title"] = ""
        await self.assert_status("draft_verified", self.payload(expected_old_value=""), writes=1)

    async def test_documented_missing_empty_settings(self):
        p=self.payload(expected_old_value="")
        p["arguments"].update(tab="settings", only_field="title_tag", fields={"title_tag":"h2"})
        await self.assert_status("draft_verified", p, writes=1)
        self.assertTrue(all(a["tab"]=="settings" for n,a in self.calls if n=="edit_record"))

    async def test_missing_expected(self):
        p=self.payload(); del p["expected_old_value"]
        await self.assert_status("precondition_required",p)

    async def test_null_expected(self):
        await self.assert_status("precondition_required",self.payload(expected_old_value=None))

    async def test_conflict(self):
        await self.assert_status("precondition_failed",self.payload(expected_old_value="OLD"))

    async def test_whitespace_not_trimmed(self):
        await self.assert_status("precondition_failed",self.payload(expected_old_value="Old "))

    async def test_false_not_integer(self):
        self.record["title"]=False
        await self.assert_status("precondition_failed",self.payload(expected_old_value=0))

    async def test_unknown_field(self):
        self.fields={}
        await self.assert_status("precondition_failed")

    async def test_unknown_schema(self):
        self.override_edit=response({"record": self.record})
        await self.assert_status("precondition_failed")

    async def test_hidden_field(self):
        self.fields["title"]["type"]="hidden"
        await self.assert_status("precondition_failed")

    async def test_send_key(self):
        self.fields["title"]["send_key"]="editor_title"
        await self.assert_status("draft_verified",writes=1)
        args=next(a for n,a in self.calls if n=="update_record")
        self.assertEqual(args["fields"],{"editor_title":"New"})
        self.assertEqual(args["only_field"],"editor_title")

    async def test_form_receiver_record_key_verified(self):
        self.fields = {"formaction": {"type": "special", "record_key": "formintegrations",
                                      "send_key": "formaction"}}
        self.record = {"formintegrations": []}
        p = self.payload(expected_old_value=[])
        p["arguments"].update(only_field="formaction", fields={"formaction": ["receiver-id"]})
        await self.assert_status("draft_verified", p, writes=1)
        self.assertEqual(self.record["formintegrations"], ["receiver-id"])

    async def test_record_key_conflict_and_invalid_key_rejected(self):
        self.fields["title"]["record_key"] = "stored_title"
        self.record["stored_title"] = "Changed"
        await self.assert_status("precondition_failed")
        self.fields["title"]["record_key"] = []
        await self.assert_status("precondition_failed")

    async def test_target_send_key(self):
        self.fields["title"]["send_key"]="editor_title"
        p=self.payload();p["arguments"].update(only_field="editor_title",fields={"editor_title":"New"})
        await self.assert_status("draft_verified",p,writes=1)

    async def test_ambiguous_send_key(self):
        self.fields["other"]={"type":"rich_text","send_key":"title"}
        await self.assert_status("precondition_failed")

    async def test_send_key_cannot_bypass_list_guard(self):
        self.fields={"editor_list":{"type":"json","send_key":"list"}}
        self.record={"editor_list":"[]"}
        p=self.payload(expected_old_value="[]")
        p["arguments"].update(only_field="editor_list",fields={"editor_list":"[]"})
        await self.assert_status("forbidden",p)

    async def test_conflicting_alias_storage(self):
        self.fields["title"]["send_key"]="editor_title"
        self.record["editor_title"]="Other"
        await self.assert_status("precondition_failed")

    async def test_select_value_validation(self):
        p=self.payload(expected_old_value="")
        p["arguments"].update(tab="settings",only_field="title_tag",fields={"title_tag":"script"})
        await self.assert_status("precondition_failed",p)

    async def test_multiple_fields(self):
        p=self.payload();p["arguments"]["fields"]["title_tag"]="h2"
        await self.assert_status("invalid_request",p)

    async def test_confirmation(self):
        p=self.payload();del p["confirmation"]
        await self.assert_status("confirmation_required",p)

    async def test_foreign_project(self):
        self.foreign_project=True
        await self.assert_status("forbidden")

    async def test_project_allowlist(self):
        p=self.payload();p["arguments"]["projectid"]=999
        await self.assert_status("forbidden",p)

    async def test_foreign_page(self):
        self.foreign_page=True
        await self.assert_status("forbidden")

    async def test_foreign_record(self):
        self.foreign_record=True
        await self.assert_status("forbidden")

    async def test_structured_write_error(self):
        self.write_result=response("OK",structured={"error":"failed"})
        await self.assert_status("write_unconfirmed",writes=1)

    async def test_nested_structured_error(self):
        self.write_result=response("OK",structured={"result":{"isError":True}})
        await self.assert_status("write_unconfirmed",writes=1)

    async def test_json_error_response(self):
        self.write_result=response({"status":"error"})
        await self.assert_status("write_unconfirmed",writes=1)

    async def test_mcp_error_response(self):
        self.write_result=response("OK",is_error=True)
        await self.assert_status("write_unconfirmed",writes=1)

    async def test_readback_failure_no_retry(self):
        self.readback_error=True
        await self.assert_status("write_unconfirmed",writes=1)

    async def test_readback_mismatch_no_retry(self):
        self.readback_mismatch=True
        await self.assert_status("verification_mismatch",writes=1)

    async def test_write_timeout_no_retry(self):
        self.write_timeout=True
        await self.assert_status("write_unconfirmed",writes=1)

    async def test_safe_read_failure_diagnostics(self):
        original = self.call
        async def fail(name, args):
            if name == "get_pages":
                raise RuntimeError("HTTP 429 secret-token-and-body")
            return await original(name, args)
        self.call = fail
        result = await self.run_call()
        self.assertEqual(result["status"], "upstream_error")
        self.assertEqual(result["upstream_http_status"], 429)
        self.assertEqual(result["upstream_tool"], "get_pages")
        self.assertNotIn("secret-token", str(result))
        self.assertEqual(self.write_count, 0)

    async def test_safe_rpc_failure_metadata(self):
        exc = RuntimeError("Too many requests: sensitive request contents")
        exc.error = NS(code=-32000)
        details = safe_failure_metadata(exc)
        self.assertEqual(details["upstream_rpc_code"], -32000)
        self.assertEqual(details["upstream_error_kind"], "rate_limit_message")
        self.assertNotIn("sensitive", str(details))

    async def test_precondition_structured_error(self):
        self.override_edit=response("OK",structured={"isError":True})
        await self.assert_status("upstream_error")

    async def test_no_control_values_forwarded(self):
        await self.assert_status("draft_verified",writes=1)
        args=next(a for n,a in self.calls if n=="update_record")
        self.assertNotIn("expected_old_value",args);self.assertNotIn("confirmation",args)

    async def test_control_values_in_args_rejected(self):
        p=self.payload();p["arguments"]["expected_old_value"]="Old"
        await self.assert_status("invalid_request",p)

    async def test_unhashable_field_rejected(self):
        p=self.payload();p["arguments"]["only_field"]=["title"]
        await self.assert_status("invalid_request",p)

    async def test_legacy_create_cannot_bypass_scope(self):
        r=await self.action("tilda_create_draft_page",{"project_id":12112685,
                                                     "confirmation":"SAVE_DRAFT_ONLY"})
        self.assertEqual(r["status"],"redirect_required")
        self.assertEqual(self.calls,[])

    async def test_protected_card_identity(self):
        self.fields["list"]={"type":"json"}
        self.record["list"]='[{"lid":"1","li_title":"A"}]'
        p=self.payload(expected_old_value=self.record["list"])
        p["arguments"].update(only_field="list",fields={"list":'[{"lid":"2","li_title":"A"}]'})
        await self.assert_status("forbidden",p)

    async def test_two_card_changes_blocked(self):
        self.fields["list"]={"type":"json"}
        self.record["list"]='[{"lid":"1","li_title":"A","li_descr":"B"}]'
        p=self.payload(expected_old_value=self.record["list"])
        p["arguments"].update(only_field="list",fields={"list":'[{"lid":"1","li_title":"X","li_descr":"Y"}]'})
        await self.assert_status("forbidden",p)

    async def test_structured_only_edit_response(self):
        self.override_edit=NS(content=[],is_error=False,
                              structured_content={"record":self.record,"fields":self.fields})
        await self.assert_status("draft_verified",writes=1)

    async def test_seo_missing_expected(self):
        p=self.seo_payload();del p["expected_old_value"]
        await self.assert_status("precondition_required",p)

    async def test_seo_no_confirmation(self):
        p=self.seo_payload();del p["confirmation"]
        await self.assert_status("confirmation_required",p)

    async def test_optional_malformed_catalog(self):
        self.tools["get_sitemap_xml"]={"other":{}}
        await self.assert_status("invalid_request",self.payload(
            tool="get_sitemap_xml",arguments={"projectid":self.pid}))

    def seo_payload(self, **kw):
        return self.payload(tool="update_page_settings", arguments={
            "projectid":self.pid,"pageid":self.pageid,"date":"2026-08-14 13:18",
            "meta_title":"New"},**kw)

    async def test_seo_verified(self):
        await self.assert_status("draft_verified",self.seo_payload(),writes=1)
        args=next(a for n,a in self.calls if n=="update_page_settings")
        self.assertEqual(set(args),{"projectid","pageid","date","meta_title"})

    async def test_seo_conflict(self):
        await self.assert_status("precondition_failed",self.seo_payload(expected_old_value="Other"))

    async def test_seo_empty(self):
        p=self.seo_payload(expected_old_value="")
        p["arguments"].pop("meta_title");p["arguments"]["meta_descr"]="New"
        await self.assert_status("draft_verified",p,writes=1)

    async def test_seo_multiple(self):
        p=self.seo_payload();p["arguments"]["meta_descr"]="New"
        await self.assert_status("invalid_request",p)

    async def test_seo_null(self):
        await self.assert_status("precondition_required",self.seo_payload(expected_old_value=None))

    async def test_seo_changed_date(self):
        p=self.seo_payload();p["arguments"]["date"]="2026-10-30 12:00"
        await self.assert_status("precondition_failed",p)

    async def test_seo_readback_failure(self):
        self.readback_error=True
        await self.assert_status("write_unconfirmed",self.seo_payload(),writes=1)

    async def test_seo_readback_mismatch(self):
        self.readback_mismatch=True
        await self.assert_status("verification_mismatch",self.seo_payload(),writes=1)

    async def test_creation_allowed_projects(self):
        for pid in (14178377,23291206,30763609):
            self.write_count=0
            await self.assert_status("upstream_accepted",self.payload(
                tool="add_blank_page",arguments={"projectid":pid}),writes=1)

    async def test_creation_disallowed(self):
        await self.assert_status("forbidden",self.payload(
            tool="add_blank_page",arguments={"projectid":12112685}))

    async def test_creation_timeout_no_retry(self):
        self.write_timeout=True
        await self.assert_status("write_unconfirmed",self.payload(
            tool="add_blank_page",arguments={"projectid":30763609}),writes=1)

    async def test_destructive_tools_forbidden(self):
        for name in ("publish_page","unpublish","delete_record","delete_page","arbitrary_write"):
            await self.assert_status("forbidden",self.payload(tool=name))

    async def test_legacy_deletion_forbidden(self):
        r=await self.action("tilda_remove_draft_block",self.payload())
        self.assertEqual(r["status"],"forbidden");self.assertEqual(self.calls,[])

    async def test_optional_reads(self):
        for method,args in (
            ("get_project_settings",{"projectid":self.pid}),
            ("get_sitemap_xml",{"projectid":self.pid}),
            ("page_preview",{"projectid":self.pid,"pageid":self.pageid}),
            ("describe_tool",{"tool_name":"update_record"})):
            await self.assert_status("upstream_accepted",self.payload(tool=method,arguments=args))

    async def test_optional_absent(self):
        self.missing_tools={"page_preview"}
        await self.assert_status("unsupported_method",self.payload(
            tool="page_preview",arguments={"projectid":self.pid,"pageid":self.pageid}))
        self.assertNotIn("page_preview",[n for n,a in self.calls])

    async def test_optional_extra_args(self):
        await self.assert_status("invalid_request",self.payload(
            tool="get_sitemap_xml",arguments={"projectid":self.pid,"extra":"x"}))

    async def test_preview_foreign_page(self):
        self.foreign_page=True
        await self.assert_status("forbidden",self.payload(
            tool="page_preview",arguments={"projectid":self.pid,"pageid":self.pageid}))

    async def test_describe_unknown(self):
        await self.assert_status("invalid_request",self.payload(
            tool="describe_tool",arguments={"tool_name":"unknown"}))

    async def test_list_structure_guard(self):
        self.fields["list"]={"type":"json"}
        self.record["list"]='[{"lid":"1","li_title":"A"}]'
        p=self.payload(expected_old_value=self.record["list"])
        p["arguments"].update(only_field="list",fields={"list":'[]'})
        await self.assert_status("forbidden",p)

    async def test_list_one_text_change(self):
        self.fields["list"]={"type":"json"}
        self.record["list"]='[{"lid":"1","li_title":"A"}]'
        p=self.payload(expected_old_value=self.record["list"])
        p["arguments"].update(only_field="list",fields={"list":'[{"lid":"1","li_title":"B"}]'})
        await self.assert_status("draft_verified",p,writes=1)

    async def test_molot_unsafe_form_field(self):
        p=self.payload();p["arguments"].update(projectid=14178377,pageid=302584509,
            recordid=4796839901,only_field="unsafe",fields={"unsafe":"x"})
        await self.assert_status("forbidden",p)

    async def test_powders_font_guard(self):
        await self.assert_status("forbidden",self.payload(tool="save_project_fonts",
            arguments={"projectid":23291206,"headlinefont":"Wrong","textfont":"Wrong"}))

    async def test_heineda_pageslist_guard(self):
        p=self.payload();p["arguments"].update(pageid=187654909,
            only_field="pageslist",fields={"pageslist":[999]})
        await self.assert_status("forbidden",p)

    def test_capabilities_routes(self):
        c=action_catalog()
        self.assertFalse(c["official_mcp"]["browser_session_required"])
        self.assertIn(30763609,c["official_mcp"]["create_page_project_ids"])
        self.assertNotIn("delete_record",c["official_mcp"]["write_methods"])
        self.assertIn("tilda_remove_draft_block",c["permanently_disabled"])

    def test_exact_nested_types(self):
        self.assertFalse(exact({"a":[False]},{"a":[0]}))

    def test_structured_errors_rejected(self):
        for obj in ({"error":"bad"},{"success":False},{"data":{"error":"bad"}}):
            with self.subTest(obj=obj), self.assertRaises(ContractError):
                result_data(response("OK",structured=obj))


if __name__=="__main__":
    unittest.main()

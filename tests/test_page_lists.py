"""Native page selectors: actual gateway, mocked upstream, no Tilda writes."""
import copy
import unittest
import test_tilda_call as gateway
from test_tilda_call import response


class PageListTests(unittest.IsolatedAsyncioTestCase):
    payload = gateway.GatewayTests.payload
    run_call = gateway.GatewayTests.run_call
    assert_status = gateway.GatewayTests.assert_status

    def setUp(self):
        gateway.GatewayTests.setUp(self)
        self.configure()
        self.pages_error = self.schema_after = None

    def configure(self, pid=30763609, pageid=187654909, rid=3496526801):
        self.pid, self.pageid, self.rid = pid, pageid, rid
        self.fields = {"pageslist": {"type": "id_list", "record_key": "pageslist",
                                     "send_key": "pageslist"}}
        self.record = {"id": str(rid), "pageslist": [11, 12]}
        self.pages = [{"id": str(p), "projectid": str(pid), "trash": ""}
                      for p in [pageid, 11, 12, 13]]

    def selection(self, desired=None, expected=None):
        p = self.payload(expected_old_value=[11, 12] if expected is None else expected)
        p["arguments"].update(only_field="pageslist",
                              fields={"pageslist": [11, 13, 12] if desired is None else desired})
        return p

    async def call(self, name, args):
        if name == "get_pages":
            self.calls.append((name, copy.deepcopy(args)))
            if self.pages_error: return self.pages_error
            return response([] if self.foreign_page else self.pages)
        if name == "edit_record" and self.write_count:
            if self.readback_error:
                self.calls.append((name, copy.deepcopy(args)))
                raise RuntimeError("readback failed")
            if self.readback_mismatch:
                self.calls.append((name, copy.deepcopy(args)))
                return response({"record": {"id": str(self.rid), "pageslist": [12, 11]},
                                 "fields": self.fields})
            if self.schema_after is not None:
                self.calls.append((name, copy.deepcopy(args)))
                return response({"record": self.record, "fields": self.schema_after})
        return await gateway.GatewayTests.call(self, name, args)

    async def test_all_approved_projects_and_different_blocks(self):
        for i, pid in enumerate([12112685, 14178377, 23291206, 30763609]):
            with self.subTest(pid=pid):
                self.configure(pid, 100 + i, 200 + i)
                self.calls, self.write_count = [], 0
                await self.assert_status("draft_verified", self.selection(), writes=1)

    async def test_add_remove_reorder_clear(self):
        for desired in [[11, 13, 12], [12], [12, 11], []]:
            with self.subTest(desired=desired):
                self.configure(); self.calls, self.write_count = [], 0
                await self.assert_status("draft_verified", self.selection(desired), writes=1)
                self.assertEqual(self.record["pageslist"], desired)
                write = next(a for n, a in self.calls if n == "update_record")
                self.assertEqual(set(write), {"projectid", "pageid", "recordid", "tab", "fields", "only_field"})
                self.assertNotIn("confirmation", write)

    async def test_previous_catalog_selections_still_allowed(self):
        for desired in [[192213509, 192399909, 192450309, 192470909],
                        [198379209, 198428009]]:
            self.configure(); self.calls, self.write_count = [], 0
            self.pages.extend({"id": str(p), "projectid": str(self.pid), "trash": ""}
                              for p in desired)
            await self.assert_status("draft_verified", self.selection(desired), writes=1)

    async def test_noop_without_write(self):
        result = await self.assert_status("draft_verified", self.selection([11, 12]))
        self.assertFalse(result["changed"])
        self.assertFalse(result["write_performed"])
        self.assertNotIn("update_record", [n for n, _ in self.calls])

    async def test_record_and_send_keys(self):
        self.fields["pageslist"].update(record_key="stored_pages", send_key="selected_ids")
        self.record = {"id": str(self.rid), "stored_pages": [11, 12]}
        await self.assert_status("draft_verified", self.selection(), writes=1)
        write = next(a for n, a in self.calls if n == "update_record")
        self.assertEqual(write["fields"], {"selected_ids": [11, 13, 12]})
        self.assertEqual(write["only_field"], "selected_ids")

    async def test_missing_documented_keys(self):
        for key in ["record_key", "send_key"]:
            self.configure(); del self.fields["pageslist"][key]
            await self.assert_status("precondition_failed", self.selection())

    async def test_invalid_or_unknown_schema(self):
        for control in [{}, {"type": "json"}, {"type": "hidden"},
                        {"type": "id_list", "record_key": [], "send_key": "pageslist"}]:
            self.fields = {"pageslist": control}
            await self.assert_status("precondition_failed", self.selection())

    async def test_ambiguous_schema(self):
        self.fields["other"] = {"type": "id_list", "send_key": "pageslist"}
        await self.assert_status("precondition_failed", self.selection())

    async def test_conflicting_storage_alias(self):
        self.fields["pageslist"]["record_key"] = "stored_pages"
        self.record["stored_pages"] = [12, 11]
        await self.assert_status("precondition_failed", self.selection())

    async def test_invalid_values(self):
        for value in [[11, 11], ["11"], [True], [11.0], [0], [-1], "[11,12]", {}, None]:
            p = self.selection(); p["arguments"]["fields"]["pageslist"] = value
            with self.subTest(value=value):
                await self.assert_status("precondition_failed", p)

    async def test_documented_empty_constraint(self):
        self.fields["pageslist"]["minItems"] = 1
        await self.assert_status("precondition_failed", self.selection([]))

    async def test_foreign_selected_page(self):
        self.pages[-1]["projectid"] = "999"
        await self.assert_status("precondition_failed", self.selection())

    async def test_trashed_selected_page(self):
        self.pages[-1]["trash"] = "1"
        await self.assert_status("precondition_failed", self.selection())

    async def test_missing_selected_page(self):
        self.pages.pop()
        await self.assert_status("precondition_failed", self.selection())

    async def test_duplicate_inventory_is_ambiguous(self):
        self.pages.append(copy.deepcopy(self.pages[-1]))
        await self.assert_status("precondition_failed", self.selection())

    async def test_project_access(self):
        self.foreign_project = True
        await self.assert_status("forbidden", self.selection())

    async def test_unapproved_project(self):
        p = self.selection(); p["arguments"]["projectid"] = 999
        await self.assert_status("forbidden", p)

    async def test_target_page_membership(self):
        self.pages[0]["projectid"] = "999"
        await self.assert_status("forbidden", self.selection())

    async def test_target_block_membership(self):
        self.foreign_record = True
        await self.assert_status("forbidden", self.selection())

    async def test_wrong_tab_has_no_schema(self):
        self.override_edit = response({"record": self.record, "fields": {}})
        p = self.selection(); p["arguments"]["tab"] = "settings"
        await self.assert_status("precondition_failed", p)

    async def test_missing_null_expected(self):
        for missing in [True, False]:
            p = self.selection()
            if missing: del p["expected_old_value"]
            else: p["expected_old_value"] = None
            await self.assert_status("precondition_required", p)

    async def test_exact_expected_type_order(self):
        for expected in [[12, 11], [11, "12"], [True, 12], "[11,12]", []]:
            await self.assert_status("precondition_failed", self.selection(expected=expected))

    async def test_multiple_fields(self):
        p = self.selection(); p["arguments"]["fields"]["title"] = "x"
        await self.assert_status("invalid_request", p)

    async def test_missing_confirmation(self):
        p = self.selection(); del p["confirmation"]
        await self.assert_status("confirmation_required", p)

    async def test_extra_arguments_not_forwarded(self):
        p = self.selection(); p["arguments"]["publish"] = True
        await self.assert_status("invalid_request", p)

    async def test_structured_inventory_error(self):
        self.pages_error = response("OK", structured={"error": "failed"})
        await self.assert_status("upstream_error", self.selection())

    async def test_structured_write_error_no_retry(self):
        self.write_result = response("OK", structured={"result": {"isError": True}})
        await self.assert_status("write_unconfirmed", self.selection(), writes=1)

    async def test_readback_mismatch_no_retry(self):
        self.readback_mismatch = True
        await self.assert_status("verification_mismatch", self.selection(), writes=1)

    async def test_readback_failure_no_retry(self):
        self.readback_error = True
        await self.assert_status("write_unconfirmed", self.selection(), writes=1)

    async def test_readback_schema_failure_no_retry(self):
        self.schema_after = {}
        await self.assert_status("write_unconfirmed", self.selection(), writes=1)

    async def test_write_timeout_no_retry(self):
        self.write_timeout = True
        await self.assert_status("write_unconfirmed", self.selection(), writes=1)

    async def test_publish_and_delete_still_forbidden(self):
        for tool in ["publish", "publish_page", "unpublish", "delete_page", "delete_record"]:
            p = self.selection(); p["tool"] = tool
            await self.assert_status("forbidden", p)
        r = await self.action("tilda_remove_draft_block", {})
        self.assertEqual(r["status"], "forbidden")

    async def test_protected_send_alias_cannot_bypass(self):
        self.fields = {"selector": {"type": "id_list", "record_key": "pageslist", "send_key": "pageslist"}}
        p = self.selection(); p["arguments"].update(only_field="selector", fields={"selector": [13]})
        await self.assert_status("forbidden", p)

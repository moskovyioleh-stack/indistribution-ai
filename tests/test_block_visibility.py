"""Visibility guards and uncertain toggles against the actual gateway."""
import unittest
import test_tilda_call as gateway
from test_tilda_call import response
from tilda_contracts import block_visibility, ContractError


class VisibilityTests(unittest.IsolatedAsyncioTestCase):
    setUp = gateway.GatewayTests.setUp
    run_call = gateway.GatewayTests.run_call
    assert_status = gateway.GatewayTests.assert_status

    def payload(self, **kw):
        p = {"tool": "toggle_page_block_visibility", "arguments": {
            "projectid": self.pid, "pageid": self.pageid, "recordid": self.rid},
            "expected_old_value": "n", "confirmation": "SAVE_DRAFT_ONLY"}
        p.update(kw)
        return p

    async def call(self, name, args):
        if name == "get_record_html":
            self.calls.append((name, dict(args)))
            if self.write_count and self.readback_error:
                raise TimeoutError("read failed")
            rid = getattr(self, "html_recordid", self.rid)
            state = getattr(self, "state", "n")
            return response({"html": f'<div off="{state}" class="record" recordid="{rid}" id="record{rid}"></div>'})
        if name == "toggle_page_block_visibility":
            self.calls.append((name, dict(args)))
            self.write_count += 1
            if not self.readback_mismatch:
                self.state = "y" if getattr(self, "state", "n") == "n" else "n"
            if self.write_timeout:
                raise TimeoutError("uncertain write")
            return response(self.state)
        return await gateway.GatewayTests.call(self, name, args)

    async def test_hide_in_each_approved_project(self):
        for pid in (12112685, 14178377, 23291206, 30763609):
            self.pid, self.state, self.write_count = pid, "n", 0
            r = await self.assert_status("draft_verified", writes=1)
            self.assertEqual(r["visibility"], "y")
            self.assertTrue(r["hidden"])
            self.assertFalse(r["published_by_agent"])

    async def test_restore_and_stale_retry(self):
        self.state = "y"
        r = await self.assert_status("draft_verified", self.payload(expected_old_value="y"), writes=1)
        self.assertEqual(r["visibility"], "n")
        await self.assert_status("precondition_failed", self.payload(expected_old_value="y"), writes=1)

    async def test_unknown_visibility(self):
        self.state = "unknown"
        await self.assert_status("precondition_failed")

    async def test_expected_required(self):
        for expected in (None, True, False, 0, 1, "", "visible", [], {}):
            await self.assert_status("precondition_required", self.payload(expected_old_value=expected))
        p = self.payload(); del p["expected_old_value"]
        await self.assert_status("precondition_required", p)

    async def test_missing_confirmation(self):
        await self.assert_status("confirmation_required", self.payload(confirmation="PUBLISH"))

    async def test_foreign_project(self):
        self.pid = 999
        await self.assert_status("forbidden")
        self.assertEqual(self.calls, [])

    async def test_unavailable_account_project(self):
        self.foreign_project = True
        await self.assert_status("forbidden")

    async def test_foreign_page(self):
        self.foreign_page = True
        await self.assert_status("forbidden")

    async def test_foreign_record(self):
        self.foreign_record = True
        await self.assert_status("forbidden")
        self.assertNotIn("get_record_html", [n for n, a in self.calls])

    async def test_wrong_html_record(self):
        self.html_recordid = self.rid + 1
        await self.assert_status("precondition_failed")

    async def test_strict_ids_and_extra_arguments(self):
        for key in ("projectid", "pageid", "recordid"):
            for value in (True, "123", 1.5, -1, 0):
                p = self.payload(); p["arguments"][key] = value
                r = await self.run_call(p)
                self.assertNotEqual(r["status"], "draft_verified")
                self.assertEqual(self.write_count, 0)
        p = self.payload(); p["arguments"]["publish"] = True
        await self.assert_status("invalid_request", p)

    async def test_mismatch_is_not_success(self):
        self.state = "n"
        self.readback_mismatch = True
        await self.assert_status("verification_mismatch", writes=1)

    async def test_uncertain_write_never_retried(self):
        self.write_timeout = True
        r = await self.assert_status("write_unconfirmed", writes=1)
        self.assertFalse(r["retry_automatically"])
        await self.assert_status("precondition_failed", writes=1)

    async def test_readback_failure(self):
        self.readback_error = True
        await self.assert_status("write_unconfirmed", writes=1)


class WrapperTests(unittest.TestCase):
    def test_quotes_order_and_whitespace(self):
        self.assertEqual(block_visibility({"html": "<!-- native -->\n<div off='y'\nrecordid='42' class='record other' id='record42'></div>"}, 42), "y")

    def test_ambiguous_or_missing_wrapper_fails_closed(self):
        good = '<div class="record" id="record42" recordid="42" off="n"></div>'
        for html in ("", '<div>' + good + '</div>', good + good,
                     good.replace('off="n"', 'off="n" off="y"'),
                     good.replace('off="n"', ''), good.replace('off="n"', 'off="false"'),
                     good.replace('id="record42"', 'id="record43"'),
                     good.replace('class="record"', 'class="other"')):
            with self.assertRaises(ContractError):
                block_visibility({"html": html}, 42)

import copy
import json
import unittest
from feeds_editor import dispatch, version

PROJECTS = {12112685, 14178377, 23291206, 30763609}


class FakeUpstream:
    def __init__(self):
        self.calls = []
        self.post = {"postuid": "abc123", "title": "Original", "active": "",
                     "text": "[{&quot;ty&quot;:&quot;text&quot;,&quot;te&quot;:&quot;Body&quot;}]",
                     "descr": "A &amp; B", "postalias": None, "seo_title": "SEO",
                     "authorname": "Author"}
        self.fail_verify = False

    async def call_tool(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if name == "get_projects":
            data = [{"id": x} for x in PROJECTS]
        elif name == "feeds_get_post":
            if self.fail_verify and any(n == "feeds_update_post" for n, _ in self.calls):
                raise RuntimeError("connection closed")
            data = copy.deepcopy(self.post)
        elif name == "feeds_update_post":
            self.post.update({k: v for k, v in args.items() if k not in {"projectid", "visibility"}})
            self.post.update(args.get("custom_fields", {}))
            data = {"success": True}
        elif name == "feeds_toggle_post_visibility":
            self.post["active"] = "" if self.post["active"] == "y" else "y"
            data = {"success": True}
        elif name == "feeds_add_post":
            self.post.update(postuid="new123", title=args["title"], active="")
            data = {"postuid": "new123", "title": args["title"]}
            data["uid"] = "new123"
        else:
            data = {"feeds": []}
        return {"structuredContent": data}


def payload(upstream, **extra):
    return {"arguments": {"projectid": 30763609, "postuid": "abc123"},
            "confirmation": "SAVE_DRAFT_ONLY", "expected_post_version": version(upstream.post),
            "patch": {"title": "New"}, **extra}


class FeedsTests(unittest.IsolatedAsyncioTestCase):
    async def test_false_flags_are_sent_as_empty_not_truthy_n(self):
        u = FakeUpstream()
        u.post.update(disableamp="n", disablerss="n", nosearch="n", nofollow="n")
        r = await dispatch(u, "feeds_update_post", payload(u), PROJECTS)
        self.assertEqual(r["status"], "draft_verified")
        saved = next(a for n, a in u.calls if n == "feeds_update_post")
        for key in ("disableamp", "disablerss", "nosearch", "nofollow"):
            self.assertEqual(saved[key], "")

    async def test_creation_is_read_back_as_unpublished(self):
        u = FakeUpstream()
        r = await dispatch(u, "feeds_add_post", {
            "arguments": {"projectid": 30763609, "feeduid": "feed123", "title": "Draft"},
            "confirmation": "SAVE_DRAFT_ONLY"}, PROJECTS)
        self.assertEqual(r["status"], "draft_created_verified")
        self.assertFalse(r["published"])
        self.assertEqual([n for n, _ in u.calls],
                         ["get_projects", "feeds_get_feed", "feeds_add_post", "feeds_get_post"])

    async def test_read_all_four_and_deny_other_project(self):
        u = FakeUpstream()
        for pid in PROJECTS:
            r = await dispatch(u, "feeds_get_feeds", {"arguments": {"projectid": pid}}, PROJECTS)
            self.assertEqual(r["status"], "ok")
        u.calls.clear()
        r = await dispatch(u, "feeds_get_feeds", {"arguments": {"projectid": 999}}, PROJECTS)
        self.assertEqual(r["status"], "forbidden")
        self.assertEqual(u.calls, [])

    async def test_edit_preserves_body_author_and_decodes_once(self):
        u = FakeUpstream()
        r = await dispatch(u, "feeds_update_post", payload(u), PROJECTS)
        self.assertEqual(r["status"], "draft_verified")
        saved = next(a for n, a in u.calls if n == "feeds_update_post")
        self.assertEqual(saved["text"], '[{"ty":"text","te":"Body"}]')
        self.assertEqual(saved["descr"], "A & B")
        self.assertEqual(saved["authorname"], "Author")
        self.assertEqual(saved["visibility"], "")

    async def test_stale_version_blocks_write(self):
        u = FakeUpstream()
        p = payload(u)
        u.post["title"] = "Concurrent change"
        r = await dispatch(u, "feeds_update_post", p, PROJECTS)
        self.assertEqual(r["status"], "precondition_failed")
        self.assertNotIn("feeds_update_post", [n for n, _ in u.calls])

    async def test_published_post_cannot_be_draft_edited(self):
        u = FakeUpstream()
        u.post["active"] = "y"
        r = await dispatch(u, "feeds_update_post", payload(u), PROJECTS)
        self.assertEqual(r["status"], "forbidden")

    async def test_publish_requires_separate_confirmation_and_never_unpublishes(self):
        u = FakeUpstream()
        r = await dispatch(u, "feeds_publish_post", payload(u), PROJECTS)
        self.assertEqual(r["status"], "confirmation_required")
        r = await dispatch(u, "feeds_publish_post", payload(u, confirmation="PUBLISH_FEED_POST"), PROJECTS)
        self.assertEqual(r["status"], "published_verified")
        r = await dispatch(u, "feeds_publish_post", payload(u, confirmation="PUBLISH_FEED_POST"), PROJECTS)
        self.assertFalse(r["write_performed"])
        self.assertEqual(sum(n == "feeds_toggle_post_visibility" for n, _ in u.calls), 1)

    async def test_no_raw_visibility_or_unknown_write(self):
        u = FakeUpstream()
        for method, p in [("feeds_toggle_post_visibility", payload(u)),
                          ("feeds_update_post", payload(u, patch={"visibility": "y"}))]:
            r = await dispatch(u, method, p, PROJECTS)
            self.assertIn(r["status"], {"forbidden", "precondition_failed"})
        self.assertFalse(any(n == "feeds_update_post" for n, _ in u.calls))

    async def test_uncertain_write_is_not_retried(self):
        u = FakeUpstream()
        u.fail_verify = True
        r = await dispatch(u, "feeds_update_post", payload(u), PROJECTS)
        self.assertEqual(r["status"], "write_unconfirmed")
        self.assertEqual(sum(n == "feeds_update_post" for n, _ in u.calls), 1)

    async def test_body_requires_blocks_and_unknown_custom_type_is_blocked(self):
        u = FakeUpstream()
        r = await dispatch(u, "feeds_update_post", payload(u, patch={"text": "plain text"}), PROJECTS)
        self.assertEqual(r["status"], "precondition_failed")
        u.post["fields"] = [{"lid": 1}]
        r = await dispatch(u, "feeds_update_post", payload(u), PROJECTS)
        self.assertEqual(r["status"], "precondition_failed")

    async def test_custom_fields_preserved_and_editable_without_hardcoded_lids(self):
        u = FakeUpstream()
        u.post.update(fields=[{"lid": "123", "li_type": "ta"},
                              {"lid": "456", "li_type": "image"},
                              {"lid": "789", "li_type": "formated"}],
                      **{"123": "Announcement", "456": {"url": "", "alt": ""},
                         "789": "<p>Original body</p>"})
        r = await dispatch(u, "feeds_update_post", payload(u,
            patch={}, custom_fields_patch={"789": "<p>New body</p>"}), PROJECTS)
        # Simulate the provider's numeric-field expansion in the mock below.
        self.assertEqual(r["status"], "draft_verified")
        saved = next(a for n, a in u.calls if n == "feeds_update_post")
        self.assertEqual(saved["custom_fields"]["123"], "Announcement")
        self.assertEqual(saved["custom_fields"]["456"], {"url": "", "alt": ""})
        self.assertEqual(saved["custom_fields"]["789"], "<p>New body</p>")


if __name__ == "__main__":
    unittest.main()

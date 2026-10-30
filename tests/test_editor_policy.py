import unittest
from editor_policy import EditRequest, EditScopeError, validate_edit_request, validate_tilda_editor_url


class EditorPolicyTests(unittest.TestCase):
    def test_pilot_page_accepted_without_execution(self):
        outcome = validate_edit_request(EditRequest("14178377", "296498609", "text", "Correct heading"))
        self.assertEqual(outcome["status"], "validated_not_executed")
        self.assertFalse(outcome["publishing_allowed"])

    def test_other_site_blocked_by_default(self):
        with self.assertRaises(EditScopeError):
            validate_edit_request(EditRequest("12112685", "73629709", "text", "Edit"))

    def test_wrong_page_blocked(self):
        with self.assertRaises(EditScopeError):
            validate_edit_request(EditRequest("14178377", "73934705", "text", "Edit"))

    def test_unapproved_project_blocked_even_when_others_enabled(self):
        with self.assertRaises(EditScopeError):
            validate_edit_request(EditRequest("unknown", "123", "text", "Edit"), enable_other_sites=True)

    def test_non_tilda_url_blocked(self):
        with self.assertRaises(EditScopeError):
            validate_tilda_editor_url("https://example.com/")

    def test_safe_editor_url_allowed(self):
        self.assertEqual(validate_tilda_editor_url("https://tilda.cc/projects/"), "https://tilda.cc/projects/")


if __name__ == "__main__":
    unittest.main()

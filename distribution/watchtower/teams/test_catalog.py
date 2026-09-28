"""Built-in templates are fixed portable policy, not mutable user configuration."""
import json
import unittest

from catalog import CATALOG_VERSION, get_template, list_templates


class CatalogTest(unittest.TestCase):
    def test_three_templates_have_stable_roles_and_review_policy(self):
        self.assertEqual(CATALOG_VERSION, 1)
        templates = list_templates()
        self.assertEqual([item["id"] for item in templates], ["quick-task", "research", "development"])
        self.assertEqual([role["id"] for role in templates[0]["roles"]], ["assistant"])
        self.assertEqual([role["id"] for role in templates[1]["roles"]], ["researcher", "reviewer"])
        self.assertEqual([role["id"] for role in templates[2]["roles"]], ["lead", "worker", "reviewer"])
        self.assertEqual([item["independent_review"] for item in templates], [False, True, True])
        for item in templates:
            self.assertEqual(item["version"], 1)
            self.assertIn(item["lead"], [role["id"] for role in item["roles"]])
            for role in item["roles"]:
                self.assertIn(role["kind"], ("controller", "worker", "reviewer"))
                self.assertTrue(role["icon"])
                self.assertTrue(role["scope"])

    def test_catalog_calls_return_detached_copies(self):
        templates = list_templates()
        templates[2]["roles"][0]["scope"] = "Changed by UI"
        templates[1]["acceptance"].clear()
        single = get_template("development")
        self.assertNotEqual(single["roles"][0]["scope"], "Changed by UI")
        single["roles"].clear()
        self.assertEqual(len(get_template("development")["roles"]), 3)
        self.assertTrue(get_template("research")["acceptance"])

    def test_catalog_contains_no_user_paths_accounts_or_permission_overrides(self):
        serialized = json.dumps(list_templates()).lower()
        for value in ("c:/users/", "/home/", "@example.com", "auth.json", "danger-full-access", "approval_policy"):
            self.assertNotIn(value, serialized)
        with self.assertRaises(ValueError):
            get_template("unknown")


if __name__ == "__main__":
    unittest.main()

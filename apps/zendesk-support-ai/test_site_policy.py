"""Site configuration tests using synthetic settings and no external services."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import site_policy
import common


class SitePolicyTests(unittest.TestCase):
    def test_group_resolution_preserves_explicit_numeric_id(self):
        with patch.object(common, "SPECIAL_GROUP_ID_ENV", "42"), \
                patch.object(common, "SPECIAL_GROUP_NAME", "Test Team"):
            group, errors = common.resolve_special_group([])
            self.assertEqual(group, {"id": 42, "name": "Test Team"})
            self.assertEqual(errors, [])
        with patch.object(common, "SPECIAL_GROUP_ID_ENV", "invalid"):
            group, errors = common.resolve_special_group([])
            self.assertIsNone(group)
            self.assertTrue(errors)

    def test_group_resolution_by_name_is_unique(self):
        with patch.object(common, "SPECIAL_GROUP_ID_ENV", ""), \
                patch.object(common, "SPECIAL_GROUP_NAME", "Test Team"):
            groups = [{"id": 42, "name": "Test Team"}, {"id": 43, "name": "Other"}]
            group, errors = common.resolve_special_group(groups)
            self.assertEqual(group["id"], 42)
            self.assertEqual(errors, [])
            group, errors = common.resolve_special_group(groups + [groups[0]])
            self.assertIsNone(group)
            self.assertTrue(errors)

    def test_unconfigured_policy_has_no_site_rules(self):
        with patch.dict(os.environ, {"SUPPORT_AI_SITE_POLICY_FILE": ""}):
            self.assertEqual(site_policy.load_policy(), {})

    def test_invalid_config_stops_without_logging_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            for data in ("not-json", "[]", "{}", '{"support_area_instructions":42}'):
                path.write_text(data)
                with self.subTest(data=data), patch.dict(os.environ, {"SUPPORT_AI_SITE_POLICY_FILE": str(path)}):
                    with self.assertRaises(RuntimeError) as error:
                        site_policy.load_policy()
                    self.assertNotIn(data, str(error.exception))
            path.unlink()
            with patch.dict(os.environ, {"SUPPORT_AI_SITE_POLICY_FILE": str(path)}):
                with self.assertRaises(RuntimeError):
                    site_policy.load_policy()

    def test_policy_controls_schema_and_assignment_identifiers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            path.write_text(json.dumps(dict(special_support_area="dedicated",
                special_agent_tag="test_team", special_group_name="Test Group",
                passkey_support_area="authentication", passkey_agent_tag="test_auth",
                support_area_instructions="Synthetic routing policy")))
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("SUPPORT_AI_", "ZENDESK_", "LLM_"))}
            env["SUPPORT_AI_SITE_POLICY_FILE"] = str(path)
            code = """import llm_client,common,generator
assert llm_client.SUPPORT_AREAS == ['general','dedicated','authentication']
assert common.SPECIAL_SUPPORT_AREA == 'dedicated'
assert common.SPECIAL_AGENT_TAG == 'test_team'
assert common.SPECIAL_GROUP_NAME == 'Test Group'
assert common.PASSKEY_AGENT_TAG == 'test_auth'
assert generator._resolved_support_area({}, {'support_area':'dedicated','support_area_confidence':'high'})[0] == 'dedicated'
"""
            subprocess.run([sys.executable, "-c", code], env=env, check=True)


if __name__ == "__main__":
    unittest.main()

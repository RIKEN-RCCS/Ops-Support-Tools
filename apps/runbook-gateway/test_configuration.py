"""Missing endpoint must stop the CLI before any network call or token read."""
import unittest
from unittest.mock import patch

import runbook_gateway as gateway


class ConfigurationTests(unittest.TestCase):
    def test_missing_endpoint_stops_before_command(self):
        with patch.object(gateway, "DEFAULT_API_URL", ""), \
                patch("sys.argv", ["gateway", "claim"]), \
                patch.object(gateway, "cmd_claim") as claim:
            with self.assertRaises(SystemExit) as error:
                gateway.main()
            self.assertEqual(error.exception.code, 2)
            claim.assert_not_called()

    def test_explicit_endpoint_is_passed_to_command(self):
        with patch("sys.argv", ["gateway", "--api", "https://knowledge.example.com", "claim"]), \
                patch.object(gateway, "cmd_claim") as claim:
            gateway.main()
            self.assertEqual(claim.call_args.args[0].api, "https://knowledge.example.com")


if __name__ == "__main__":
    unittest.main()

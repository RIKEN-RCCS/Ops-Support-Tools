"""Exercise publication gates in disposable repositories, without network calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("check_publication.py")


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.repo = self.directory / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Example")
        self.git("config", "user.email", "author@example.com")
        self.policy = self.directory / "policy.json"
        self.policy.write_text(json.dumps({"deny_patterns": ["internal[.]example[.]invalid"]}))
        self.env = dict(os.environ, PUBLICATION_POLICY_FILE=str(self.policy))

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=self.repo)

    def scan(self, *args, input=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.repo,
                              env=self.env, input=input, text=True, capture_output=True)

    def stage(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self.git("add", "-f", "--", name)

    def test_public_example_passes(self):
        self.stage(".env.example", "SERVICE_URL=https://service.example.com\n")
        self.assertEqual(self.scan("--staged", "--require-policy").returncode, 0)

    def test_private_paths_are_blocked(self):
        for name in (".env", "apps/demo/oauth.env", "apps/demo/config/policy.json", "state.sqlite"):
            with self.subTest(name=name):
                self.stage(name, "synthetic")
                result = self.scan("--staged")
                self.assertEqual(result.returncode, 1)
                self.assertIn("private-path", result.stderr)

    def test_site_rule_and_known_secret_are_blocked_without_values(self):
        self.stage("example.txt", "internal.example.invalid")
        result = self.scan("--staged")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("internal.example.invalid", result.stderr)
        secret = "fixture-" + "private" * 5
        source = self.directory / "key"
        source.write_text(secret)
        self.policy.write_text(json.dumps({"secret_globs": [str(source)]}))
        self.stage("example.txt", secret)
        result = self.scan("--staged")
        self.assertEqual(result.returncode, 1)
        self.assertIn("known-secret-value", result.stderr)
        self.assertNotIn(secret, result.stderr)

    def test_removed_historical_token_is_blocked_by_pre_push(self):
        token = "gh" + "p_" + "A" * 40
        self.stage("example.txt", token)
        self.git("commit", "-qm", "Synthetic fixture")
        (self.repo / "example.txt").unlink()
        self.git("add", "-u")
        self.git("commit", "-qm", "Remove fixture")
        head = self.git("rev-parse", "HEAD").decode().strip()
        result = self.scan("--pre-push", input="refs/heads/main " + head + " refs/heads/main " + "0" * 40 + "\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("provider-token", result.stderr)
        self.assertNotIn(token, result.stderr)

    def test_missing_or_malformed_policy_fails_closed(self):
        self.stage("example.txt", "synthetic")
        self.policy.write_text("invalid-json")
        self.assertEqual(self.scan("--staged", "--require-policy").returncode, 2)
        self.policy.unlink()
        self.assertEqual(self.scan("--staged", "--require-policy").returncode, 2)

    def test_private_path_shared_blob_is_not_missed(self):
        self.stage("public.txt", "same contents")
        self.git("commit", "-qm", "Public fixture")
        self.stage("apps/demo/secrets/key", "same contents")
        self.git("commit", "-qm", "Private path fixture")
        result = self.scan("--ref", "HEAD")
        self.assertEqual(result.returncode, 1)
        self.assertIn("private-path", result.stderr)

    def test_literal_credential_without_provider_prefix_is_blocked(self):
        value = "B" * 32
        self.stage("example.py", "CLIENT_" + "SECRET = '" + value + "'\n")
        result = self.scan("--staged")
        self.assertEqual(result.returncode, 1)
        self.assertIn("credential-literal", result.stderr)
        self.assertNotIn(value, result.stderr)

    def test_sensitive_filename_is_blocked_and_redacted(self):
        self.stage("internal.example.invalid.txt", "synthetic")
        result = self.scan("--staged")
        self.assertEqual(result.returncode, 1)
        self.assertIn("redacted-path", result.stderr)
        self.assertNotIn("internal.example.invalid", result.stderr)

    def test_sensitive_commit_message_is_blocked_without_values(self):
        self.stage("public.txt", "synthetic")
        self.git("commit", "-qm", "internal.example.invalid")
        result = self.scan("--ref", "HEAD")
        self.assertEqual(result.returncode, 1)
        self.assertIn("commit-message", result.stderr)
        self.assertNotIn("internal.example.invalid", result.stderr)


if __name__ == "__main__":
    unittest.main()

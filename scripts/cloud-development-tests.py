"""Behavioral checks for the cloud candidate security boundary; no network/API use."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("cloud_development", Path(__file__).with_name("cloud-development.py"))
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        def git(*args, **kwargs):
            return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, **kwargs).stdout

        self.git_mock = patch.object(cloud, "git", side_effect=git)
        self.git_mock.start()
        git("init", "-q")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (self.root / "apps/web/src").mkdir(parents=True)
        (self.root / "apps/web/src/App.tsx").write_text("export const title = 'Before';\n")
        git("add", "--all")
        git("commit", "-qm", "baseline")
        self.base = git("rev-parse", "HEAD").decode().strip()

    def tearDown(self):
        self.git_mock.stop()
        self.temp.cleanup()

    def stage(self, name, content):
        destination = self.root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
        cloud.git("add", "--all")

    def test_supported_text_round_trip(self):
        self.stage("apps/web/src/App.tsx", "export const title = 'After';\n")
        candidate = cloud.validate_index()
        cloud.git("reset", "--hard", self.base)
        patch_file = self.root.parent / (self.root.name + ".patch")
        try:
            patch_file.write_bytes(candidate)
            cloud.apply(self.base, patch_file)
            self.assertIn("After", (self.root / "apps/web/src/App.tsx").read_text())
        finally:
            patch_file.unlink(missing_ok=True)

    def test_backend_api_and_release_controls_are_rejected(self):
        for name in ["apps/agent/office/agent.py", "apps/web/src/api.ts", ".github/workflows/release.yml", "scripts/local-demo.py", "config/office.json", "package.json", "packages/contracts/src/index.ts"]:
            with self.subTest(name=name):
                cloud.git("reset", "--hard", self.base)
                cloud.git("clean", "-fd")
                self.stage(name, "protected\n")
                with self.assertRaisesRegex(ValueError, "protected"):
                    cloud.validate_index()

    def test_symlink_and_executable_are_rejected(self):
        destination = self.root / "apps/web/src/leak.ts"
        destination.symlink_to("/etc/passwd")
        cloud.git("add", "--all")
        with self.assertRaisesRegex(ValueError, "symlink"):
            cloud.validate_index()
        cloud.git("reset", "--hard", self.base)
        cloud.git("clean", "-fd")
        self.stage("apps/web/src/leak.ts", "example\n")
        cloud.git("update-index", "--chmod=+x", "apps/web/src/leak.ts")
        with self.assertRaisesRegex(ValueError, "executable"):
            cloud.validate_index()

    def test_secret_and_binary_content_are_rejected(self):
        self.stage("apps/web/src/leak.ts", "sk-ant-" + "x" * 24 + "\n")
        with self.assertRaisesRegex(ValueError, "Credential"):
            cloud.validate_index()
        self.stage("apps/web/src/leak.ts", "binary\0text\n")
        with self.assertRaisesRegex(ValueError, "Binary"):
            cloud.validate_index()

    def test_no_changes_and_stale_base_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "between 1"):
            cloud.validate_index()
        with self.assertRaisesRegex(ValueError, "expected base"):
            cloud.validate_base("0" * 40)

    def test_request_ids_cannot_inject_shell_or_outputs(self):
        for value in ["a\nbase=other", "$(echo bad)", "a/b", "a" * 81, "nø"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                cloud.validate_id(value)
        self.assertEqual(cloud.validate_id("demo_12-ab"), "demo_12-ab")


if __name__ == "__main__":
    unittest.main()

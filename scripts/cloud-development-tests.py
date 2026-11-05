"""Behavioral checks for the cloud candidate security boundary; no network/API use."""

import importlib.util
import copy
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
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

    def test_backend_feature_api_registration_and_test_round_trip(self):
        files = {
            "apps/agent/office/temperature_monitor.py": (
                "def temperature_changed(previous, current, threshold=2):\n"
                "    return abs(current - previous) >= threshold\n"
            ),
            "apps/agent/office/api.py": (
                "from fastapi import APIRouter\n"
                "from office.temperature_monitor import temperature_changed\n"
                "router = APIRouter()\n"
                "@router.get('/api/temperature-change')\n"
                "async def temperature_change(previous: float, current: float):\n"
                "    return {'changed': temperature_changed(previous, current)}\n"
            ),
            "apps/agent/tests/test_temperature_monitor.py": (
                "from office.temperature_monitor import temperature_changed\n"
                "def test_temperature_change_requires_threshold():\n"
                "    assert temperature_changed(20, 22)\n"
                "    assert not temperature_changed(20, 21)\n"
            ),
            "apps/agent/office/conversation.py": "# Register a read-only temperature monitoring tool here.\n",
            "apps/procurement/office_procurement/worker.py": "# An ordinary discovery feature change.\n",
            "apps/procurement/tests/test_temperature_supply.py": "def test_supply_feature():\n    assert True\n",
            "apps/web/src/api.ts": "export const temperatureChange = '/api/temperature-change';\n",
        }
        for name, content in files.items():
            self.stage(name, content)
        candidate = cloud.validate_index()
        cloud.git("reset", "--hard", self.base)
        patch_file = self.root.parent / (self.root.name + "-backend.patch")
        try:
            patch_file.write_bytes(candidate)
            cloud.apply(self.base, patch_file)
            for name, content in files.items():
                self.assertEqual((self.root / name).read_text(), content)
        finally:
            patch_file.unlink(missing_ok=True)

    def test_approval_credential_release_dependency_and_migration_controls_are_rejected(self):
        names = sorted(cloud.PROTECTED_FILES) + [
            ".github/workflows/release.yml", "scripts/local-demo.py", "scripts/cloud-development.py",
            "config/office.json", "package.json", "package-lock.json", "apps/agent/pyproject.toml",
            "apps/agent/uv.lock", "apps/procurement/pyproject.toml", "packages/contracts/src/index.ts",
            "apps/device/firmware/main.py", "apps/agent/office/migrations/001_temperature.py",
            "apps/agent/office/temperature_migration.py", "apps/agent/office/auth/new.py",
            "apps/web/src/auth.test.ts", "apps/agent/office/credentials.py", "apps/agent/tests/conftest.py",
        ]
        for name in names:
            with self.subTest(name=name):
                cloud.git("reset", "--hard", self.base)
                cloud.git("clean", "-fd")
                self.stage(name, "protected\n")
                with self.assertRaisesRegex(ValueError, "protected"):
                    cloud.validate_index()

    def test_patch_apply_cannot_bypass_protected_path_guard(self):
        # Construct a valid Git patch without calling validate_index, as if an
        # untrusted artifact paired a legitimate feature with a control edit.
        self.stage("apps/agent/office/temperature_monitor.py", "def current():\n    return 20\n")
        self.stage("apps/agent/office/development_api.py", "# forbidden control edit\n")
        candidate = cloud.git("diff", "--cached", "--binary", "--full-index")
        cloud.git("reset", "--hard", self.base)
        patch_file = self.root.parent / (self.root.name + "-malicious.patch")
        try:
            patch_file.write_bytes(candidate)
            with self.assertRaisesRegex(ValueError, "protected"):
                cloud.apply(self.base, patch_file)
        finally:
            patch_file.unlink(missing_ok=True)

    def test_noncanonical_and_hidden_paths_cannot_bypass_scope(self):
        for name in [
            "apps/agent/office/../office/development.py", "apps/agent/office//temperature.py",
            "/apps/agent/office/temperature.py", "apps/agent/office/.env.py", "apps/agent/office/feature/.git/config.py",
            "apps/agent/office/feature\\development.py", "apps/agent/office/temperature.py\nconfig.py",
        ]:
            with self.subTest(name=name):
                self.assertFalse(cloud.allowed_path(name))

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


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base, self.head, self.tree = "b" * 40, "a" * 40, "c" * 40
        self.repo = "owner/repo"
        self.pr = {"number": 17, "state": "open", "merged": False, "merged_at": None,
                   "draft": False, "title": "Claude development request123", "user": {"login": "github-actions[bot]"},
                   "head": {"ref": "NLS/claude-123-1", "sha": self.head, "repo": {"full_name": self.repo}},
                   "base": {"ref": "main", "sha": self.base, "repo": {"full_name": self.repo}}}
        self.calls = []
        self.merged = False
        self.env = patch.dict(os.environ, {
            "GITHUB_REPOSITORY": self.repo, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
            "VALIDATION_RESULT": "success", "GITHUB_OUTPUT": self.temp.name + "/output",
            "GITHUB_STEP_SUMMARY": self.temp.name + "/summary",
            "RUNNER_TEMP": self.temp.name,
        })
        self.env.start()
        self.base_patch = patch.object(cloud, "validate_base")
        self.base_patch.start()
        self.api_patch = patch.object(cloud, "api", side_effect=self.fake_api)
        self.mock_api = self.api_patch.start()

    def tearDown(self):
        self.api_patch.stop()
        self.base_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def fake_api(self, path, *, method="GET", body=None):
        self.calls.append((path, method, body))
        if method == "POST" and path.endswith("/check-runs"):
            return {"id": 1}
        if method == "PATCH":
            self.merged = True
            return {"object": {"sha": self.head}}
        if path.endswith("/jobs?per_page=100"):
            return {"jobs": [{"name": "Validate candidate", "status": "completed", "conclusion": "success"}]}
        if path == "repos/owner/repo":
            return {"default_branch": "main"}
        if path.endswith("/pulls/17"):
            pr = copy.deepcopy(self.pr)
            if self.merged:
                pr.update(merged=True, merged_at="2026-11-05T12:00:00Z", state="closed")
            return pr
        if path.endswith("/commits/main"):
            return {"sha": self.base}
        if path.endswith("/commits/" + self.head):
            return {"parents": [{"sha": self.base}], "commit": {"tree": {"sha": self.tree}}}
        self.fail("Unexpected mocked API path")

    def merge(self, **overrides):
        values = dict(base=self.base, head_sha=self.head, number="17", request_id="request123",
                      auto_merge=True, tree_sha=self.tree)
        values.update(overrides)
        cloud.merge(**values)

    def writes(self):
        return [call for call in self.calls if call[1] != "GET"]

    def test_auto_merge_is_false_by_default_and_strictly_typed(self):
        self.assertIs(cloud.parse_auto_merge(), False)
        self.assertIs(cloud.parse_auto_merge("false"), False)
        self.assertIs(cloud.parse_auto_merge("true"), True)
        self.assertIs(cloud.parse_auto_merge(True), True)
        for value in [None, "yes", "True", "1", "false\n", 1, 0, [], {}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                cloud.parse_auto_merge(value)
        with self.assertRaisesRegex(ValueError, "did not authorize"):
            self.merge(auto_merge=False)
        self.assertEqual(self.calls, [])

    def test_publish_is_draft_by_default_and_ready_only_when_authorized(self):
        def fake_git(*args, **kwargs):
            return (self.tree + "\n").encode() if args[0] == "write-tree" else (self.head + "\n").encode() if args[0] == "rev-parse" else b""
        for auto_merge in [False, True]:
            with self.subTest(auto_merge=auto_merge), patch.object(cloud, "apply"), patch.object(cloud, "git", side_effect=fake_git), patch.object(cloud.subprocess, "run", return_value=SimpleNamespace(stdout="https://github.com/owner/repo/pull/17")) as run:
                cloud.publish(self.base, "unused.patch", "request123", auto_merge=auto_merge, tree_sha=self.tree)
                command = run.call_args.args[0]
                self.assertEqual("--draft" in command, not auto_merge)
                body = (Path(self.temp.name) / "development-pr-body.md").read_text()
                self.assertIn("explicitly authorized checked automatic merge" if auto_merge else "review-only", body)

    def test_publish_refuses_candidate_tree_different_from_checked_tree(self):
        with patch.object(cloud, "apply"), patch.object(cloud, "git", return_value=("0" * 40).encode()):
            with self.assertRaisesRegex(ValueError, "differs from the successfully validated tree"):
                cloud.publish(self.base, "unused.patch", "request123", auto_merge=True, tree_sha=self.tree)
        self.assertEqual(self.calls, [])

    def test_exact_checked_sha_is_attested_then_fast_forwarded_without_force(self):
        self.merge()
        self.assertEqual(self.writes(), [
            ("repos/owner/repo/check-runs", "POST", {
                "name": "Office checks", "head_sha": self.head, "status": "completed", "conclusion": "success",
                "details_url": "https://github.com/owner/repo/actions/runs/123/attempts/1",
                "external_id": "cloud-development:123:1:" + self.head,
                "output": {"title": "Hosted Claude candidate checks passed",
                           "summary": "Scope guard, type checks, frontend build, backend/device/procurement tests and unchanged shared-schema checks passed for this exact tree in the linked workflow attempt."},
            }),
            ("repos/owner/repo/git/refs/heads/main", "PATCH", {"sha": self.head, "force": False}),
        ])
        self.assertIn("merge_sha=" + self.head, Path(os.environ["GITHUB_OUTPUT"]).read_text())
        self.assertIn("merged by non-forced fast-forward", Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text())

    def test_changed_head_or_pr_identity_cannot_merge(self):
        for change in ["sha", "branch", "repo", "title", "draft"]:
            original = copy.deepcopy(self.pr)
            if change == "sha":
                self.pr["head"]["sha"] = "0" * 40
            elif change == "branch":
                self.pr["head"]["ref"] = "NLS/claude-other"
            elif change == "repo":
                self.pr["head"]["repo"]["full_name"] = "other/repo"
            elif change == "title":
                self.pr["title"] = "Unrelated PR"
            else:
                self.pr["draft"] = True
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "PR identity"):
                self.merge()
            self.assertEqual(self.writes(), [])
            self.pr = original

    def test_changed_base_or_candidate_tree_cannot_merge(self):
        original = self.fake_api
        for change in ["base", "tree", "parents"]:
            def response(path, **kwargs):
                result = original(path, **kwargs)
                if change == "base" and path.endswith("/commits/main"):
                    return {"sha": "0" * 40}
                if change == "tree" and path.endswith("/commits/" + self.head):
                    result["commit"]["tree"]["sha"] = "0" * 40
                if change == "parents" and path.endswith("/commits/" + self.head):
                    result["parents"].append({"sha": "0" * 40})
                return result
            self.mock_api.side_effect = response
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.merge()
            self.assertEqual(self.writes(), [])

    def test_failed_or_unconfirmed_same_run_validation_cannot_merge(self):
        with patch.dict(os.environ, {"VALIDATION_RESULT": "failure"}):
            with self.assertRaisesRegex(ValueError, "validation did not succeed"):
                self.merge()
        self.assertEqual(self.calls, [])
        original = self.fake_api
        def response(path, **kwargs):
            result = original(path, **kwargs)
            if path.endswith("/jobs?per_page=100"):
                result["jobs"][0]["conclusion"] = "failure"
            return result
        self.mock_api.side_effect = response
        with self.assertRaisesRegex(ValueError, "no successful"):
            self.merge()
        self.assertEqual(self.writes(), [])

    def test_uncertain_ref_update_is_never_retried(self):
        original = self.fake_api
        def response(path, **kwargs):
            result = original(path, **kwargs)
            if kwargs.get("method") == "PATCH":
                raise TimeoutError("connection dropped after possible acceptance")
            return result
        self.mock_api.side_effect = response
        with self.assertRaisesRegex(ValueError, "do not retry automatically"):
            self.merge()
        self.assertEqual(len([call for call in self.calls if call[1] == "PATCH"]), 1)
        self.assertIn("no confirmed result", Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text())

    def test_workflow_merge_depends_on_successful_validation_and_publish(self):
        workflow = Path(__file__).parents[1] / ".github/workflows/claude-development.yml"
        text = workflow.read_text()
        merge_job = text.split("\n  merge:\n", 1)[1]
        self.assertIn("needs: [implement, validate, publish]", merge_job)
        self.assertIn("needs.validate.result == 'success'", merge_job)
        self.assertIn("needs.publish.result == 'success'", merge_job)
        self.assertIn("needs.validate.outputs.tree_sha", merge_job)
        self.assertNotIn("download-artifact", merge_job)


if __name__ == "__main__":
    unittest.main()

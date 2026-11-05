"""Release control behavior; no GitHub calls, model calls, app starts or purchases."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "local_demo.py"
SPEC = importlib.util.spec_from_file_location("local_demo", SOURCE)
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)
OLD = "a" * 40
NEW = "b" * 40


def check(sha=NEW, **extra):
    return {"id": 1, "name": "Office checks", "head_sha": sha,
            "app": {"id": 15368}, "status": "completed", "conclusion": "success", **extra}


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_approval_requires_full_exact_sha(self):
        for value in ["main", NEW[:12], NEW + "; echo bad", NEW.upper()]:
            with self.assertRaises(demo.UpdateError):
                demo.require_sha(value)
        self.assertEqual(demo.require_sha(NEW), NEW)

    def test_pr_must_be_merged_to_this_main_at_exact_approved_sha(self):
        valid = {"merged": True, "merge_commit_sha": NEW,
                 "base": {"ref": "main", "repo": {"full_name": demo.REPOSITORY}}}
        with patch.object(demo, "github", return_value=valid):
            self.assertEqual(demo.verify_pr(self.root, 5, NEW), valid)
            with self.assertRaisesRegex(demo.UpdateError, "differs"):
                demo.verify_pr(self.root, 5, OLD)
        for change in [{"merged": False}, {"base": {"ref": "feature"}}]:
            with patch.object(demo, "github", return_value=valid | change):
                with self.assertRaises(demo.UpdateError):
                    demo.verify_pr(self.root, 5, NEW)

    def test_checks_require_exact_sha_trusted_app_and_completed_success(self):
        with patch.object(demo, "github", return_value={"check_runs": [check()]}):
            demo.verify_checks(self.root, NEW)
        for invalid in [check(OLD), check(app={"id": 1}), check(status="in_progress"), check(conclusion="failure")]:
            with patch.object(demo, "github", return_value={"check_runs": [invalid]}):
                with self.assertRaises(demo.UpdateError):
                    demo.verify_checks(self.root, NEW)

    def test_failed_new_attempt_invalidates_previous_passing_check(self):
        with patch.object(demo, "github", return_value={"check_runs": [check(), check(id=2, conclusion="failure")]}):
            with self.assertRaises(demo.UpdateError):
                demo.verify_checks(self.root, NEW)

    def test_checks_are_paginated_and_other_failures_block_release(self):
        first_page = [check(name=f"other-{i}", id=i + 1) for i in range(100)]
        with patch.object(demo, "github", side_effect=[{"check_runs": first_page}, {"check_runs": [check()]}]) as api:
            demo.verify_checks(self.root, NEW)
            self.assertIn("page=2", api.call_args.args[0])
        with patch.object(demo, "github", return_value={"check_runs": [check(), check(name="Security", conclusion="failure")]}):
            with self.assertRaises(demo.UpdateError):
                demo.verify_checks(self.root, NEW)

    def test_sensitive_paths_need_manual_installation(self):
        demo.safe_changes(["apps/web/src/App.tsx", "apps/agent/office/help.py", "docs/help.md"])
        for path in [".github/workflows/validate.yml", "scripts/local_demo.py", "config/office.json",
                     "apps/agent/office/db.py", "apps/agent/office/dispatch.py", "apps/agent/uv.lock",
                     "apps/web/package.json", "apps/agent/migrations/001.py", "data/new.sql",
                     "apps/device/src/bridge.py", ".claude/settings.json", ".npmrc"]:
            with self.subTest(path=path), self.assertRaises(demo.UpdateError):
                demo.safe_changes([path])

    def test_dirty_checkout_is_refused_without_discarding_changes(self):
        with patch.object(demo, "command", return_value=" M apps/web/src/App.tsx") as command:
            with self.assertRaisesRegex(demo.UpdateError, "local changes"):
                demo.clean(self.root)
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[0][1], "status")

    def test_stale_approval_cannot_prepare_or_select_release(self):
        manifest = {"sha": OLD, "path": str(self.root), "bootstrap": True}
        path = demo.state_dir(self.root) / "release.json"
        demo.write_json(path, manifest)
        with patch.object(demo, "clean"), patch.object(demo, "verify_release"), \
             patch.object(demo, "verify_pr", side_effect=demo.UpdateError("stale")), \
             patch.object(demo, "prepare") as prepare:
            with self.assertRaisesRegex(demo.UpdateError, "stale"):
                demo.update(self.root, 5, NEW)
            prepare.assert_not_called()
        self.assertEqual(demo.read_json(path), manifest)

    def test_failed_health_rolls_back_code_selection_and_preserves_database(self):
        current = {"sha": OLD, "path": str(self.root), "bootstrap": True}
        candidate = {"sha": NEW, "path": str(self.root / "candidate"), "previous": current}
        directory = demo.state_dir(self.root)
        database = self.root / ".data" / "agent.sqlite"
        database.write_bytes(b"existing persistent runtime state")
        demo.write_json(directory / "release.json", candidate)
        started = []
        def starter(_root, release):
            started.append(release["sha"])
            if release["sha"] == NEW:
                raise demo.UpdateError("unhealthy")
            return ["restored-process"]
        active, processes = demo.activate(self.root, candidate, current, [], starter=starter)
        self.assertEqual(started, [NEW, OLD])
        self.assertEqual(active, current)
        self.assertEqual(processes, ["restored-process"])
        self.assertEqual(demo.read_json(directory / "release.json"), current)
        self.assertFalse(demo.read_json(directory / "status.json")["database_restored"])
        self.assertEqual(database.read_bytes(), b"existing persistent runtime state")

    def test_missing_approved_manifest_never_starts_application(self):
        with patch.object(demo, "start") as start:
            with self.assertRaisesRegex(demo.UpdateError, "approved release"):
                demo.run(self.root)
            start.assert_not_called()

    def test_source_integrity_checks_detect_local_edits_and_unexpected_files(self):
        subprocess.run(["git", "init", "-q", self.root], check=True)
        (self.root / "main.py").write_text("print('approved')\n")
        subprocess.run(["git", "-C", self.root, "add", "main.py"], check=True)
        subprocess.run(["git", "-C", self.root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "initial"], check=True)
        sha = demo.command(["git", "rev-parse", "HEAD"], self.root)
        release = self.root / "release"
        release.mkdir()
        source = release / "main.py"
        source.write_text("print('approved')\n")
        demo.verify_source(self.root, release, sha)
        source.write_text("print('changed after approval')\n")
        with self.assertRaisesRegex(demo.UpdateError, "differs"):
            demo.verify_source(self.root, release, sha)
        source.write_text("print('approved')\n")
        (release / "unexpected.py").write_text("print('unexpected')\n")
        with self.assertRaisesRegex(demo.UpdateError, "unexpected source"):
            demo.verify_source(self.root, release, sha)


if __name__ == "__main__":
    unittest.main()

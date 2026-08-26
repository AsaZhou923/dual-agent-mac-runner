from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "scripts" / "mac-runner-self-update-helper"
LOADER = importlib.machinery.SourceFileLoader("mac_runner_self_update_helper", str(HELPER_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
helper = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(helper)


class SelfUpdateHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temporary.name)
        self.app = self.root / "app"
        self.candidate = self.root / "candidate"
        self.app.mkdir()
        self.candidate.mkdir()
        self.base = "a" * 40
        self.target = "b" * 40
        (self.app / "runner.py").write_text("print('old runner')\n", encoding="utf-8")
        (self.app / ".source-commit").write_text(f"{self.base}\n", encoding="utf-8")
        self.config = self.app / "config.toml"
        self.config.write_text("[runner]\n", encoding="utf-8")
        os.chmod(self.config, 0o600)
        (self.candidate / ".source-commit").write_text(f"{self.target}\n", encoding="utf-8")
        self.db = self.root / "state" / "runner.db"
        self.db.parent.mkdir()
        connection = sqlite3.connect(self.db)
        connection.execute("CREATE TABLE proof(value TEXT NOT NULL)")
        connection.execute("INSERT INTO proof VALUES ('old')")
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _plan(self) -> dict[str, object]:
        return {
            "schema": "mac-runner/self-update-plan-v1",
            "job_id": "self-update-helper-test",
            "attempt": 1,
            "base_sha": self.base,
            "target_sha": self.target,
            "app_dir": str(self.app),
            "candidate_dir": str(self.candidate),
            "candidate_sha256": helper.tree_sha256(self.candidate),
            "backup_dir": str(self.root / "rollback-app"),
            "db_backup_dir": str(self.root / "rollback-db"),
            "config_path": str(self.config),
            "config_sha256": hashlib.sha256(self.config.read_bytes()).hexdigest(),
            "db_path": str(self.db),
            "result_path": str(self.root / "helper-result.json"),
            "service_label": "com.example.mac-runner",
            "before_pid": 111,
            "source_marker": str(self.app / ".source-commit"),
            "helper_sha256": hashlib.sha256(HELPER_PATH.read_bytes()).hexdigest(),
            "runtime_path": str(Path(sys.executable).resolve()),
            "runtime_sha256": hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
            "runtime_version": platform.python_version(),
            "created_at": "2026-08-25T12:00:00+00:00",
        }

    def _run_with_launchctl(self, plan: dict[str, object]) -> dict[str, object]:
        real_run = subprocess.run

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args and args[0] == "launchctl":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args and args[0] == "/bin/ps":
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=(
                        f"{Path(sys.executable).resolve()} {self.app.resolve() / 'runner.py'} "
                        f"--config {self.config.resolve()} serve\n"
                    ),
                    stderr="",
                )
            return real_run(args, **kwargs)

        with mock.patch.object(helper.subprocess, "run", side_effect=fake_run), mock.patch.object(
            helper, "launchctl_pid", return_value=222
        ):
            return helper.swap_app(plan)

    def test_atomic_swap_preserves_config_mode_and_verifies_new_status(self) -> None:
        (self.candidate / "runner.py").write_text(
            "import json, sys\n"
            "payload = ({'status': 'VERIFYING', 'payload': {'target_sha': '"
            + self.target
            + "'}} if 'get' in sys.argv else {'runner': {'self_update': {'source_marker': '"
            + self.target
            + "', 'ready': True}}, 'queue': {'pending': 0, 'running': 1, 'retryable': 0}, "
            "'git': {'worktrees': 0, 'dirty_outside_jobs': False}})\n"
            "print(json.dumps(payload))\n",
            encoding="utf-8",
        )
        plan = self._plan()
        result = self._run_with_launchctl(plan)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["source_marker"], self.target)
        self.assertEqual((self.app / ".source-commit").read_text(encoding="utf-8").strip(), self.target)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.root / "rollback-app" / "runner.py").is_file())
        self.assertEqual(helper.sqlite_integrity(self.db), "ok")

    def test_failed_new_status_restores_old_app_and_database(self) -> None:
        (self.candidate / "runner.py").write_text("raise SystemExit(2)\n", encoding="utf-8")
        plan = self._plan()
        with mock.patch.object(helper, "RUNNER_STATUS_RETRY_SECONDS", 0):
            result = self._run_with_launchctl(plan)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_stage"], "runner_status_command")
        self.assertIn("new runner status command failed rc=2", result["error_message"])
        self.assertTrue(result["rollback_verified"])
        self.assertEqual((self.app / "runner.py").read_text(encoding="utf-8"), "print('old runner')\n")
        connection = sqlite3.connect(self.db)
        try:
            self.assertEqual(connection.execute("SELECT value FROM proof").fetchone()[0], "old")
        finally:
            connection.close()

    def test_transient_new_status_failure_is_retried(self) -> None:
        (self.candidate / "runner.py").write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "counter = Path(__file__).with_name('status-attempted')\n"
            "if 'status' in sys.argv and not counter.exists():\n"
            "    counter.write_text('1', encoding='utf-8')\n"
            "    raise SystemExit(2)\n"
            "payload = ({'status': 'VERIFYING', 'payload': {'target_sha': '"
            + self.target
            + "'}} if 'get' in sys.argv else {'runner': {'self_update': {'source_marker': '"
            + self.target
            + "', 'ready': True}}, 'queue': {'pending': 0, 'running': 1, 'retryable': 0}, "
            "'git': {'worktrees': 0, 'dirty_outside_jobs': False}})\n"
            "print(json.dumps(payload))\n",
            encoding="utf-8",
        )
        plan = self._plan()
        with mock.patch.object(helper, "RUNNER_STATUS_RETRY_INTERVAL", 0):
            result = self._run_with_launchctl(plan)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertTrue((self.app / "status-attempted").is_file())

    def test_candidate_tree_hash_rejects_symlinks_and_covers_file_mode(self) -> None:
        regular = self.candidate / "runner.py"
        regular.write_text("print('candidate')\n", encoding="utf-8")
        os.chmod(regular, 0o644)
        first = helper.tree_sha256(self.candidate)
        os.chmod(regular, 0o755)
        self.assertNotEqual(first, helper.tree_sha256(self.candidate))
        regular.unlink()
        regular.symlink_to(self.app / "runner.py")
        with self.assertRaisesRegex(RuntimeError, "unsafe file"):
            helper.tree_sha256(self.candidate)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import datetime as dt
import http.server
import json
import os
import signal
import sqlite3
import socketserver
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import runner


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


class OllamaHandler(http.server.BaseHTTPRequestHandler):
    mode = "tool_loop"

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/api/tags":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"models": [{"name": "ornith-9b-agent:latest"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        payload = json.loads(body.decode("utf-8"))
        assert payload["think"] is False
        messages = payload["messages"]
        if self.mode == "slow":
            time.sleep(2)
        if self.mode == "raw_tool_call":
            response = {"message": {"content": "<tool_call>{}</tool_call>"}}
        elif self.mode == "path_escape_call":
            response = {
                "message": {
                    "content": "",
                    "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "../secret.txt"}}}],
                }
            }
        elif self.mode == "tool_loop":
            if any(message.get("role") == "tool" for message in messages):
                response = {
                    "message": {
                        "content": json.dumps(
                            {
                                "findings": [
                                    {"severity": "info", "title": "README checked", "detail": "Observed README through tool call"}
                                ]
                            }
                        ),
                        "tool_calls": [],
                    }
                }
            else:
                response = {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "read_file",
                                    "arguments": {"path": "README.md", "max_chars": 200},
                                }
                            }
                        ],
                    }
                }
        elif self.mode == "tool_budget":
            if "tools" in payload:
                response = {
                    "message": {
                        "content": "",
                        "tool_calls": [{"function": {"name": "repo_status", "arguments": {}}}],
                    }
                }
            else:
                assert len(messages) == 1
                assert messages[-1]["role"] == "user"
                assert "tool budget is exhausted" in messages[-1]["content"]
                assert "Bounded readonly tool evidence" in messages[-1]["content"]
                assert len(messages[-1]["content"]) < 20000
                assert payload["format"] == runner.WORKER_SCHEMA
                response = {
                    "message": {
                        "content": json.dumps(
                            {
                                "findings": [
                                    {
                                        "severity": "info",
                                        "title": "Tool budget summarized",
                                        "detail": "Returned final JSON after tools were disabled",
                                    }
                                ]
                            }
                        )
                    }
                }
        elif self.mode == "context_direct":
            assert "Do not call tools for this canary." in messages[-1]["content"]
            assert "Task context" in messages[-1]["content"]
            response = {
                "message": {
                    "content": json.dumps(
                        {
                            "findings": [
                                {
                                    "severity": "info",
                                    "title": "Context honored",
                                    "detail": "Returned findings without a tool call",
                                }
                            ]
                        }
                    ),
                    "tool_calls": [],
                }
            }
        else:
            raise AssertionError(f"unknown mode {self.mode}")
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


class RunnerTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        OllamaHandler.mode = "tool_loop"
        self.original_env = os.environ.copy()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.real_home = self.root / "real-home"
        (self.real_home / ".ssh").mkdir(parents=True)
        (self.real_home / ".codex").mkdir(parents=True)
        (self.real_home / ".ssh" / "secret.txt").write_text("top-secret\n", encoding="utf-8")
        os.environ["HOME"] = str(self.real_home)
        os.environ["OPENAI_API_KEY"] = "secret-openai"
        os.environ["CODEX_API_KEY"] = "secret-codex"

        self.app_dir = self.root / "app"
        self.app_dir.mkdir()
        self.profiles_dir = self.app_dir / "profiles"
        self.profiles_dir.mkdir()
        self._copy_fixture("job_schema.json")
        self._copy_fixture("result_schema.json")
        self._copy_fixture("runner.py")
        (self.app_dir / ".source-commit").write_text("uninitialized\n", encoding="utf-8")
        self.helper_tempdir = tempfile.TemporaryDirectory(dir="/tmp")
        self.external_helper = Path(self.helper_tempdir.name) / "external-helper.py"
        self._write_file(self.external_helper, "#!/usr/bin/env python3\n")
        self.external_helper.chmod(0o755)

        self.codex_log = self.root / "codex-log.jsonl"
        self.fake_codex = self.app_dir / "fake_codex.py"
        self._write_file(self.fake_codex, self._fake_codex_script())
        self.fake_codex.chmod(0o755)

        self.server, self.endpoint = self._start_server()
        self.origin = self.root / "origin.git"
        self.repo = self.root / "repo"
        self._init_repo()

        self._write_file(self.profiles_dir / "review-readonly.toml", 'command = ["/usr/bin/true"]\ntimeout_seconds = 30\n')
        self._write_file(self.profiles_dir / "backend-unit.toml", 'command = ["/usr/bin/true"]\ntimeout_seconds = 30\n')
        self._write_file(
            self.profiles_dir / "git-sync-verify.toml",
            'command = ["/usr/bin/git", "status", "--porcelain=v1", "--untracked-files=all"]\ntimeout_seconds = 30\n',
        )
        self._write_file(
            self.profiles_dir / "self-update-runner.toml",
            'command = ["/usr/bin/true"]\ntimeout_seconds = 30\n',
        )

        self.config_path = self._write_config()
        self.config = runner.RunnerConfig.load(self.config_path)
        self.sample_job = self._job()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_env)
        self.server.shutdown()
        self.server.server_close()
        self.helper_tempdir.cleanup()
        self.tempdir.cleanup()

    def _copy_fixture(self, name: str) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        source = repository_root / "schemas" / name if name.endswith("_schema.json") else repository_root / name
        target = self.app_dir / name
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    def _write_file(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    def _start_server(self) -> tuple[ReusableTCPServer, str]:
        server = ReusableTCPServer(("127.0.0.1", 0), OllamaHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        return server, f"http://{host}:{port}"

    def _run(self, *args: str, cwd: Path | None = None, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=cwd, env=env, check=check, capture_output=True, text=True)

    def _git(self, repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
        return self._run("git", "-C", str(repo), *args, env=env).stdout.strip()

    def _init_repo(self) -> None:
        self._run("git", "init", "--bare", "--initial-branch=main", str(self.origin))
        self._run("git", "clone", str(self.origin), str(self.repo))
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        (self.repo / "README.md").write_text("head\n", encoding="utf-8")
        self._run("git", "-C", str(self.repo), "add", "README.md", env=env)
        self._run("git", "-C", str(self.repo), "commit", "-m", "base", env=env)
        self.base_sha = self._git(self.repo, "rev-parse", "HEAD")
        self._run("git", "-C", str(self.repo), "push", "origin", "HEAD", env=env)
        (self.repo / "README.md").write_text("target\n", encoding="utf-8")
        self._run("git", "-C", str(self.repo), "commit", "-am", "target", env=env)
        self.target_sha = self._git(self.repo, "rev-parse", "HEAD")
        self._run("git", "-C", str(self.repo), "push", "origin", "HEAD", env=env)

    def _commit_repo_file(self, relative_path: str, content: str, message: str) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        target = self.repo / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._run("git", "-C", str(self.repo), "add", relative_path, env=env)
        self._run("git", "-C", str(self.repo), "commit", "-m", message, env=env)
        sha = self._git(self.repo, "rev-parse", "HEAD")
        self._run("git", "-C", str(self.repo), "push", "origin", "HEAD", env=env)
        return sha

    def _prepare_sync_repo(self) -> None:
        self._run("git", "-C", str(self.repo), "checkout", "main")
        self._run("git", "-C", str(self.repo), "reset", "--hard", self.base_sha)

    def _write_config(
        self,
        *,
        allow_write_tasks: bool = True,
        require_source_metadata: bool = False,
        model: str = "",
        max_diff_bytes: int = 4096,
        owner_pubkey: str = "",
        extra_capabilities: str = "",
        capability_bindings: str = "",
        repo_prepare_config: str = "",
        sensitive_paths: str = '[".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "credentials/*", "secrets/*"]',
    ) -> Path:
        config_path = self.app_dir / "config.toml"
        config = textwrap.dedent(
            f"""
            [runner]
            state_dir = "{self.root / 'state'}"
            app_dir = "{self.app_dir}"
            db_path = "{self.root / 'state' / 'runner.db'}"
            worktree_root = "{self.root / 'share' / 'worktrees'}"
            artifacts_dir = "{self.root / 'share' / 'artifacts'}"
            log_path = "{self.root / 'state' / 'runner.jsonl'}"
            profiles_dir = "{self.profiles_dir}"
            job_schema_path = "{self.app_dir / 'job_schema.json'}"
            result_schema_path = "{self.app_dir / 'result_schema.json'}"
            max_changed_files = 2
            max_diff_bytes = {max_diff_bytes}
            active_lease_seconds = 2
            service_label = "com.example.mac-runner"
            owner_pubkey = "{owner_pubkey}"

            [ollama]
            endpoint = "{self.endpoint}"
            model = "ornith-9b-agent"
            timeout_seconds = 2
            max_output_chars = 4096
            native_tool_calls_required = true

            [supervisor]
            codex_path = "{self.fake_codex}"
            model = "{model}"
            allow_write_tasks = {str(allow_write_tasks).lower()}
            require_source_metadata = {str(require_source_metadata).lower()}

            [capabilities]
            image_generation = false
            git = true
            {extra_capabilities}

            [repos.repo1]
            path = "{self.repo}"
            fetch_remote = "origin"
            test_profiles = ["review-readonly", "backend-unit", "git-sync-verify", "self-update-runner"]
            sensitive_paths = {sensitive_paths}
            sync_enabled = true
            canonical_remote_url = "{self.origin}"
            sync_remote = "origin"
            sync_branch = "main"
            {repo_prepare_config}
            """
        ).strip()
        if capability_bindings:
            config = config + "\n\n" + textwrap.dedent(capability_bindings).strip() + "\n"
        config_path.write_text(config, encoding="utf-8")
        return config_path

    def _write_no_repo_config(self) -> Path:
        config_path = self.app_dir / "config-no-repo.toml"
        config = textwrap.dedent(
            f"""
            [runner]
            state_dir = "{self.root / 'state-no-repo'}"
            db_path = "{self.root / 'state-no-repo' / 'runner.db'}"
            worktree_root = "{self.root / 'share-no-repo' / 'worktrees'}"
            artifacts_dir = "{self.root / 'share-no-repo' / 'artifacts'}"
            log_path = "{self.root / 'state-no-repo' / 'runner.jsonl'}"
            profiles_dir = "{self.profiles_dir}"
            job_schema_path = "{self.app_dir / 'job_schema.json'}"
            result_schema_path = "{self.app_dir / 'result_schema.json'}"
            max_changed_files = 2
            max_diff_bytes = 128
            active_lease_seconds = 2

            [ollama]
            endpoint = "{self.endpoint}"
            model = "ornith-9b-agent"
            timeout_seconds = 2
            max_output_chars = 4096
            native_tool_calls_required = true

            [supervisor]
            codex_path = "{self.fake_codex}"
            model = ""
            allow_write_tasks = true

            [capabilities]
            image_generation = false
            """
        ).strip()
        config_path.write_text(config, encoding="utf-8")
        return config_path

    def _job(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": "mac-job/v1",
            "job_id": "agent-20260821-0042",
            "attempt": 1,
            "repo_id": "repo1",
            "base_sha": self.base_sha,
            "target_sha": self.target_sha,
            "task_type": "review",
            "focus": ["concurrency"],
            "write": False,
            "allowed_paths": [],
            "test_profile": "review-readonly",
            "deadline_seconds": 30,
            "supervisor": "mac-codex",
            "execution_route": "auto",
            "preferred_worker": "ornith",
            "required_capabilities": [],
        }
        payload.update(overrides)
        return payload

    def _sync_job(self, **overrides: object) -> dict[str, object]:
        payload = self._job(
            job_id="sync-job",
            task_type="sync",
            focus=["git-sync", "exact-sha-verification", "clean-worktree-safety"],
            write=True,
            allowed_paths=["."],
            test_profile="git-sync-verify",
            execution_route="ornith-then-codex",
            preferred_worker="ornith",
            required_capabilities=["git"],
            summary="Synchronize the configured checkout.",
            instructions="Audit text only and never command input.",
            acceptance_criteria=["Exact target SHA"],
            metadata={
                "remote_url": "https://example.invalid/audit-only.git",
                "branch": "audit-only",
                "source_channel_id": "123e4567-e89b-12d3-a456-426614174000",
                "source_event_id": "a" * 64,
            },
        )
        payload.update(overrides)
        return payload

    def _runner(self, *, allow_write_tasks: bool = True, model: str = "", max_diff_bytes: int = 4096) -> runner.Runner:
        config = runner.RunnerConfig.load(self._write_config(allow_write_tasks=allow_write_tasks, model=model, max_diff_bytes=max_diff_bytes))
        self.config = config
        return runner.Runner(config)

    def _policy_v2_job(self, **overrides: object) -> dict[str, object]:
        payload = self._job()
        payload.pop("write", None)
        payload.pop("allowed_paths", None)
        payload.pop("test_profile", None)
        payload.update(
            {
                "policy_version": 2,
                "permission_profile": "observe",
                "capabilities": [],
                "scope": {"root": "worktree", "paths": []},
                "network": {"mode": "none"},
                "verification_profiles": ["review-readonly"],
                "context": {"source": "test"},
                "extensions": {"trace": "keep"},
            }
        )
        payload.update(overrides)
        return payload

    def _codex_entries(self) -> list[dict[str, object]]:
        if not self.codex_log.exists():
            return []
        return [json.loads(line) for line in self.codex_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _fake_codex_script(self) -> str:
        return textwrap.dedent(
            f"""#!{sys.executable}
import json
import os
import re
import sys
import time
from pathlib import Path

LOG = Path({str(self.codex_log)!r})
prompt = sys.stdin.read()
argv = sys.argv[1:]
task_match = re.search(r"TASK:\\s*(\\w+)", prompt)
task = task_match.group(1) if task_match else "unknown"
LOG.parent.mkdir(parents=True, exist_ok=True)
with LOG.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"task": task, "argv": argv, "cwd": os.getcwd()}}) + "\\n")

def extract_block(label):
    marker = label + ":\\n"
    start = prompt.find(marker)
    if start == -1:
        return {{}}
    start += len(marker)
    rest = prompt[start:]
    if "\\n\\n" in rest:
        rest = rest.split("\\n\\n", 1)[0]
    return json.loads(rest)

job = extract_block("Job")
if isinstance(job, dict) and job.get("job_id") == "slow-job":
    time.sleep(2)
if task == "decide_route":
    payload = {{"route": "codex" if job.get("write") else "ornith", "reason": "fake route"}}
elif task == "execute_job":
    if job.get("write"):
        allowed_paths = job.get("allowed_paths") or []
        if job.get("job_id") == "sensitive-write":
            target = Path(".env")
        else:
            target = Path(allowed_paths[0]) if allowed_paths else Path("README.md")
        target.write_text("changed by fake codex\\n", encoding="utf-8")
        if job.get("job_id") == "escape-write":
            Path("outside.txt").write_text("outside\\n", encoding="utf-8")
    payload = {{"findings": [{{"severity": "info", "title": "codex", "detail": "fake codex executed"}}]}}
elif task == "accept_result":
    if job.get("job_id") == "accept-retry":
        marker = LOG.with_suffix(".accept-once")
        if not marker.exists():
            marker.write_text("failed once\\n", encoding="utf-8")
            raise SystemExit(9)
    tests = extract_block("Tests")
    accepted = tests.get("exit_code") == 0
    payload = {{"accepted": accepted, "summary": "fake acceptance", "errors": [] if accepted else ["tests failed"]}}
else:
    payload = {{"error": "unknown task"}}
print(json.dumps({{"type": "event", "message": "noise"}}))
print(json.dumps({{"type": "final", "content": json.dumps(payload)}}))
"""
        )

    def test_submit_replay_and_conflict(self) -> None:
        subject = self._runner(max_diff_bytes=128)
        first = subject.submit(self.sample_job)
        self.assertEqual(first["status"], "VALIDATED")
        executed = subject.execute("agent-20260821-0042", 1)
        self.assertEqual(executed["status"], "DONE")
        for _ in range(20):
            replay = subject.submit(self.sample_job)
            self.assertEqual(replay["status"], "DONE")
            self.assertEqual(replay["result"], executed["result"])
        conflict = subject.submit(self._job(focus=["different"]))
        self.assertEqual(conflict["status"], "REJECTED")
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
        subject.close()

    def test_unknown_repo_and_variable_sha_are_rejected(self) -> None:
        subject = self._runner(max_diff_bytes=128)
        bad_repo = subject.submit(self._job(repo_id="missing"))
        self.assertEqual(bad_repo["status"], "REJECTED")
        self.assertEqual(bad_repo["error"]["code"], "unknown_repo")
        bad_sha = subject.submit(self._job(job_id="job-two", target_sha="main"))
        self.assertEqual(bad_sha["status"], "REJECTED")
        self.assertEqual(bad_sha["error"]["code"], "schema_validation_failed")
        bad_profile = subject.submit(self._job(job_id="bad-profile", test_profile="arbitrary-shell"))
        self.assertEqual(bad_profile["status"], "REJECTED")
        self.assertEqual(bad_profile["error"]["code"], "unknown_test_profile")
        self._write_file(self.profiles_dir / "wrong-repo.toml", 'command = ["/usr/bin/true"]\n')
        wrong_repo_profile = subject.submit(self._job(job_id="wrong-repo-profile", test_profile="wrong-repo"))
        self.assertEqual(wrong_repo_profile["status"], "REJECTED")
        self.assertEqual(wrong_repo_profile["error"]["code"], "test_profile_not_allowed")
        unavailable = subject.submit(
            self._job(job_id="capability-job", required_capabilities=["image_generation"])
        )
        self.assertEqual(unavailable["status"], "REJECTED")
        self.assertEqual(unavailable["error"]["code"], "capability_unavailable")
        subject.close()

    def test_sync_happy_path_is_deterministic_and_model_free(self) -> None:
        self._prepare_sync_repo()
        subject = self._runner()
        submitted = subject.submit(self._sync_job())
        self.assertEqual(submitted["status"], "VALIDATED")
        git_commands: list[list[str]] = []
        original_run_command = runner.run_command

        def record_command(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            git_commands.append(args)
            return original_run_command(args, **kwargs)

        with mock.patch.object(runner, "run_command", side_effect=record_command):
            result = subject.execute("sync-job", 1)
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["route"], "sync")
        self.assertEqual(result["result"]["route"], "sync")
        self.assertEqual(result["result"]["commit_sha"], self.target_sha)
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), self.target_sha)
        self.assertEqual(self._git(self.repo, "rev-parse", "refs/remotes/origin/main"), self.target_sha)
        self.assertEqual(self._git(self.repo, "branch", "--show-current"), "main")
        self.assertEqual(self._git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"), "")
        self.assertEqual(self._codex_entries(), [])
        flattened = [token for command in git_commands for token in command]
        for forbidden in ("push", "reset", "clean", "checkout", "tag", "--force", "-f"):
            self.assertNotIn(forbidden, flattened)
        self.assertNotIn("https://example.invalid/audit-only.git", flattened)
        states = [
            row[0]
            for row in subject.ledger.conn.execute(
                "SELECT state FROM job_events WHERE job_id = ? AND attempt = ? ORDER BY id",
                ("sync-job", 1),
            ).fetchall()
        ]
        self.assertEqual(states, ["RECEIVED", "VALIDATED", "SUPERVISING", "PREPARING", "RUNNING", "VERIFYING", "DONE"])
        subject.close()

    def test_sync_dirty_worktree_fails_without_moving_head(self) -> None:
        self._prepare_sync_repo()
        (self.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        subject = self._runner()
        self.assertEqual(subject.submit(self._sync_job())["status"], "VALIDATED")
        result = subject.execute("sync-job", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "dirty_worktree")
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), self.base_sha)
        subject.close()

    def test_sync_wrong_remote_fails_without_fetch_or_checkout_update(self) -> None:
        self._prepare_sync_repo()
        wrong_origin = self.root / "wrong-origin.git"
        self._run("git", "init", "--bare", "--initial-branch=main", str(wrong_origin))
        self._run("git", "-C", str(self.repo), "remote", "set-url", "origin", str(wrong_origin))
        subject = self._runner()
        self.assertEqual(subject.submit(self._sync_job())["status"], "VALIDATED")
        result = subject.execute("sync-job", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "wrong_remote")
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), self.base_sha)
        subject.close()

    def test_sync_wrong_branch_fails_without_checkout_update(self) -> None:
        self._prepare_sync_repo()
        self._run("git", "-C", str(self.repo), "checkout", "-b", "other")
        subject = self._runner()
        self.assertEqual(subject.submit(self._sync_job())["status"], "VALIDATED")
        result = subject.execute("sync-job", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "wrong_branch")
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), self.base_sha)
        subject.close()

    def test_sync_base_mismatch_fails_without_checkout_update(self) -> None:
        self._prepare_sync_repo()
        subject = self._runner()
        job = self._sync_job(base_sha=self.target_sha)
        self.assertEqual(subject.submit(job)["status"], "VALIDATED")
        result = subject.execute("sync-job", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "base_mismatch")
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), self.base_sha)
        subject.close()

    def test_sync_target_mismatch_fails_without_checkout_update(self) -> None:
        self._prepare_sync_repo()
        subject = self._runner()
        job = self._sync_job(target_sha=self.base_sha)
        self.assertEqual(subject.submit(job)["status"], "VALIDATED")
        result = subject.execute("sync-job", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "target_mismatch")
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), self.base_sha)
        subject.close()

    def test_sync_divergence_is_rejected_as_non_fast_forward(self) -> None:
        self._prepare_sync_repo()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        (self.repo / "local.txt").write_text("local divergence\n", encoding="utf-8")
        self._run("git", "-C", str(self.repo), "add", "local.txt", env=env)
        self._run("git", "-C", str(self.repo), "commit", "-m", "local divergence", env=env)
        divergent_sha = self._git(self.repo, "rev-parse", "HEAD")
        subject = self._runner()
        job = self._sync_job(base_sha=divergent_sha)
        self.assertEqual(subject.submit(job)["status"], "VALIDATED")
        result = subject.execute("sync-job", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "non_fast_forward")
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), divergent_sha)
        subject.close()

    def test_sync_fetch_failure_is_structured_and_does_not_move_head(self) -> None:
        self._prepare_sync_repo()
        missing_origin = self.root / "origin-unavailable.git"
        self.origin.rename(missing_origin)
        subject = self._runner()
        self.assertEqual(subject.submit(self._sync_job())["status"], "VALIDATED")
        result = subject.execute("sync-job", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "fetch_failed")
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), self.base_sha)
        subject.close()

    def test_sync_repo_lock_rejects_concurrent_execution(self) -> None:
        self._prepare_sync_repo()
        subject = self._runner()
        self.assertEqual(subject.submit(self._sync_job())["status"], "VALIDATED")
        repo_config = subject.worktrees.repo("repo1")
        with subject.sync.lock(repo_config):
            result = subject.execute("sync-job", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "repo_sync_busy")
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), self.base_sha)
        subject.close()

    def test_sync_verifying_state_resumes_idempotently_without_model_routing(self) -> None:
        self._prepare_sync_repo()
        subject = self._runner()
        self.assertEqual(subject.submit(self._sync_job(job_id="sync-resume"))["status"], "VALIDATED")
        previous = "VALIDATED"
        for state in ("SUPERVISING", "PREPARING", "RUNNING", "VERIFYING"):
            subject.ledger.transition("sync-resume", 1, {previous}, state, route="sync", lease_expires=0.0)
            previous = state
        result = subject.execute("sync-resume", 1)
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["result"]["route"], "sync")
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), self.target_sha)
        self.assertEqual(self._codex_entries(), [])
        subject.close()

    def test_sync_dry_run_accepts_sender_audit_fields_without_ledger_or_git_mutation(self) -> None:
        self._prepare_sync_repo()
        subject = self._runner()
        result = subject.validate_dry_run(self._sync_job(attempt=2))
        self.assertEqual(result["status"], "VALIDATED")
        self.assertTrue(result["dry_run"])
        with self.assertRaises(runner.RunnerError) as error:
            subject.get("sync-job", 2)
        self.assertEqual(error.exception.code, "job_not_found")
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), self.base_sha)
        self.assertEqual(self._codex_entries(), [])
        subject.close()

    def test_policy_v2_sync_accepts_root_scope_without_paths_and_records_wire_hash(self) -> None:
        self._prepare_sync_repo()
        info_exclude = self.repo / ".git" / "info" / "exclude"
        info_exclude.write_text("retained-cache.bin\n", encoding="utf-8")
        ignored = self.repo / "retained-cache.bin"
        ignored.write_text("local cache\n", encoding="utf-8")
        config = runner.RunnerConfig.load(
            self._write_config(
                extra_capabilities="sync-registered-repo = true",
            )
        )
        subject = runner.Runner(config)
        job = self._policy_v2_job(
            job_id="sync-v2",
            task_type="sync",
            permission_profile="operational",
            capabilities=["sync-registered-repo"],
            scope={"root": "registered-checkout", "paths": []},
            network={"mode": "declared-remotes-and-registries"},
            verification_profiles=["git-sync-verify"],
            execution_route="auto",
        )
        submitted = subject.submit(job)
        self.assertEqual(submitted["status"], "VALIDATED")
        result = subject.execute("sync-v2", 1)
        self.assertEqual(result["status"], "DONE")
        stored = subject.get("sync-v2", 1)
        self.assertEqual(stored["payload"]["permission_profile"], "operational")
        self.assertEqual(stored["payload"]["capabilities"], ["sync-registered-repo"])
        self.assertRegex(stored["wire_payload_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(stored["wire_payload"]["scope"]["paths"], [])
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), self.target_sha)
        self.assertEqual(ignored.read_text(encoding="utf-8"), "local cache\n")
        subject.close()

    def test_policy_v2_sync_rejects_ignored_target_collision_before_head_mutation(self) -> None:
        collision_target = self._commit_repo_file("generated/cache.bin", "tracked\n", "track generated cache")
        self._prepare_sync_repo()
        info_exclude = self.repo / ".git" / "info" / "exclude"
        info_exclude.write_text("generated\n", encoding="utf-8")
        ignored = self.repo / "generated"
        ignored.write_text("local cache\n", encoding="utf-8")
        config = runner.RunnerConfig.load(self._write_config(extra_capabilities="sync-registered-repo = true"))
        subject = runner.Runner(config)
        job = self._policy_v2_job(
            job_id="sync-ignored-collision",
            task_type="sync",
            permission_profile="operational",
            capabilities=["sync-registered-repo"],
            scope={"root": "registered-checkout", "paths": []},
            network={"mode": "declared-remotes-and-registries"},
            verification_profiles=["git-sync-verify"],
            execution_route="auto",
            target_sha=collision_target,
        )
        self.assertEqual(subject.submit(job)["status"], "VALIDATED")
        result = subject.execute("sync-ignored-collision", 1)
        self.assertEqual(result["status"], "FAILED", result)
        self.assertEqual(result["error"]["code"], "ignored_target_collision", result)
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), self.base_sha)
        self.assertEqual(ignored.read_text(encoding="utf-8"), "local cache\n")
        subject.close()

    def test_policy_v2_prepare_registered_repo_backs_up_untracked_files_and_repairs_remote(self) -> None:
        self._prepare_sync_repo()
        first = self.repo / "notes" / "first.txt"
        first.parent.mkdir()
        first.write_text("first\n", encoding="utf-8")
        second = self.repo / "second.txt"
        second.write_text("second\n", encoding="utf-8")
        info_exclude = self.repo / ".git" / "info" / "exclude"
        info_exclude.write_text("retained-cache/\n", encoding="utf-8")
        ignored = self.repo / "retained-cache" / "cache.bin"
        ignored.parent.mkdir()
        ignored.write_text("local cache\n", encoding="utf-8")
        previous_remote = self.root / "old-origin.git"
        self._run("git", "-C", str(self.repo), "remote", "set-url", "origin", str(previous_remote))
        status_text = self._run(
            "git",
            "-C",
            str(self.repo),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout
        backup_root = self.root / "prep-backups"
        config = runner.RunnerConfig.load(
            self._write_config(
                extra_capabilities="prepare-registered-repo = true",
                repo_prepare_config=textwrap.dedent(
                    f"""
                    prepare_enabled = true
                    prepare_backup_root = "{backup_root}"
                    prepare_expected_status_sha256 = "{runner.sha256_hex(status_text.encode('utf-8'))}"
                    prepare_expected_untracked_count = 2
                    prepare_allowed_remote_urls = ["{previous_remote}", "{self.origin}"]
                    """
                ).strip(),
            )
        )
        subject = runner.Runner(config)
        job = self._policy_v2_job(
            job_id="prepare-v2",
            base_sha=self.base_sha,
            target_sha=self.base_sha,
            task_type="prepare",
            permission_profile="operational",
            capabilities=["prepare-registered-repo"],
            scope={"root": "registered-checkout", "paths": []},
            network={"mode": "none"},
            verification_profiles=["review-readonly"],
            execution_route="auto",
            preferred_worker="codex",
        )
        self.assertEqual(subject.submit(job)["status"], "VALIDATED")
        with mock.patch.object(
            subject,
            "_run_capability_verification",
            return_value={"profile": "review-readonly", "exit_code": 0},
        ):
            result = subject.execute("prepare-v2", 1)
        self.assertEqual(result["status"], "DONE", result)
        self.assertEqual(self._git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"), "")
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        backup = backup_root / "prepare-v2-attempt-1"
        self.assertEqual((backup / "notes" / "first.txt").read_text(encoding="utf-8"), "first\n")
        self.assertEqual((backup / "second.txt").read_text(encoding="utf-8"), "second\n")
        self.assertTrue((backup / "manifest.json").is_file())
        self.assertEqual(ignored.read_text(encoding="utf-8"), "local cache\n")
        worker = subject._read_artifact(result, "worker-result")
        self.assertEqual(worker["outcome"]["ignored_file_count"], 1)
        self.assertRegex(worker["outcome"]["ignored_paths_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(self._git(self.repo, "remote", "get-url", "origin"), str(self.origin))
        subject.close()

    def test_prepare_registered_repo_status_mismatch_fails_without_mutation(self) -> None:
        self._prepare_sync_repo()
        untracked = self.repo / "keep.txt"
        untracked.write_text("keep\n", encoding="utf-8")
        backup_root = self.root / "prep-backups-mismatch"
        config = runner.RunnerConfig.load(
            self._write_config(
                extra_capabilities="prepare-registered-repo = true",
                repo_prepare_config=textwrap.dedent(
                    f"""
                    prepare_enabled = true
                    prepare_backup_root = "{backup_root}"
                    prepare_expected_status_sha256 = "{'0' * 64}"
                    prepare_expected_untracked_count = 1
                    prepare_allowed_remote_urls = ["{self.origin}"]
                    """
                ).strip(),
            )
        )
        subject = runner.Runner(config)
        job = self._policy_v2_job(
            job_id="prepare-mismatch",
            base_sha=self.base_sha,
            target_sha=self.base_sha,
            task_type="prepare",
            permission_profile="operational",
            capabilities=["prepare-registered-repo"],
            scope={"root": "registered-checkout", "paths": []},
            network={"mode": "none"},
            verification_profiles=["review-readonly"],
        )
        self.assertEqual(subject.submit(job)["status"], "VALIDATED")
        with mock.patch.object(
            subject,
            "_run_capability_verification",
            return_value={"profile": "review-readonly", "exit_code": 0},
        ):
            result = subject.execute("prepare-mismatch", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "prep_status_mismatch", result)
        self.assertEqual(untracked.read_text(encoding="utf-8"), "keep\n")
        self.assertFalse(backup_root.exists())
        self.assertEqual(self._git(self.repo, "remote", "get-url", "origin"), str(self.origin))
        subject.close()

    def test_prepare_registered_repo_ignored_inventory_drift_rolls_back_visible_backup(self) -> None:
        self._prepare_sync_repo()
        visible = self.repo / "keep.txt"
        visible.write_text("keep\n", encoding="utf-8")
        info_exclude = self.repo / ".git" / "info" / "exclude"
        info_exclude.write_text("cache.bin\n", encoding="utf-8")
        ignored = self.repo / "cache.bin"
        ignored.write_text("cache\n", encoding="utf-8")
        status_text = self._run(
            "git", "-C", str(self.repo), "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout
        backup_root = self.root / "prep-backups-ignored-drift"
        config = runner.RunnerConfig.load(
            self._write_config(
                extra_capabilities="prepare-registered-repo = true",
                repo_prepare_config=textwrap.dedent(
                    f"""
                    prepare_enabled = true
                    prepare_backup_root = "{backup_root}"
                    prepare_expected_status_sha256 = "{runner.sha256_hex(status_text.encode('utf-8'))}"
                    prepare_expected_untracked_count = 1
                    prepare_allowed_remote_urls = ["{self.origin}"]
                    """
                ).strip(),
            )
        )
        subject = runner.Runner(config)
        job = self._policy_v2_job(
            job_id="prepare-ignored-drift",
            base_sha=self.base_sha,
            target_sha=self.base_sha,
            task_type="prepare",
            permission_profile="operational",
            capabilities=["prepare-registered-repo"],
            scope={"root": "registered-checkout", "paths": []},
            network={"mode": "none"},
            verification_profiles=["review-readonly"],
        )
        self.assertEqual(subject.submit(job)["status"], "VALIDATED")
        original_inventory = subject.sync._ignored_inventory
        inventory_calls = 0

        def drift_once(repo: runner.RepoConfig, deadline: runner.Deadline) -> tuple[list[str], str]:
            nonlocal inventory_calls
            inventory_calls += 1
            paths, digest = original_inventory(repo, deadline)
            if inventory_calls == 2:
                changed = [*paths, "injected-drift.bin"]
                encoded = "".join(f"{path}\0" for path in changed).encode("utf-8")
                return changed, runner.sha256_hex(encoded)
            return paths, digest

        with (
            mock.patch.object(
                subject,
                "_run_capability_verification",
                return_value={"profile": "review-readonly", "exit_code": 0},
            ),
            mock.patch.object(subject.sync, "_ignored_inventory", side_effect=drift_once),
        ):
            result = subject.execute("prepare-ignored-drift", 1)
        self.assertEqual(result["status"], "FAILED", result)
        self.assertEqual(result["error"]["code"], "prep_ignored_inventory_changed", result)
        self.assertEqual(visible.read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(ignored.read_text(encoding="utf-8"), "cache\n")
        self.assertFalse((backup_root / "prepare-ignored-drift-attempt-1").exists())
        subject.close()

    def test_prepare_registered_repo_move_failure_restores_exact_status(self) -> None:
        self._prepare_sync_repo()
        first = self.repo / "first.txt"
        second = self.repo / "second.txt"
        first.write_text("first\n", encoding="utf-8")
        second.write_text("second\n", encoding="utf-8")
        status_text = self._run(
            "git",
            "-C",
            str(self.repo),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout
        backup_root = self.root / "prep-backups-rollback"
        config = runner.RunnerConfig.load(
            self._write_config(
                extra_capabilities="prepare-registered-repo = true",
                repo_prepare_config=textwrap.dedent(
                    f"""
                    prepare_enabled = true
                    prepare_backup_root = "{backup_root}"
                    prepare_expected_status_sha256 = "{runner.sha256_hex(status_text.encode('utf-8'))}"
                    prepare_expected_untracked_count = 2
                    prepare_allowed_remote_urls = ["{self.origin}"]
                    """
                ).strip(),
            )
        )
        subject = runner.Runner(config)
        job = self._policy_v2_job(
            job_id="prepare-rollback",
            base_sha=self.base_sha,
            target_sha=self.base_sha,
            task_type="prepare",
            permission_profile="operational",
            capabilities=["prepare-registered-repo"],
            scope={"root": "registered-checkout", "paths": []},
            network={"mode": "none"},
            verification_profiles=["review-readonly"],
        )
        self.assertEqual(subject.submit(job)["status"], "VALIDATED")
        original_move = runner.shutil.move
        injected = False

        def fail_second_backup(source: str, destination: str) -> object:
            nonlocal injected
            if not injected and source.endswith("second.txt") and str(backup_root) in destination:
                injected = True
                raise OSError("injected move failure")
            return original_move(source, destination)

        with (
            mock.patch.object(
                subject,
                "_run_capability_verification",
                return_value={"profile": "review-readonly", "exit_code": 0},
            ),
            mock.patch.object(runner.shutil, "move", side_effect=fail_second_backup),
        ):
            result = subject.execute("prepare-rollback", 1)
        self.assertEqual(result["status"], "FAILED", result)
        self.assertEqual(result["error"]["code"], "prep_failed", result)
        self.assertEqual(first.read_text(encoding="utf-8"), "first\n")
        self.assertEqual(second.read_text(encoding="utf-8"), "second\n")
        restored = self._run(
            "git",
            "-C",
            str(self.repo),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout
        self.assertEqual(runner.sha256_hex(restored.encode("utf-8")), runner.sha256_hex(status_text.encode("utf-8")))
        self.assertFalse((backup_root / "prepare-rollback-attempt-1").exists())
        subject.close()

    def test_prepare_backup_root_inside_checkout_is_rejected(self) -> None:
        with self.assertRaises(runner.RunnerError) as error:
            runner.RunnerConfig.load(
                self._write_config(
                    extra_capabilities="prepare-registered-repo = true",
                    repo_prepare_config=textwrap.dedent(
                        f"""
                        prepare_enabled = true
                        prepare_backup_root = "{self.repo / 'unsafe-backup'}"
                        prepare_expected_status_sha256 = "{'0' * 64}"
                        prepare_expected_untracked_count = 1
                        prepare_allowed_remote_urls = ["{self.origin}"]
                        """
                    ).strip(),
                )
            )
        self.assertEqual(error.exception.code, "invalid_config")

    def test_policy_v2_dry_run_rejects_unknown_top_level_field(self) -> None:
        subject = self._runner()
        payload = self._policy_v2_job(unexpected_top_level_field="rejected")
        with self.assertRaises(runner.RunnerError) as error:
            subject.validate_dry_run(payload)
        self.assertEqual(error.exception.code, "schema_validation_failed")
        subject.close()

    def test_policy_v2_network_modes_and_privileged_owner_approval_are_strict(self) -> None:
        config = runner.RunnerConfig.load(
            self._write_config(
                owner_pubkey="1" * 64,
                extra_capabilities="\n".join(
                    [
                        "sync-registered-repo = true",
                        "push-task-branch = true",
                        "restart-user-service = true",
                    ]
                ),
                capability_bindings="""
                [capability_bindings.push-task-branch]
                remote = "origin"
                allowed_branch_prefixes = ["job/"]
                protected_branch_prefixes = ["main", "master", "release/"]

                [capability_bindings.restart-user-service]
                service_labels = ["com.example.mac-runner"]
                """,
            )
        )
        subject = runner.Runner(config)
        relay_sync = subject.submit(
            self._policy_v2_job(
                job_id="sync-relay",
                task_type="sync",
                permission_profile="operational",
                capabilities=["sync-registered-repo"],
                scope={"root": "registered-checkout", "paths": []},
                network={"mode": "relay-only"},
                verification_profiles=["git-sync-verify"],
            )
        )
        self.assertEqual(relay_sync["status"], "REJECTED")
        self.assertEqual(relay_sync["error"]["code"], "network_not_allowed")

        restart_bad_network = subject.submit(
            self._policy_v2_job(
                job_id="restart-bad-network",
                permission_profile="operational",
                capabilities=["restart-user-service"],
                scope={"root": "registered-checkout", "paths": []},
                network={"mode": "declared-remotes-and-registries"},
                verification_profiles=["review-readonly"],
            )
        )
        self.assertEqual(restart_bad_network["status"], "REJECTED")
        self.assertEqual(restart_bad_network["error"]["code"], "network_not_allowed")

        summary = "capability=push-task-branch;repo=repo1;job=push-priv;attempt=1;branch=job/push-priv"
        privileged_ok = subject.validate_dry_run(
            self._policy_v2_job(
                job_id="push-priv",
                permission_profile="privileged",
                capabilities=["push-task-branch"],
                scope={"root": "registered-checkout", "paths": []},
                network={"mode": "declared-remotes-and-registries"},
                verification_profiles=["review-readonly"],
                owner_approval={
                    "approved_by": "1" * 64,
                    "approval_ref": "oa-event-1",
                    "approved_at": "2026-08-23T10:00:00+09:00",
                    "summary": summary,
                },
            )
        )
        self.assertEqual(privileged_ok["status"], "VALIDATED")

        future = subject.submit(
            self._policy_v2_job(
                job_id="push-priv-future",
                permission_profile="privileged",
                capabilities=["push-task-branch"],
                scope={"root": "registered-checkout", "paths": []},
                network={"mode": "declared-remotes-and-registries"},
                verification_profiles=["review-readonly"],
                owner_approval={
                    "approved_by": "1" * 64,
                    "approval_ref": "oa-event-2",
                    "approved_at": (
                        dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
                    ).isoformat(),
                    "summary": "capability=push-task-branch;repo=repo1;job=push-priv-future;attempt=1;branch=job/push-priv-future",
                },
            )
        )
        self.assertEqual(future["status"], "REJECTED")
        self.assertEqual(future["error"]["code"], "invalid_owner_approval")

        self_restart = subject.submit(
            self._policy_v2_job(
                job_id="restart-self",
                permission_profile="operational",
                capabilities=["restart-user-service"],
                scope={"root": "registered-checkout", "paths": []},
                network={"mode": "none"},
                verification_profiles=["review-readonly"],
            )
        )
        self.assertEqual(self_restart["status"], "VALIDATED")
        failed = subject.execute("restart-self", 1)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"]["code"], "capability_not_configured")
        subject.close()

    def test_future_owner_approval_is_rejected_relative_to_runtime_clock(self) -> None:
        config = runner.RunnerConfig.load(
            self._write_config(
                owner_pubkey="1" * 64,
                extra_capabilities="push-task-branch = true",
                capability_bindings="""
                [capability_bindings.push-task-branch]
                remote = "origin"
                allowed_branch_prefixes = ["job/"]
                protected_branch_prefixes = ["main", "master"]
                """,
            )
        )
        subject = runner.Runner(config)
        future = subject.submit(
            self._policy_v2_job(
                job_id="push-priv-future-clock",
                permission_profile="privileged",
                capabilities=["push-task-branch"],
                scope={"root": "registered-checkout", "paths": []},
                network={"mode": "declared-remotes-and-registries"},
                verification_profiles=["review-readonly"],
                owner_approval={
                    "approved_by": "1" * 64,
                    "approval_ref": "oa-event-future-clock",
                    "approved_at": (
                        dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
                    ).isoformat(),
                    "summary": (
                        "capability=push-task-branch;repo=repo1;"
                        "job=push-priv-future-clock;attempt=1;branch=job/push-priv-future-clock"
                    ),
                },
            )
        )
        self.assertEqual(future["status"], "REJECTED")
        self.assertEqual(future["error"]["code"], "invalid_owner_approval")
        subject.close()

    def test_self_update_runner_is_default_disabled_and_requires_exact_owner_summary(self) -> None:
        (self.app_dir / ".source-commit").write_text(f"{self.target_sha}\n", encoding="utf-8")
        disabled = self._runner()
        rejected = disabled.submit(
            self._policy_v2_job(
                job_id="self-update-disabled",
                base_sha=self.target_sha,
                target_sha=self.target_sha,
                task_type="self-update",
                permission_profile="privileged",
                capabilities=["self-update-runner"],
                scope={"root": "registered-checkout", "paths": []},
                network={"mode": "declared-remotes-and-registries"},
                verification_profiles=["self-update-runner"],
                owner_approval={
                    "approved_by": "1" * 64,
                    "approval_ref": "oa-self-disabled",
                    "approved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "summary": "capability=self-update-runner",
                },
            )
        )
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertEqual(rejected["error"]["code"], "capability_unavailable")
        disabled.close()

        config = runner.RunnerConfig.load(
            self._write_config(
                owner_pubkey="1" * 64,
                extra_capabilities="self-update-runner = true",
                capability_bindings=f"""
                [capability_bindings.self-update-runner]
                repo_id = "repo1"
                canonical_remote_url = "{self.origin}"
                remote = "origin"
                branch = "main"
                service_label = "com.example.mac-runner"
                helper_path = "{self.external_helper}"
                """,
            )
        )
        subject = runner.Runner(config)
        status = subject.status()["runner"]["self_update"]
        self.assertTrue(status["enabled"])
        self.assertEqual(status["service_label"], "com.example.mac-runner")
        self.assertEqual(status["source_marker"], self.target_sha)
        good_summary = (
            "capability=self-update-runner;repo=repo1;job=self-update-summary;attempt=1;"
            f"base={self.target_sha};target={self.target_sha};service=com.example.mac-runner;"
            f"url={self.origin};remote=origin;branch=main"
        )
        accepted = subject.validate_dry_run(
            self._policy_v2_job(
                job_id="self-update-summary",
                base_sha=self.target_sha,
                target_sha=self.target_sha,
                task_type="self-update",
                permission_profile="privileged",
                capabilities=["self-update-runner"],
                scope={"root": "registered-checkout", "paths": []},
                network={"mode": "declared-remotes-and-registries"},
                verification_profiles=["self-update-runner"],
                owner_approval={
                    "approved_by": "1" * 64,
                    "approval_ref": "oa-self-summary",
                    "approved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "summary": good_summary,
                },
            )
        )
        self.assertEqual(accepted["status"], "VALIDATED")
        subject.close()

        self.external_helper.chmod(0o777)
        with self.assertRaisesRegex(runner.RunnerError, "not group/other writable"):
            runner.RunnerConfig.load(
                self._write_config(
                    owner_pubkey="1" * 64,
                    extra_capabilities="self-update-runner = true",
                    capability_bindings=f"""
                    [capability_bindings.self-update-runner]
                    repo_id = "repo1"
                    canonical_remote_url = "{self.origin}"
                    remote = "origin"
                    branch = "main"
                    service_label = "com.example.mac-runner"
                    helper_path = "{self.external_helper}"
                    """,
                )
            )

    def test_self_update_noop_finishes_without_helper(self) -> None:
        (self.app_dir / ".source-commit").write_text(f"{self.target_sha}\n", encoding="utf-8")
        config = runner.RunnerConfig.load(
            self._write_config(
                owner_pubkey="1" * 64,
                extra_capabilities="self-update-runner = true",
                capability_bindings=f"""
                [capability_bindings.self-update-runner]
                repo_id = "repo1"
                canonical_remote_url = "{self.origin}"
                remote = "origin"
                branch = "main"
                service_label = "com.example.mac-runner"
                helper_path = "{self.external_helper}"
                """,
            )
        )
        subject = runner.Runner(config)
        job_id = "self-update-noop"
        summary = (
            f"capability=self-update-runner;repo=repo1;job={job_id};attempt=1;"
            f"base={self.target_sha};target={self.target_sha};service=com.example.mac-runner;"
            f"url={self.origin};remote=origin;branch=main"
        )
        self.assertEqual(
            subject.submit(
                self._policy_v2_job(
                    job_id=job_id,
                    base_sha=self.target_sha,
                    target_sha=self.target_sha,
                    task_type="self-update",
                    permission_profile="privileged",
                    capabilities=["self-update-runner"],
                    scope={"root": "registered-checkout", "paths": []},
                    network={"mode": "declared-remotes-and-registries"},
                    verification_profiles=["self-update-runner"],
                    owner_approval={
                        "approved_by": "1" * 64,
                        "approval_ref": "oa-self-noop",
                        "approved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "summary": summary,
                    },
                )
            )["status"],
            "VALIDATED",
        )
        original_run_command = runner.run_command

        def fake_launchctl(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args and args[0] == "launchctl":
                return subprocess.CompletedProcess(args, 0, stdout="pid = 123\n", stderr="")
            return original_run_command(args, **kwargs)

        with (
            mock.patch.object(runner, "run_command", side_effect=fake_launchctl),
            mock.patch.object(runner, "subprocess", wraps=runner.subprocess) as subprocess_module,
        ):
            result = subject.execute(job_id, 1)
        subprocess_module.Popen.assert_not_called()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["result"]["commit_sha"], self.target_sha)
        subject.close()

    def test_self_update_requires_quiescent_runner(self) -> None:
        base = self.target_sha
        target = self._commit_repo_file("runner.py", "print('new runner')\n", "self update candidate busy")
        self._run("git", "-C", str(self.repo), "reset", "--hard", base)
        (self.app_dir / ".source-commit").write_text(f"{base}\n", encoding="utf-8")
        config = runner.RunnerConfig.load(
            self._write_config(
                owner_pubkey="1" * 64,
                extra_capabilities="self-update-runner = true",
                capability_bindings=f"""
                [capability_bindings.self-update-runner]
                repo_id = "repo1"
                canonical_remote_url = "{self.origin}"
                remote = "origin"
                branch = "main"
                service_label = "com.example.mac-runner"
                helper_path = "{self.external_helper}"
                """,
            )
        )
        subject = runner.Runner(config)
        self.assertEqual(subject.submit(self._policy_v2_job(job_id="other-pending"))["status"], "VALIDATED")
        job_id = "self-update-busy"
        summary = (
            f"capability=self-update-runner;repo=repo1;job={job_id};attempt=1;"
            f"base={base};target={target};service=com.example.mac-runner;"
            f"url={self.origin};remote=origin;branch=main"
        )
        self.assertEqual(
            subject.submit(
                self._policy_v2_job(
                    job_id=job_id,
                    base_sha=base,
                    target_sha=target,
                    task_type="self-update",
                    permission_profile="privileged",
                    capabilities=["self-update-runner"],
                    scope={"root": "registered-checkout", "paths": []},
                    network={"mode": "declared-remotes-and-registries"},
                    verification_profiles=["self-update-runner"],
                    owner_approval={
                        "approved_by": "1" * 64,
                        "approval_ref": "oa-self-busy",
                        "approved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "summary": summary,
                    },
                )
            )["status"],
            "VALIDATED",
        )
        failed = subject.execute(job_id, 1)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"]["code"], "runner_not_quiescent")
        subject.close()

    def test_self_update_starts_helper_then_resumes_from_success_result(self) -> None:
        base = self.target_sha
        target = self._commit_repo_file("runner.py", "print('new runner')\n", "self update candidate")
        self._run("git", "-C", str(self.repo), "reset", "--hard", base)
        (self.app_dir / ".source-commit").write_text(f"{base}\n", encoding="utf-8")
        writes_during_validation = "from pathlib import Path; Path('validator-output.txt').write_text('dirty\\n', encoding='utf-8')"
        self._write_file(
            self.profiles_dir / "self-update-runner.toml",
            f'command = ["python3", "-c", {json.dumps(writes_during_validation)}]\ntimeout_seconds = 30\n',
        )
        config = runner.RunnerConfig.load(
            self._write_config(
                owner_pubkey="1" * 64,
                extra_capabilities="self-update-runner = true",
                capability_bindings=f"""
                [capability_bindings.self-update-runner]
                repo_id = "repo1"
                canonical_remote_url = "{self.origin}"
                remote = "origin"
                branch = "main"
                service_label = "com.example.mac-runner"
                helper_path = "{self.external_helper}"
                """,
            )
        )
        subject = runner.Runner(config)
        job_id = "self-update-start"
        summary = (
            f"capability=self-update-runner;repo=repo1;job={job_id};attempt=1;"
            f"base={base};target={target};service=com.example.mac-runner;"
            f"url={self.origin};remote=origin;branch=main"
        )
        job = self._policy_v2_job(
            job_id=job_id,
            base_sha=base,
            target_sha=target,
            task_type="self-update",
            permission_profile="privileged",
            capabilities=["self-update-runner"],
            scope={"root": "registered-checkout", "paths": []},
            network={"mode": "declared-remotes-and-registries"},
            verification_profiles=["self-update-runner"],
            owner_approval={
                "approved_by": "1" * 64,
                "approval_ref": "oa-self-start",
                "approved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "summary": summary,
            },
        )
        self.assertEqual(subject.submit(job)["status"], "VALIDATED")
        original_run_command = runner.run_command

        def fake_launchctl(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args and args[0] == "launchctl":
                return subprocess.CompletedProcess(args, 0, stdout="pid = 123\n", stderr="")
            return original_run_command(args, **kwargs)

        original_popen = subprocess.Popen
        helper_starts: list[list[str]] = []

        def fake_popen(args: list[str], **kwargs: object) -> object:
            if any(Path(str(item)).resolve() == self.external_helper.resolve() for item in args):
                self.assertEqual(subject.get(job_id, 1)["status"], "VERIFYING")
                helper_starts.append(args)
                return mock.Mock()
            return original_popen(args, **kwargs)

        with (
            mock.patch.object(runner, "run_command", side_effect=fake_launchctl),
            mock.patch.object(runner.subprocess, "Popen", side_effect=fake_popen),
        ):
            started = subject.execute(job_id, 1)
        self.assertEqual(started["status"], "VERIFYING")
        self.assertEqual(len(helper_starts), 1)
        worker = subject._read_artifact(started, "worker-result")
        deploy_candidate = Path(worker["outcome"]["candidate_dir"])
        self.assertFalse((deploy_candidate / "validator-output.txt").exists())
        staged_plan = subject._read_artifact(started, "self-update-plan")
        self.assertEqual(staged_plan["candidate_sha256"], subject._tree_sha256(deploy_candidate))
        result_path = Path(worker["outcome"]["result_path"])
        result_path.write_text(
            json.dumps(
                {
                    "schema": "mac-runner/self-update-result-v1",
                    "status": "succeeded",
                    "target_sha": target,
                    "service_label": "com.example.mac-runner",
                    "before_pid": 123,
                    "after_pid": 456,
                    "config_sha256": config.config_sha256,
                    "sqlite_integrity": "ok",
                    "runner_status": "ok",
                    "source_marker": target,
                    "process_verified": True,
                    "queue": {"pending": 0, "running": 1, "retryable": 0, "worktrees": 0},
                    "job_status": "VERIFYING",
                    "runtime_path": worker["outcome"]["runtime_path"],
                    "runtime_sha256": worker["outcome"]["runtime_sha256"],
                    "runtime_version": worker["outcome"]["runtime_version"],
                }
            ),
            encoding="utf-8",
        )
        resumed = subject.execute(job_id, 1)
        self.assertEqual(resumed["status"], "DONE", resumed)
        self.assertEqual(resumed["result"]["commit_sha"], target)
        subject.close()

    def test_self_update_resume_failure_becomes_terminal_failed(self) -> None:
        base = self.target_sha
        target = self._commit_repo_file("runner.py", "print('bad runner')\n", "bad self update candidate")
        self._run("git", "-C", str(self.repo), "reset", "--hard", base)
        (self.app_dir / ".source-commit").write_text(f"{base}\n", encoding="utf-8")
        config = runner.RunnerConfig.load(
            self._write_config(
                owner_pubkey="1" * 64,
                extra_capabilities="self-update-runner = true",
                capability_bindings=f"""
                [capability_bindings.self-update-runner]
                repo_id = "repo1"
                canonical_remote_url = "{self.origin}"
                remote = "origin"
                branch = "main"
                service_label = "com.example.mac-runner"
                helper_path = "{self.external_helper}"
                """,
            )
        )
        subject = runner.Runner(config)
        job_id = "self-update-fail"
        summary = (
            f"capability=self-update-runner;repo=repo1;job={job_id};attempt=1;"
            f"base={base};target={target};service=com.example.mac-runner;"
            f"url={self.origin};remote=origin;branch=main"
        )
        self.assertEqual(
            subject.submit(
                self._policy_v2_job(
                    job_id=job_id,
                    base_sha=base,
                    target_sha=target,
                    task_type="self-update",
                    permission_profile="privileged",
                    capabilities=["self-update-runner"],
                    scope={"root": "registered-checkout", "paths": []},
                    network={"mode": "declared-remotes-and-registries"},
                    verification_profiles=["self-update-runner"],
                    owner_approval={
                        "approved_by": "1" * 64,
                        "approval_ref": "oa-self-fail",
                        "approved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "summary": summary,
                    },
                )
            )["status"],
            "VALIDATED",
        )
        original_run_command = runner.run_command

        def fake_launchctl(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args and args[0] == "launchctl":
                return subprocess.CompletedProcess(args, 0, stdout="pid = 123\n", stderr="")
            return original_run_command(args, **kwargs)

        original_popen = subprocess.Popen
        helper_starts: list[list[str]] = []

        def fake_popen(args: list[str], **kwargs: object) -> object:
            if any(Path(str(item)).resolve() == self.external_helper.resolve() for item in args):
                helper_starts.append(args)
                return mock.Mock()
            return original_popen(args, **kwargs)

        with (
            mock.patch.object(runner, "run_command", side_effect=fake_launchctl),
            mock.patch.object(runner.subprocess, "Popen", side_effect=fake_popen),
        ):
            started = subject.execute(job_id, 1)
        self.assertEqual(len(helper_starts), 1)
        worker = subject._read_artifact(started, "worker-result")
        result_path = Path(worker["outcome"]["result_path"])
        result_path.write_text(
            json.dumps(
                {
                    "schema": "mac-runner/self-update-result-v1",
                    "status": "failed",
                    "target_sha": target,
                    "error": "injected failure",
                }
            ),
            encoding="utf-8",
        )
        failed = subject.execute(job_id, 1)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"]["code"], "self_update_failed")
        subject.close()

    def test_legacy_write_with_empty_allowed_paths_maps_to_full_worktree(self) -> None:
        verify_code = "from pathlib import Path; raise SystemExit(0 if Path('README.md').read_text(encoding='utf-8').strip() == 'changed by fake codex' else 9)"
        self._write_file(
            self.profiles_dir / "backend-unit.toml",
            f'command = ["python3", "-c", {json.dumps(verify_code)}]\ntimeout_seconds = 30\n',
        )
        subject = self._runner()
        submitted = subject.submit(self._job(job_id="legacy-full-write", write=True, allowed_paths=[], test_profile="backend-unit"))
        self.assertEqual(submitted["status"], "VALIDATED")
        result = subject.execute("legacy-full-write", 1)
        self.assertEqual(result["status"], "DONE")
        self.assertRegex(result["result"]["commit_sha"], r"^[0-9a-f]{40}$")
        subject.close()

    def test_observe_rejects_target_commit_with_sensitive_tracked_path(self) -> None:
        secret_sha = self._commit_repo_file(".env", "TOKEN=secret\n", "add secret")
        subject = self._runner()
        rejected = subject.submit(self._job(job_id="sensitive-observe", target_sha=secret_sha))
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertEqual(rejected["error"]["code"], "sensitive_path_present")
        subject.close()

    def test_operational_rejects_target_commit_with_sensitive_tracked_path_before_side_effects(self) -> None:
        secret_sha = self._commit_repo_file("credentials/api.txt", "secret\n", "add credential")
        config = runner.RunnerConfig.load(
            self._write_config(
                extra_capabilities="push-task-branch = true",
                capability_bindings="""
                [capability_bindings.push-task-branch]
                remote = "origin"
                allowed_branch_prefixes = ["job/"]
                protected_branch_prefixes = ["main", "master", "release/"]
                """,
            )
        )
        subject = runner.Runner(config)
        rejected = subject.submit(
            self._policy_v2_job(
                job_id="sensitive-operational",
                target_sha=secret_sha,
                permission_profile="operational",
                capabilities=["push-task-branch"],
                scope={"root": "registered-checkout", "paths": []},
                network={"mode": "declared-remotes-and-registries"},
                verification_profiles=["review-readonly"],
            )
        )
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertEqual(rejected["error"]["code"], "sensitive_path_present")
        subject.close()

    def test_standard_write_rejects_modified_sensitive_path(self) -> None:
        verify_code = "raise SystemExit(0)"
        self._write_file(
            self.profiles_dir / "backend-unit.toml",
            f'command = ["python3", "-c", {json.dumps(verify_code)}]\ntimeout_seconds = 30\n',
        )
        subject = self._runner()
        submitted = subject.submit(self._job(job_id="sensitive-write", write=True, allowed_paths=[".env"], test_profile="backend-unit"))
        self.assertEqual(submitted["status"], "VALIDATED")
        result = subject.execute("sensitive-write", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "sensitive_path_present")
        subject.close()

    def test_sensitive_path_patterns_do_not_reject_normal_files(self) -> None:
        subject = self._runner()
        accepted = subject.submit(self._job(job_id="normal-file-job", target_sha=self.target_sha))
        self.assertEqual(accepted["status"], "VALIDATED")
        subject.close()

    def test_push_task_branch_is_deterministic_and_no_force(self) -> None:
        config = runner.RunnerConfig.load(
            self._write_config(
                extra_capabilities="push-task-branch = true",
                capability_bindings="""
                [capability_bindings.push-task-branch]
                remote = "origin"
                allowed_branch_prefixes = ["job/"]
                protected_branch_prefixes = ["main", "master", "release/"]
                """,
            )
        )
        subject = runner.Runner(config)
        job = self._policy_v2_job(
            job_id="push-task",
            permission_profile="operational",
            capabilities=["push-task-branch"],
            scope={"root": "registered-checkout", "paths": []},
            network={"mode": "declared-remotes-and-registries"},
            verification_profiles=["review-readonly"],
        )
        self.assertEqual(subject.submit(job)["status"], "VALIDATED")
        result = subject.execute("push-task", 1)
        self.assertEqual(result["status"], "DONE")
        self.assertIn("job/push-task", self._git(self.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads"))
        expected_hash = runner.sha256_hex(
            runner.canonical_json_bytes({"remote": "origin", "branch": "job/push-task", "commit_sha": self.target_sha})
        )
        self.assertEqual(result["result"]["diff_hash"], expected_hash)

        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        shadow = self.root / "shadow"
        self._run("git", "clone", str(self.origin), str(shadow))
        (shadow / "shadow.txt").write_text("shadow\n", encoding="utf-8")
        self._run("git", "-C", str(shadow), "checkout", "-b", "job/push-force", env=env)
        self._run("git", "-C", str(shadow), "add", "shadow.txt", env=env)
        self._run("git", "-C", str(shadow), "commit", "-m", "shadow", env=env)
        self._run("git", "-C", str(shadow), "push", "origin", "job/push-force", env=env)

        rejected = subject.submit(
            self._policy_v2_job(
                job_id="push-force",
                permission_profile="operational",
                capabilities=["push-task-branch"],
                scope={"root": "registered-checkout", "paths": []},
                network={"mode": "declared-remotes-and-registries"},
                verification_profiles=["review-readonly"],
                target_sha=self.target_sha,
            )
        )
        self.assertEqual(rejected["status"], "VALIDATED")
        failed = subject.execute("push-force", 1)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"]["code"], "push_requires_force")
        subject.close()

    def test_ledger_migrates_old_schema_in_place(self) -> None:
        db_path = self.root / "state" / "runner.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE jobs (
              job_id TEXT NOT NULL,
              attempt INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL,
              route TEXT,
              repo_id TEXT NOT NULL,
              write_enabled INTEGER NOT NULL,
              deadline_seconds INTEGER NOT NULL,
              lease_expires REAL,
              started_at REAL,
              finished_at REAL,
              result_json TEXT,
              error_json TEXT,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              PRIMARY KEY (job_id, attempt)
            )
            """
        )
        conn.execute(
            "CREATE TABLE job_events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, attempt INTEGER NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, note_json TEXT)"
        )
        conn.commit()
        conn.close()
        subject = self._runner()
        columns = {row[1] for row in subject.ledger.conn.execute("PRAGMA table_info(jobs)").fetchall()}
        self.assertIn("wire_payload_json", columns)
        self.assertIn("wire_payload_hash", columns)
        self.assertIn("permission_profile", columns)
        self.assertIn("network_mode", columns)
        subject.close()

    def test_legacy_job_rows_are_backfilled_and_replay_without_conflict(self) -> None:
        db_path = self.root / "state" / "runner.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_payload = self._job(job_id="legacy-replay", attempt=3)
        legacy_result = {
            "job_id": "legacy-replay",
            "attempt": 3,
            "status": "DONE",
            "route": "ornith",
            "findings": [],
            "test_exit_code": 0,
            "diff_hash": "0" * 64,
            "commit_sha": None,
            "duration_seconds": 0.1,
            "errors": [],
            "supervisor": {
                "decision": {"route": "ornith", "reason": "legacy"},
                "acceptance": {"accepted": True, "summary": "ok", "errors": []},
            },
            "artifacts": {
                "route_decision": "a",
                "worker_result": "b",
                "tests": "c",
                "acceptance": "d",
                "result": "e",
            },
        }
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE jobs (
              job_id TEXT NOT NULL,
              attempt INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL,
              route TEXT,
              repo_id TEXT NOT NULL,
              write_enabled INTEGER NOT NULL,
              deadline_seconds INTEGER NOT NULL,
              lease_expires REAL,
              started_at REAL,
              finished_at REAL,
              result_json TEXT,
              error_json TEXT,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              PRIMARY KEY (job_id, attempt)
            )
            """
        )
        conn.execute(
            "CREATE TABLE job_events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, attempt INTEGER NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, note_json TEXT)"
        )
        conn.execute(
            """
            INSERT INTO jobs (
              job_id, attempt, payload_json, status, route, repo_id, write_enabled, deadline_seconds,
              lease_expires, started_at, finished_at, result_json, error_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-replay",
                3,
                json.dumps(legacy_payload, sort_keys=True),
                "DONE",
                "ornith",
                "repo1",
                0,
                30,
                None,
                1.0,
                2.0,
                json.dumps(legacy_result, sort_keys=True),
                None,
                1.0,
                2.0,
            ),
        )
        for state in ("RECEIVED", "VALIDATED", "DONE"):
            conn.execute(
                "INSERT INTO job_events (job_id, attempt, state, created_at, note_json) VALUES (?, ?, ?, ?, ?)",
                ("legacy-replay", 3, state, 1.0, None),
            )
        conn.commit()
        conn.close()
        subject = self._runner()
        replay = subject.submit(legacy_payload)
        self.assertEqual(replay["status"], "DONE")
        self.assertEqual(replay["result"]["status"], "DONE")
        stored = subject.get("legacy-replay", 3)
        self.assertEqual(stored["payload"]["policy_version"], 1)
        self.assertEqual(stored["wire_payload"], legacy_payload)
        self.assertEqual(
            subject.ledger.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            1,
        )
        self.assertEqual(
            subject.ledger.conn.execute("SELECT COUNT(*) FROM job_events WHERE job_id = 'legacy-replay' AND attempt = 3").fetchone()[0],
            3,
        )
        subject.close()

    def test_verification_profiles_run_in_order_and_artifact_records_each_result(self) -> None:
        self._write_file(self.profiles_dir / "profile-first.toml", 'command = ["python3", "-c", "print(\'first\')"]\ntimeout_seconds = 30\n')
        self._write_file(self.profiles_dir / "profile-second.toml", 'command = ["python3", "-c", "print(\'second\'); raise SystemExit(7)"]\ntimeout_seconds = 30\n')
        config_path = self._write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'test_profiles = ["review-readonly", "backend-unit", "git-sync-verify", "self-update-runner"]',
                'test_profiles = ["review-readonly", "backend-unit", "git-sync-verify", "self-update-runner", "profile-first", "profile-second"]',
            ),
            encoding="utf-8",
        )
        subject = runner.Runner(runner.RunnerConfig.load(config_path))
        job = self._policy_v2_job(
            job_id="multi-profiles",
            permission_profile="observe",
            verification_profiles=["profile-first", "profile-second"],
        )
        self.assertEqual(subject.submit(job)["status"], "VALIDATED")
        result = subject.execute("multi-profiles", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "test_profile_failed")
        tests_artifact = json.loads((self.root / "share" / "artifacts" / "multi-profiles" / "1" / "tests.json").read_text(encoding="utf-8"))
        self.assertEqual([item["profile"] for item in tests_artifact["profiles"]], ["profile-first", "profile-second"])
        self.assertEqual([item["exit_code"] for item in tests_artifact["profiles"]], [0, 7])
        self.assertEqual([item["stdout"].strip() for item in tests_artifact["profiles"]], ["first", "second"])
        self.assertEqual(tests_artifact["exit_code"], 7)
        subject.close()

    def test_assert_exact_commit_fetches_only_for_declared_network(self) -> None:
        repo = self.config.repos["repo1"]
        commands: list[list[str]] = []
        original_run_command = runner.run_command

        def record(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(args)
            return original_run_command(args, **kwargs)

        subject = self._runner()
        try:
            with mock.patch.object(runner, "run_command", side_effect=record):
                subject.worktrees.assert_exact_commit(repo, self.target_sha, network_mode="none")
            self.assertFalse(any(command[:4] == ["git", "-C", str(repo.path), "fetch"] for command in commands))

            commands.clear()
            with mock.patch.object(runner, "run_command", side_effect=record):
                subject.worktrees.assert_exact_commit(
                    repo,
                    self.target_sha,
                    network_mode="declared-remotes-and-registries",
                )
            self.assertTrue(any(command[:4] == ["git", "-C", str(repo.path), "fetch"] for command in commands))
        finally:
            subject.close()

    def test_manage_pr_verifies_branch_and_final_pr_state(self) -> None:
        config_path = self._write_config(
            extra_capabilities="manage-pr = true",
            capability_bindings="""
            [capability_bindings.manage-pr]
            remote = "origin"
            allowed_branch_prefixes = ["job/"]
            protected_branch_prefixes = ["main", "master", "release/"]
            """,
        )
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(str(self.origin), "https://github.com/owner/repo.git"),
            encoding="utf-8",
        )
        subject = runner.Runner(runner.RunnerConfig.load(config_path))
        repo = subject.config.repos["repo1"]
        payload = subject.validate_dry_run(
            self._policy_v2_job(
                job_id="pr-job",
                permission_profile="operational",
                capabilities=["manage-pr"],
                scope={"root": "registered-checkout", "paths": []},
                network={"mode": "declared-remotes-and-registries"},
                verification_profiles=["review-readonly"],
                metadata={
                    "source_channel_id": "123e4567-e89b-12d3-a456-426614174000",
                    "source_event_id": "a" * 64,
                },
            )
        )["payload"]
        calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args[:6] == [runner.GIT_BIN, "-C", str(repo.path), "remote", "get-url", "--all"]:
                return subprocess.CompletedProcess(args, 0, stdout="https://github.com/owner/repo.git\n", stderr="")
            if args[:6] == [runner.GIT_BIN, "-C", str(repo.path), "ls-remote", "--heads", "origin"]:
                return subprocess.CompletedProcess(args, 0, stdout=f"{self.target_sha}\trefs/heads/job/pr-job\n", stderr="")
            if args[0].endswith("gh") and args[1:3] == ["pr", "view"] and "--json" in args:
                if len([call for call in calls if call[:3] == ["/usr/local/bin/gh", "pr", "view"]]) == 1:
                    return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"number": 42, "url": "https://github.com/owner/repo/pull/42", "headRefName": "job/pr-job", "baseRefName": "main"}), stderr="")
            if args[0].endswith("gh") and args[1:3] == ["pr", "create"]:
                return subprocess.CompletedProcess(args, 0, stdout="https://github.com/owner/repo/pull/42\n", stderr="")
            raise AssertionError(args)

        with mock.patch.object(runner.shutil, "which", return_value="/usr/local/bin/gh"), mock.patch.object(runner, "run_command", side_effect=fake_run):
            outcome = subject._capability_manage_pr(repo, payload, runner.Deadline(30))
        self.assertEqual(outcome["result"]["pr_number"], 42)
        self.assertEqual(outcome["result"]["pr_url"], "https://github.com/owner/repo/pull/42")
        self.assertTrue(any(args[:6] == [runner.GIT_BIN, "-C", str(repo.path), "ls-remote", "--heads", "origin"] for args in calls))
        subject.close()

    def test_restart_user_service_requires_pid_change_and_summarizes_only(self) -> None:
        config = runner.RunnerConfig.load(
            self._write_config(
                extra_capabilities="restart-user-service = true",
                capability_bindings="""
                [capability_bindings.restart-user-service]
                service_labels = ["com.example.buzz-acp"]
                """,
            )
        )
        subject = runner.Runner(config)
        calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args[:2] == ["launchctl", "print"]:
                if len([call for call in calls if call[:2] == ["launchctl", "print"]]) == 1:
                    return subprocess.CompletedProcess(args, 0, stdout="state = running\npid = 123\n", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="state = running\npid = 456\n", stderr="")
            if args[:2] == ["launchctl", "kickstart"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            raise AssertionError(args)

        payload = self._policy_v2_job(
            job_id="restart-service",
            permission_profile="operational",
            capabilities=["restart-user-service"],
            scope={"root": "registered-checkout", "paths": []},
            network={"mode": "none"},
            verification_profiles=["review-readonly"],
        )
        with mock.patch.object(runner, "run_command", side_effect=fake_run):
            result = subject._capability_restart_user_service(payload, runner.Deadline(30))
        self.assertEqual(result["result"]["before_pid"], 123)
        self.assertEqual(result["result"]["after_pid"], 456)
        self.assertNotIn("state = running", json.dumps(result))
        subject.close()

    def test_capability_verification_failure_does_not_dispatch_side_effects(self) -> None:
        self._write_file(self.profiles_dir / "profile-fail.toml", 'command = ["python3", "-c", "raise SystemExit(9)"]\ntimeout_seconds = 30\n')
        config_path = self._write_config(
            extra_capabilities="push-task-branch = true",
            capability_bindings="""
            [capability_bindings.push-task-branch]
            remote = "origin"
            allowed_branch_prefixes = ["job/"]
            protected_branch_prefixes = ["main", "master", "release/"]
            """,
        )
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'test_profiles = ["review-readonly", "backend-unit", "git-sync-verify", "self-update-runner"]',
                'test_profiles = ["review-readonly", "backend-unit", "git-sync-verify", "self-update-runner", "profile-fail"]',
            ),
            encoding="utf-8",
        )
        subject = runner.Runner(runner.RunnerConfig.load(config_path))
        job = self._policy_v2_job(
            job_id="capability-verify-fail",
            permission_profile="operational",
            capabilities=["push-task-branch"],
            scope={"root": "registered-checkout", "paths": []},
            network={"mode": "declared-remotes-and-registries"},
            verification_profiles=["profile-fail"],
        )
        self.assertEqual(subject.submit(job)["status"], "VALIDATED")
        with mock.patch.object(subject, "_dispatch_capability", side_effect=AssertionError("dispatch must not run")):
            result = subject.execute("capability-verify-fail", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "test_profile_failed")
        states = [
            row[0]
            for row in subject.ledger.conn.execute(
                "SELECT state FROM job_events WHERE job_id = ? AND attempt = ? ORDER BY id",
                ("capability-verify-fail", 1),
            ).fetchall()
        ]
        self.assertEqual(states, ["RECEIVED", "VALIDATED", "SUPERVISING", "PREPARING", "FAILED"])
        subject.close()

    def test_capability_cancelled_before_running_does_not_dispatch(self) -> None:
        config = runner.RunnerConfig.load(
            self._write_config(
                extra_capabilities="push-task-branch = true",
                capability_bindings="""
                [capability_bindings.push-task-branch]
                remote = "origin"
                allowed_branch_prefixes = ["job/"]
                protected_branch_prefixes = ["main", "master", "release/"]
                """,
            )
        )
        subject = runner.Runner(config)
        job = self._policy_v2_job(
            job_id="capability-cancel-race",
            permission_profile="operational",
            capabilities=["push-task-branch"],
            scope={"root": "registered-checkout", "paths": []},
            network={"mode": "declared-remotes-and-registries"},
            verification_profiles=["review-readonly"],
        )
        self.assertEqual(subject.submit(job)["status"], "VALIDATED")
        original_assert = subject._assert_not_cancelled
        call_count = {"value": 0}

        def cancel_on_second_check(job_id: str, attempt: int) -> None:
            call_count["value"] += 1
            if call_count["value"] == 2:
                subject.ledger.transition(job_id, attempt, {"PREPARING"}, "CANCELLED")
            return original_assert(job_id, attempt)

        with mock.patch.object(subject, "_assert_not_cancelled", side_effect=cancel_on_second_check), mock.patch.object(
            subject, "_dispatch_capability", side_effect=AssertionError("dispatch must not run")
        ):
            result = subject.execute("capability-cancel-race", 1)
        self.assertEqual(result["status"], "CANCELLED")
        subject.close()

    def test_sync_is_default_deny_when_repo_is_not_enabled(self) -> None:
        self._prepare_sync_repo()
        config_path = self._write_config()
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace("sync_enabled = true", "sync_enabled = false"),
            encoding="utf-8",
        )
        subject = runner.Runner(runner.RunnerConfig.load(config_path))
        result = subject.submit(self._sync_job(job_id="sync-disabled"))
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(result["error"]["code"], "sync_not_allowed")
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), self.base_sha)
        subject.close()

    def test_restart_recovers_validated_and_fails_stale_active_states(self) -> None:
        subject = self._runner(max_diff_bytes=128)
        recoverable = self._job(job_id="restart-validated")
        self.assertEqual(subject.submit(recoverable)["status"], "VALIDATED")
        subject.close()

        restarted = runner.Runner(self.config)
        recovered = restarted.execute("restart-validated", 1)
        self.assertEqual(recovered["status"], "DONE")
        restarted.close()

        transition_paths = {
            "SUPERVISING": ["SUPERVISING"],
            "PREPARING": ["SUPERVISING", "PREPARING"],
            "RUNNING": ["SUPERVISING", "PREPARING", "RUNNING"],
            "VERIFYING": ["SUPERVISING", "PREPARING", "RUNNING", "VERIFYING"],
        }
        previous = "VALIDATED"
        for index, (terminal_from, path) in enumerate(transition_paths.items(), start=1):
            job_id = f"restart-stale-{index}"
            subject = runner.Runner(self.config)
            self.assertEqual(subject.submit(self._job(job_id=job_id))["status"], "VALIDATED")
            previous = "VALIDATED"
            for state in path:
                subject.ledger.transition(job_id, 1, {previous}, state, route="ornith", lease_expires=0.0)
                previous = state
            subject.close()

            restarted = runner.Runner(self.config)
            failed = restarted.execute(job_id, 1)
            self.assertEqual(failed["status"], "FAILED", msg=terminal_from)
            expected_error = "verification_artifact_invalid" if terminal_from == "VERIFYING" else "stale_inflight_job"
            self.assertEqual(failed["error"]["code"], expected_error, msg=terminal_from)
            restarted.close()

        self.assertEqual(len([line for line in self._git(self.repo, "log", "--format=%s").splitlines() if line.startswith("job/")]), 0)

    def test_ornith_tool_loop_executes_and_readonly_stays_clean(self) -> None:
        subject = self._runner(max_diff_bytes=128)
        subject.submit(self.sample_job)
        result = subject.execute("agent-20260821-0042", 1)
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["result"]["route"], "ornith")
        self.assertIsNone(result["result"]["commit_sha"])
        config_proof = result["result"]["configuration"]
        expected_config_hash = runner.sha256_hex(self.config.config_path.read_bytes())
        self.assertTrue(config_proof["unchanged"])
        self.assertEqual(config_proof["before_sha256"], expected_config_hash)
        self.assertEqual(config_proof["after_sha256"], expected_config_hash)
        self.assertEqual(config_proof["loaded_sha256"], expected_config_hash)
        self.assertEqual({entry["task"] for entry in self._codex_entries()}, {"decide_route", "accept_result"})
        subject.close()

    def test_runner_rejects_configuration_change_during_execution(self) -> None:
        subject = self._runner(max_diff_bytes=4096)
        subject.submit(self._job(write=True, allowed_paths=["allowed.txt"], test_profile="backend-unit"))
        original_run_tests = subject._run_tests

        def mutate_config_after_tests(*args: object, **kwargs: object) -> dict[str, object]:
            tests = original_run_tests(*args, **kwargs)
            config_path = subject.config.config_path
            config_path.write_text(config_path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
            return tests

        with mock.patch.object(subject, "_run_tests", side_effect=mutate_config_after_tests), mock.patch.object(
            subject.worktrees, "commit", wraps=subject.worktrees.commit
        ) as commit:
            result = subject.execute("agent-20260821-0042", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "config_changed_during_execution")
        self.assertFalse(result["error"]["details"]["unchanged"])
        commit.assert_not_called()
        subject.close()

    def test_ornith_tool_budget_forces_final_json_without_more_tools(self) -> None:
        OllamaHandler.mode = "tool_budget"
        subject = self._runner(max_diff_bytes=128)
        subject.submit(self._job(job_id="tool-budget"))
        result = subject.execute("tool-budget", 1)
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["result"]["findings"][0]["title"], "Tool budget summarized")
        self.assertEqual(self._git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"), "")
        subject.close()

    def test_ornith_receives_nested_policy_v2_task_context(self) -> None:
        OllamaHandler.mode = "context_direct"
        subject = self._runner(max_diff_bytes=128)
        stored = subject.submit(
            self._policy_v2_job(
                job_id="nested-context",
                execution_route="ornith",
                preferred_worker="ornith",
                context={
                    "summary": "Minimal post-deploy canary.",
                    "instructions": "Do not call tools for this canary.",
                    "acceptance_criteria": ["Return one info finding"],
                },
            )
        )
        stored["route"] = "ornith"
        result = subject._run_worker(stored, self.repo, runner.Deadline(30))
        self.assertEqual(result["findings"][0]["title"], "Context honored")
        subject.close()

    def test_read_file_tool_honors_max_chars_with_truncation(self) -> None:
        executor = runner.OrnithToolExecutor(self.repo.resolve(), runner.Deadline(10), 4000)
        results = executor.execute(
            [
                {
                    "function": {
                        "name": "read_file",
                        "arguments": {"path": "README.md", "max_chars": 3},
                    }
                }
            ]
        )
        payload = json.loads(results[0]["content"])
        self.assertEqual(payload["content"], "tar")
        self.assertTrue(payload["truncated"])

    def test_readonly_tool_numeric_arguments_are_clamped_to_safe_schema_bounds(self) -> None:
        self._write_file(self.repo / "large.txt", "x" * 13000)
        executor = runner.OrnithToolExecutor(self.repo.resolve(), runner.Deadline(10), 20000)
        results = executor.execute(
            [
                {
                    "function": {
                        "name": "read_file",
                        "arguments": {"path": "large.txt", "max_chars": 50000},
                    }
                }
            ]
        )
        payload = json.loads(results[0]["content"])
        self.assertEqual(len(payload["content"]), 12000)
        self.assertTrue(payload["truncated"])

        with self.assertRaisesRegex(runner.RunnerError, "max_chars must be an integer"):
            executor.execute(
                [
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": "large.txt", "max_chars": True},
                        }
                    }
                ]
            )

    def test_readonly_tool_schema_matches_runtime_numeric_bounds(self) -> None:
        self.assertEqual(runner.DECISION_SCHEMA["properties"]["route"]["enum"], ["ornith", "codex"])
        definitions = {item["function"]["name"]: item["function"] for item in runner.READONLY_TOOL_DEFS}
        self.assertEqual(definitions["list_files"]["parameters"]["properties"]["limit"], {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
        })
        self.assertEqual(definitions["read_file"]["parameters"]["properties"]["max_chars"], {
            "type": "integer",
            "minimum": 1,
            "maximum": 12000,
        })
        self.assertEqual(definitions["rg"]["parameters"]["properties"]["max_matches"], {
            "type": "integer",
            "minimum": 1,
            "maximum": 200,
        })

    def test_explicit_execution_route_is_deterministic_without_route_model_call(self) -> None:
        subject = self._runner()
        job = self._job(execution_route="codex", preferred_worker="codex")
        submitted = subject.submit(job)
        with mock.patch.object(subject.supervisor, "_invoke_json") as invoke:
            decision = subject.supervisor.decide(submitted, subject.status(), runner.Deadline(30))
        self.assertEqual(decision["route"], "codex")
        self.assertIn("explicit codex", decision["reason"])
        invoke.assert_not_called()
        subject.close()

    def test_standard_worktree_rejects_explicit_ornith_route(self) -> None:
        subject = self._runner()
        rejected = subject.submit(
            self._job(
                write=True,
                allowed_paths=["allowed.txt"],
                execution_route="ornith",
                preferred_worker="ornith",
            )
        )
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertEqual(rejected["error"]["code"], "execution_route_not_allowed")
        subject.close()

    def test_deterministic_operational_job_rejects_explicit_worker_route(self) -> None:
        config = runner.RunnerConfig.load(
            self._write_config(extra_capabilities="restart-user-service = true")
        )
        subject = runner.Runner(config)
        rejected = subject.submit(
            self._policy_v2_job(
                job_id="explicit-operational-route",
                permission_profile="operational",
                capabilities=["restart-user-service"],
                scope={"root": "registered-checkout", "paths": []},
                network={"mode": "none"},
                execution_route="codex",
            )
        )
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertEqual(rejected["error"]["code"], "execution_route_not_allowed")
        subject.close()

    def test_policy_v2_sync_rejects_explicit_worker_route(self) -> None:
        config = runner.RunnerConfig.load(
            self._write_config(extra_capabilities="sync-registered-repo = true")
        )
        subject = runner.Runner(config)
        rejected = subject.submit(
            self._policy_v2_job(
                job_id="explicit-sync-route",
                task_type="sync",
                permission_profile="operational",
                capabilities=["sync-registered-repo"],
                scope={"root": "registered-checkout", "paths": []},
                network={"mode": "declared-remotes-and-registries"},
                verification_profiles=["git-sync-verify"],
                execution_route="ornith",
            )
        )
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertEqual(rejected["error"]["code"], "execution_route_not_allowed")
        subject.close()

    def test_supervisor_acceptance_recovers_without_rerunning_worker(self) -> None:
        subject = self._runner(max_diff_bytes=128)
        job = self._job(job_id="accept-retry")
        subject.submit(job)
        first = subject.execute("accept-retry", 1)
        self.assertEqual(first["status"], "VERIFYING")
        self.assertEqual(first["error"]["code"], "subprocess_failed")
        worker_artifact = self.root / "share" / "artifacts" / "accept-retry" / "1" / "worker-result.json"
        worker_mtime = worker_artifact.stat().st_mtime_ns
        subject.ledger.transition("accept-retry", 1, {"VERIFYING"}, "VERIFYING", lease_expires=0.0)
        subject.close()

        restarted = runner.Runner(self.config)
        recovered = restarted.execute("accept-retry", 1)
        self.assertEqual(recovered["status"], "DONE")
        self.assertIsNone(recovered["error"])
        self.assertEqual(worker_artifact.stat().st_mtime_ns, worker_mtime)
        self.assertEqual(
            [entry["task"] for entry in self._codex_entries()],
            ["decide_route", "accept_result", "accept_result"],
        )
        restarted.close()

    def test_raw_tool_markup_and_tool_path_escape_are_rejected(self) -> None:
        subject = self._runner()
        OllamaHandler.mode = "raw_tool_call"
        subject.submit(self.sample_job)
        result = subject.execute("agent-20260821-0042", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "ollama_native_tool_calls_missing")
        subject.close()

        OllamaHandler.mode = "path_escape_call"
        subject = self._runner()
        path_job = self._job(job_id="path-escape-job")
        subject.submit(path_job)
        result = subject.execute("path-escape-job", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "path_escape")
        subject.close()

    def test_ornith_tool_executor_normalizes_absolute_paths_inside_worktree(self) -> None:
        executor = runner.OrnithToolExecutor(self.repo, runner.Deadline(30), 4096)
        results = executor.execute(
            [
                {
                    "function": {
                        "name": "read_file",
                        "arguments": {"path": str(self.repo / "README.md"), "max_chars": 200},
                    }
                }
            ]
        )
        content = json.loads(results[0]["content"])
        self.assertEqual(content["path"], "README.md")

        with self.assertRaises(runner.RunnerError) as error:
            executor.execute(
                [
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": str(self.real_home / ".ssh" / "secret.txt")},
                        }
                    }
                ]
            )
        self.assertEqual(error.exception.code, "path_escape")

    def test_codex_write_route_edits_before_tests_and_commits_without_push(self) -> None:
        verify_code = "from pathlib import Path; p=Path('allowed.txt'); raise SystemExit(0 if p.read_text(encoding='utf-8').strip() == 'changed by fake codex' else 9)"
        self._write_file(
            self.profiles_dir / "backend-unit.toml",
            f'command = ["python3", "-c", {json.dumps(verify_code)}]\ntimeout_seconds = 30\n',
        )
        subject = self._runner()
        subject.submit(self._job(write=True, allowed_paths=["allowed.txt"], test_profile="backend-unit"))
        result = subject.execute("agent-20260821-0042", 1)
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["result"]["route"], "codex")
        self.assertRegex(result["result"]["commit_sha"], r"^[0-9a-f]{40}$")
        self.assertTrue(result["result"]["supervisor"]["acceptance"]["accepted"])
        self.assertEqual(
            sorted(result["result"]["artifacts"]),
            ["acceptance", "result", "route_decision", "tests", "worker_result"],
        )
        self.assertNotIn("job/agent-20260821-0042", self._git(self.repo, "ls-remote", "--heads", "origin"))
        entries = self._codex_entries()
        self.assertEqual([entry["task"] for entry in entries], ["decide_route", "execute_job", "accept_result"])
        self.assertIn("--skip-git-repo-check", entries[0]["argv"])
        self.assertIn("--skip-git-repo-check", entries[1]["argv"])
        self.assertNotIn("--model", entries[0]["argv"])
        subject.close()

    def test_codex_write_route_rejects_changes_outside_allowed_paths(self) -> None:
        verify_code = "raise SystemExit(0)"
        self._write_file(
            self.profiles_dir / "backend-unit.toml",
            f'command = ["python3", "-c", {json.dumps(verify_code)}]\ntimeout_seconds = 30\n',
        )
        subject = self._runner()
        subject.submit(self._job(job_id="escape-write", write=True, allowed_paths=["allowed.txt"], test_profile="backend-unit"))
        result = subject.execute("escape-write", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "path_escape")
        self.assertFalse((self.repo / "outside.txt").exists())
        subject.close()

    def test_codex_write_route_rejects_symlink_bearing_repo(self) -> None:
        external_target = self.real_home / ".ssh" / "secret.txt"
        os.symlink(external_target, self.repo / "escape-link")
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        self._run("git", "-C", str(self.repo), "add", "escape-link", env=env)
        self._run("git", "-C", str(self.repo), "commit", "-m", "add escape symlink", env=env)
        symlink_sha = self._git(self.repo, "rev-parse", "HEAD")
        self._run("git", "-C", str(self.repo), "push", "origin", "HEAD", env=env)

        subject = self._runner()
        subject.submit(
            self._job(
                job_id="symlink-write",
                target_sha=symlink_sha,
                write=True,
                allowed_paths=["allowed.txt"],
                test_profile="backend-unit",
            )
        )
        result = subject.execute("symlink-write", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "path_escape")
        self.assertEqual(external_target.read_text(encoding="utf-8"), "top-secret\n")
        subject.close()

    def test_write_tasks_disabled_and_diff_limit_fail(self) -> None:
        disabled = self._runner(allow_write_tasks=False)
        rejected = disabled.submit(self._job(write=True, allowed_paths=["allowed.txt"]))
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertEqual(rejected["error"]["code"], "write_tasks_disabled")
        disabled.close()

        writer = self.app_dir / "write_big.py"
        self._write_file(
            writer,
            "from pathlib import Path\nPath('allowed.txt').write_text('x'*400, encoding='utf-8')\nraise SystemExit(0)\n",
        )
        self._write_file(self.profiles_dir / "backend-unit.toml", f'command = ["python3", "{writer}"]\ntimeout_seconds = 30\n')
        subject = self._runner(max_diff_bytes=128)
        diff_job = self._job(job_id="diff-job", write=True, allowed_paths=["allowed.txt"], test_profile="backend-unit")
        subject.submit(diff_job)
        result = subject.execute("diff-job", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "diff_too_large")
        subject.close()

    def test_readonly_dirty_worktree_and_test_failure_are_rejected(self) -> None:
        dirty_code = "from pathlib import Path; Path('junk.txt').write_text('junk', encoding='utf-8')"
        self._write_file(
            self.profiles_dir / "review-readonly.toml",
            f'command = ["python3", "-c", {json.dumps(dirty_code)}]\ntimeout_seconds = 30\n',
        )
        subject = self._runner()
        subject.submit(self.sample_job)
        result = subject.execute("agent-20260821-0042", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "readonly_dirty_worktree")
        subject.close()

        self._write_file(self.profiles_dir / "review-readonly.toml", 'command = ["python3", "-c", "raise SystemExit(7)"]\ntimeout_seconds = 30\n')
        subject = self._runner()
        fail_job = self._job(job_id="test-fail-job")
        subject.submit(fail_job)
        result = subject.execute("test-fail-job", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "test_profile_failed")
        subject.close()

    def test_temp_home_and_seatbelt_block_real_home_secrets(self) -> None:
        check_home_code = textwrap.dedent(
            f"""
            import os
            from pathlib import Path
            if os.environ["HOME"] == {str(self.real_home)!r}:
                raise SystemExit(8)
            if not os.environ["TMPDIR"].startswith(os.environ["HOME"] + "/"):
                raise SystemExit(10)
            if os.environ["CFFIXED_USER_HOME"] != os.environ["HOME"]:
                raise SystemExit(11)
            try:
                Path({str(self.real_home / '.ssh' / 'secret.txt')!r}).read_text(encoding="utf-8")
                raise SystemExit(9)
            except Exception:
                raise SystemExit(0)
            """
        ).strip()
        self._write_file(
            self.profiles_dir / "review-readonly.toml",
            f'command = ["python3", "-c", {json.dumps(check_home_code)}]\ntimeout_seconds = 30\n',
        )
        subject = self._runner()
        subject.submit(self.sample_job)
        result = subject.execute("agent-20260821-0042", 1)
        self.assertEqual(result["status"], "DONE")
        sandboxes = self.root / "state" / "test-sandboxes"
        leftovers = [path for path in sandboxes.iterdir()] if sandboxes.exists() else []
        self.assertEqual(leftovers, [])
        log_content = (self.root / "state" / "runner.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("secret-openai", log_content)
        self.assertNotIn("secret-codex", log_content)
        subject.close()

    @unittest.skipUnless(Path("/usr/bin/sandbox-exec").exists(), "requires macOS Seatbelt")
    def test_seatbelt_allows_read_only_git_metadata_for_disposable_worktree(self) -> None:
        worktree = self.root / "seatbelt-worktree"
        self._run("git", "-C", str(self.repo), "worktree", "add", "--detach", str(worktree), self.base_sha)
        try:
            with runner.TestSandbox(self.root / "state-seatbelt-git", worktree) as sandbox:
                result = runner.run_command(
                    sandbox.wrap(["/usr/bin/git", "diff", "--check", "HEAD"]),
                    cwd=worktree,
                    env=sandbox.env(),
                    timeout=30,
                    check=False,
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("xcrun_db", result.stderr)
        finally:
            self._run(
                "git",
                "-C",
                str(self.repo),
                "worktree",
                "remove",
                "--force",
                str(worktree),
                check=False,
            )
            self._run("git", "-C", str(self.repo), "worktree", "prune", check=False)

    def test_test_sandbox_isolates_darwin_cache_roots_for_readonly_profiles(self) -> None:
        with runner.TestSandbox(self.root / "state-darwin-cache", self.repo) as sandbox:
            self.assertIsNotNone(sandbox.home_dir)
            self.assertIsNotNone(sandbox.temp_dir)
            self.assertIsNotNone(sandbox.darwin_user_cache_dir)
            self.assertIsNotNone(sandbox.system_temp_dir)
            developer_tools_cache = sandbox.darwin_user_cache_dir / "com.apple.DeveloperTools"
            clang_cache = sandbox.darwin_user_cache_dir / "clang"
            self.assertTrue(developer_tools_cache.is_dir())
            self.assertTrue(clang_cache.is_dir())
            profile = sandbox.profile_path.read_text(encoding="utf-8")
            self.assertIn(str(sandbox.temp_dir), profile)
            self.assertIn(str(sandbox.darwin_user_cache_dir), profile)
            self.assertIn(str(developer_tools_cache), profile)
            self.assertIn(str(clang_cache), profile)
            read_section = profile.split("(allow file-read*", 1)[1].split("(allow file-write*", 1)[0]
            write_section = profile.split("(allow file-write*", 1)[1].split("(deny file-write*", 1)[0]
            system_temp_rule = f"(subpath {json.dumps(str(sandbox.system_temp_dir))})"
            xcrun_cache_rule = f"(literal {json.dumps(str(sandbox.system_temp_dir / 'xcrun_db'))})"
            self.assertNotIn(system_temp_rule, read_section)
            self.assertIn(xcrun_cache_rule, read_section)
            self.assertIn(system_temp_rule, write_section)
            env = sandbox.env()
            self.assertEqual(env["TMPDIR"], f"{sandbox.temp_dir}/")
            self.assertEqual(env["DARWIN_USER_CACHE_DIR"], f"{sandbox.darwin_user_cache_dir}/")
            self.assertEqual(env["CFFIXED_USER_HOME"], str(sandbox.home_dir))
        self.assertFalse(developer_tools_cache.exists())

    def test_test_sandbox_reads_selected_versioned_xcode_bundle_without_write_access(self) -> None:
        developer_dir = "/Applications/Xcode_26.6.app/Contents/Developer"
        with mock.patch.dict(os.environ, {"DEVELOPER_DIR": developer_dir}):
            with runner.TestSandbox(self.root / "state-versioned-xcode", self.repo) as sandbox:
                profile = sandbox.profile_path.read_text(encoding="utf-8")
                self.assertIn('(subpath "/Applications/Xcode_26.6.app")', profile)
                write_section = profile.split("(allow file-write*", 1)[1].split("(deny file-write*", 1)[0]
                self.assertNotIn("Xcode_26.6.app", write_section)
        with mock.patch.dict(os.environ, {"DEVELOPER_DIR": "/Users/shared/FakeXcode.app/Contents/Developer"}):
            self.assertNotIn(Path("/Users/shared/FakeXcode.app"), runner.TestSandbox._xcode_read_roots())

    def test_test_sandbox_resolves_linked_worktree_git_metadata_roots(self) -> None:
        worktree = self.root / "metadata-worktree"
        self._run("git", "-C", str(self.repo), "worktree", "add", "--detach", str(worktree), self.base_sha)
        try:
            sandbox = runner.TestSandbox(self.root / "state-metadata-git", worktree)
            roots = sandbox._git_metadata_read_roots()
            self.assertIn((self.repo / ".git").resolve(), roots)
            self.assertTrue(all(path.is_dir() for path in roots))
        finally:
            self._run(
                "git",
                "-C",
                str(self.repo),
                "worktree",
                "remove",
                "--force",
                str(worktree),
                check=False,
            )
            self._run("git", "-C", str(self.repo), "worktree", "prune", check=False)

    def test_test_sandbox_denies_network(self) -> None:
        host, port = self.server.server_address
        network_code = textwrap.dedent(
            f"""
            import socket
            try:
                socket.create_connection(({host!r}, {port}), timeout=1)
            except Exception:
                raise SystemExit(0)
            raise SystemExit(9)
            """
        ).strip()
        self._write_file(
            self.profiles_dir / "review-readonly.toml",
            f'command = ["python3", "-c", {json.dumps(network_code)}]\ntimeout_seconds = 30\n',
        )
        subject = self._runner()
        subject.submit(self.sample_job)
        result = subject.execute("agent-20260821-0042", 1)
        self.assertEqual(result["status"], "DONE")
        subject.close()

    def test_xcode_derived_data_placeholder_is_isolated_and_cleaned(self) -> None:
        sandbox = runner.TestSandbox(self.root / "state", self.repo, needs_xcode_derived_data=True)
        with sandbox:
            expanded = sandbox.expand(["xcodebuild", "-derivedDataPath", "{xcode_derived_data}"])
            derived_data = Path(expanded[-1])
            self.assertTrue(derived_data.is_dir())
            profile = sandbox.profile_path.read_text(encoding="utf-8")
            self.assertIn(str(derived_data), profile)
            self.assertIn("com.apple.CoreSimulator.simdiskimaged", profile)
            self.assertIn("com.apple.dt.xcodebuild", profile)
        self.assertFalse(derived_data.exists())

    def test_malformed_payload_is_structured_rejection(self) -> None:
        subject = self._runner()
        rejected = subject.submit({"schema": "mac-job/v1"})
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertEqual(rejected["error"]["code"], "schema_validation_failed")
        subject.close()

    def test_production_source_metadata_gate_accepts_canonical_and_legacy_references(self) -> None:
        subject = runner.Runner(
            runner.RunnerConfig.load(self._write_config(require_source_metadata=True))
        )
        missing = subject.submit(self._job(job_id="missing-source"))
        self.assertEqual(missing["status"], "REJECTED")
        self.assertEqual(missing["error"]["code"], "source_reference_required")

        canonical = subject.submit(
            self._job(
                job_id="canonical-source",
                metadata={
                    "source_channel_id": "123e4567-e89b-12d3-a456-426614174000",
                    "source_event_id": "a" * 64,
                },
            )
        )
        self.assertEqual(canonical["status"], "VALIDATED")

        legacy = subject.submit(
            self._job(
                job_id="legacy-source",
                context={
                    "metadata": {
                        "source_channel_id": "123e4567-e89b-12d3-a456-426614174000",
                        "source_event_id": "b" * 64,
                    }
                },
            )
        )
        self.assertEqual(legacy["status"], "VALIDATED")
        subject.close()

    def test_serve_reconciles_expired_inflight_job_without_new_pending_work(self) -> None:
        subject = self._runner()
        subject.submit(self._job(job_id="stale-without-pending"))
        subject.ledger.transition(
            "stale-without-pending",
            1,
            {"VALIDATED"},
            "SUPERVISING",
            lease_expires=0.0,
        )
        status_calls = 0

        def status_then_stop() -> dict[str, object]:
            nonlocal status_calls
            status_calls += 1
            subject.shutdown_requested = True
            return {"queue": {"running": 0, "pending": 0, "retryable": 0}}

        with mock.patch.object(subject, "status", side_effect=status_then_stop):
            subject.serve(poll_seconds=1, heartbeat_seconds=60, busy_summary_seconds=60)
        self.assertEqual(status_calls, 1)
        failed = subject.get("stale-without-pending", 1)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"]["code"], "stale_inflight_job")
        subject.close()

    def test_deadline_applies_to_subprocesses(self) -> None:
        subject = self._runner()
        subject.submit(self._job(job_id="slow-job", write=True, allowed_paths=["allowed.txt"], deadline_seconds=1))
        result = subject.execute("slow-job", 1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "subprocess_timeout")
        subject.close()

    def test_status_reports_real_host_fields_and_stale_job_fails(self) -> None:
        subject = self._runner()
        status = subject.status()
        self.assertIn(status["host"]["memory_pressure"], {"normal", "warning", "critical", "unknown"})
        self.assertIn(status["host"]["power"], {"ac", "battery", "unknown"})
        self.assertIn(status["host"]["thermal"], {"nominal", "warning", "critical", "unknown"})
        self.assertTrue(status["ollama"]["ready"])
        subject.submit(self.sample_job)
        subject.ledger.transition("agent-20260821-0042", 1, {"VALIDATED"}, "SUPERVISING", lease_expires=0.0)
        failed = subject.ledger.mark_stale_active_jobs_failed()
        self.assertEqual(failed[0]["status"], "FAILED")
        subject.close()

    def test_memory_pressure_prefers_system_free_percentage(self) -> None:
        output = textwrap.dedent(
            """
            Pages free: 10
            Pages active: 1000
            Pages inactive: 1000
            System-wide memory free percentage: 80%
            """
        )
        completed = subprocess.CompletedProcess(["memory_pressure"], 0, stdout=output, stderr="")
        collector = runner.HostStatusCollector(self.config)
        with mock.patch.object(runner, "run_command", return_value=completed):
            self.assertEqual(collector._memory_pressure(), "normal")

    def test_model_flag_and_cleanup_admin_entries(self) -> None:
        subject = self._runner(model="gpt-5.6-sol")
        subject.submit(self.sample_job)
        result = subject.execute("agent-20260821-0042", 1)
        self.assertEqual(result["status"], "DONE")
        entries = self._codex_entries()
        self.assertIn("--model", entries[0]["argv"])
        self.assertIn("gpt-5.6-sol", entries[0]["argv"])
        worktree_list = self._git(self.repo, "worktree", "list")
        self.assertEqual(len([line for line in worktree_list.splitlines() if line.strip()]), 1)
        subject.close()

    def test_serve_monitor_only_without_repos(self) -> None:
        config_path = self._write_no_repo_config()
        proc = subprocess.Popen(
            [
                "/opt/homebrew/bin/python3",
                str(self.app_dir / "runner.py"),
                "--config",
                str(config_path),
                "serve",
                "--poll-seconds",
                "1",
                "--heartbeat-seconds",
                "1",
                "--busy-summary-seconds",
                "1",
            ],
            cwd=self.app_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(2)
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")
        log_path = self.root / "state-no-repo" / "runner.jsonl"
        log = log_path.read_text(encoding="utf-8")
        self.assertIn('"event": "heartbeat"', log)
        self.assertIn('"event": "status_changed"', log)
        self.assertNotIn('"event": "serve_execute_pending"', log)


if __name__ == "__main__":
    unittest.main()

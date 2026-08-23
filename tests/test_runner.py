from __future__ import annotations

import http.server
import json
import os
import signal
import socketserver
import subprocess
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
                assert messages[-1]["role"] == "user"
                assert "tool budget is exhausted" in messages[-1]["content"]
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

        self.config_path = self._write_config()
        self.config = runner.RunnerConfig.load(self.config_path)
        self.sample_job = self._job()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_env)
        self.server.shutdown()
        self.server.server_close()
        self.tempdir.cleanup()

    def _copy_fixture(self, name: str) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        source = repository_root / ("schemas" if name.endswith("_schema.json") else "") / name
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
        self._run("git", "init", "--bare", str(self.origin))
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

    def _write_config(self, *, allow_write_tasks: bool = True, model: str = "", max_diff_bytes: int = 4096) -> Path:
        config_path = self.app_dir / "config.toml"
        config = textwrap.dedent(
            f"""
            [runner]
            state_dir = "{self.root / 'state'}"
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

            [capabilities]
            image_generation = false

            [repos.repo1]
            path = "{self.repo}"
            fetch_remote = "origin"
            test_profiles = ["review-readonly", "backend-unit"]
            """
        ).strip()
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

    def _runner(self, *, allow_write_tasks: bool = True, model: str = "", max_diff_bytes: int = 4096) -> runner.Runner:
        config = runner.RunnerConfig.load(self._write_config(allow_write_tasks=allow_write_tasks, model=model, max_diff_bytes=max_diff_bytes))
        self.config = config
        return runner.Runner(config)

    def _codex_entries(self) -> list[dict[str, object]]:
        if not self.codex_log.exists():
            return []
        return [json.loads(line) for line in self.codex_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _fake_codex_script(self) -> str:
        return textwrap.dedent(
            f"""#!/opt/homebrew/bin/python3
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
        target = Path(job["allowed_paths"][0])
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
        self.assertEqual({entry["task"] for entry in self._codex_entries()}, {"decide_route", "accept_result"})
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

    def test_readonly_tool_schema_matches_runtime_numeric_bounds(self) -> None:
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

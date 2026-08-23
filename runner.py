#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import errno
import fcntl
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator


TERMINAL_STATES = {"DONE", "FAILED", "REJECTED", "CANCELLED"}
ACTIVE_STATES = {"SUPERVISING", "PREPARING", "RUNNING", "VERIFYING"}
STATE_SEQUENCE = [
    "RECEIVED",
    "VALIDATED",
    "SUPERVISING",
    "PREPARING",
    "RUNNING",
    "VERIFYING",
    "DONE",
]
SECRET_ENV_KEYS = {
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "ANTHROPIC_API_KEY",
    "BUZZ_PRIVATE_KEY",
    "BUZZ_SECRET_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_API_KEY",
    "GITHUB_TOKEN",
}
READONLY_TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "repo_status",
            "description": "Return git status summary for the current worktree.",
            "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List repository files under a relative path.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the worktree.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": 12000},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rg",
            "description": "Run ripgrep under a relative path.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "max_matches": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["pattern"],
            },
        },
    },
]
DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["route", "reason"],
    "properties": {
        "route": {"type": "string", "enum": ["ornith", "codex"]},
        "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
}
ACCEPTANCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["accepted", "summary", "errors"],
    "properties": {
        "accepted": {"type": "boolean"},
        "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
        "errors": {
            "type": "array",
            "items": {"type": "string", "maxLength": 1000},
            "maxItems": 64,
        },
    },
}
WORKER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "title", "detail"],
                "properties": {
                    "severity": {"type": "string", "enum": ["info", "warning", "error"]},
                    "title": {"type": "string", "minLength": 1, "maxLength": 256},
                    "detail": {"type": "string", "minLength": 1, "maxLength": 4000},
                },
            },
            "maxItems": 256,
        }
    },
}


class RunnerError(Exception):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        payload = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


def utc_now() -> float:
    return time.time()


def json_dumps(data: Any) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=True)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_subprocess_env(*, allow_credentials: bool = False, home: str | None = None) -> dict[str, str]:
    base: dict[str, str] = {}
    passthrough = ["LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP"]
    for key in passthrough:
        value = os.environ.get(key)
        if value:
            base[key] = value
    base["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    base["HOME"] = home or os.environ.get("HOME", str(Path.home()))
    if allow_credentials:
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            value = os.environ.get(key)
            if value:
                base[key] = value
    return base


def trim_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 32] + "\n...[truncated]..."


def parse_json_object_text(value: str) -> dict[str, Any]:
    stripped = value.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].strip().lower() in {"```", "```json"}:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise RunnerError("invalid_json", "Model response must contain one JSON object") from exc
    if not isinstance(parsed, dict):
        raise RunnerError("invalid_json", "Model response must be a JSON object")
    return parsed


def run_command(
    args: list[str],
    *,
    env: dict[str, str],
    cwd: Path | None = None,
    timeout: float | None = None,
    stdin: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )
    except subprocess.TimeoutExpired as exc:
        raise RunnerError("subprocess_timeout", f"Command timed out: {' '.join(args[:4])}") from exc
    except subprocess.CalledProcessError as exc:
        raise RunnerError(
            "subprocess_failed",
            f"Command failed with exit code {exc.returncode}: {args[0]}",
            details={
                "returncode": exc.returncode,
                "stdout": trim_text(exc.stdout or "", 4000),
                "stderr": trim_text(exc.stderr or "", 4000),
            },
        ) from exc
    except FileNotFoundError as exc:
        raise RunnerError("command_not_found", f"Approved executable was not found: {args[0]}") from exc


class SimpleSchemaValidator:
    def __init__(self, schema: dict[str, Any]) -> None:
        self.schema = schema

    def validate(self, data: Any) -> None:
        self._validate_node(self.schema, data, "$")

    def _validate_node(self, schema: dict[str, Any], value: Any, path: str) -> None:
        schema_type = schema.get("type")
        if isinstance(schema_type, list):
            errors: list[str] = []
            for candidate in schema_type:
                try:
                    self._validate_node({**schema, "type": candidate}, value, path)
                    return
                except RunnerError as exc:
                    errors.append(exc.message)
            raise RunnerError("schema_validation_failed", f"{path} does not match any allowed type: {errors}")
        if schema_type == "object":
            if not isinstance(value, dict):
                raise RunnerError("schema_validation_failed", f"{path} must be an object")
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))
            for key in required:
                if key not in value:
                    raise RunnerError("schema_validation_failed", f"{path}.{key} is required")
            if schema.get("additionalProperties", True) is False:
                extra = sorted(set(value) - set(properties))
                if extra:
                    raise RunnerError("schema_validation_failed", f"{path} has unknown fields: {', '.join(extra)}")
            for key, child in properties.items():
                if key in value:
                    self._validate_node(child, value[key], f"{path}.{key}")
        elif schema_type == "array":
            if not isinstance(value, list):
                raise RunnerError("schema_validation_failed", f"{path} must be an array")
            min_items = schema.get("minItems")
            max_items = schema.get("maxItems")
            if min_items is not None and len(value) < min_items:
                raise RunnerError("schema_validation_failed", f"{path} must contain at least {min_items} items")
            if max_items is not None and len(value) > max_items:
                raise RunnerError("schema_validation_failed", f"{path} must contain at most {max_items} items")
            item_schema = schema.get("items")
            if item_schema:
                for index, item in enumerate(value):
                    self._validate_node(item_schema, item, f"{path}[{index}]")
        elif schema_type == "string":
            if not isinstance(value, str):
                raise RunnerError("schema_validation_failed", f"{path} must be a string")
            min_length = schema.get("minLength")
            max_length = schema.get("maxLength")
            if min_length is not None and len(value) < min_length:
                raise RunnerError("schema_validation_failed", f"{path} must be at least {min_length} chars")
            if max_length is not None and len(value) > max_length:
                raise RunnerError("schema_validation_failed", f"{path} must be at most {max_length} chars")
            pattern = schema.get("pattern")
            if pattern:
                import re

                if not re.fullmatch(pattern, value):
                    raise RunnerError("schema_validation_failed", f"{path} does not match required pattern")
        elif schema_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise RunnerError("schema_validation_failed", f"{path} must be an integer")
            self._validate_number(schema, float(value), path)
        elif schema_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RunnerError("schema_validation_failed", f"{path} must be a number")
            self._validate_number(schema, float(value), path)
        elif schema_type == "boolean":
            if not isinstance(value, bool):
                raise RunnerError("schema_validation_failed", f"{path} must be a boolean")
        elif schema_type == "null":
            if value is not None:
                raise RunnerError("schema_validation_failed", f"{path} must be null")
        elif schema_type is not None:
            raise RunnerError("schema_validation_failed", f"{path} uses unsupported schema type {schema_type}")
        if "enum" in schema and value not in schema["enum"]:
            raise RunnerError("schema_validation_failed", f"{path} must be one of {schema['enum']}")

    def _validate_number(self, schema: dict[str, Any], value: float, path: str) -> None:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise RunnerError("schema_validation_failed", f"{path} must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise RunnerError("schema_validation_failed", f"{path} must be <= {maximum}")


class Deadline:
    def __init__(self, duration_seconds: int) -> None:
        self.started_at = utc_now()
        self.ends_at = self.started_at + duration_seconds

    def remaining(self, limit: float | None = None) -> float:
        value = self.ends_at - utc_now()
        if value <= 0:
            raise RunnerError("deadline_exceeded", "Job deadline exceeded")
        if limit is not None:
            value = min(value, limit)
        return max(0.001, value)

    def elapsed(self) -> float:
        return utc_now() - self.started_at


@dataclasses.dataclass(frozen=True)
class RepoConfig:
    repo_id: str
    path: Path
    fetch_remote: str | None
    test_profiles: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class TestProfile:
    name: str
    command: list[str]
    timeout_seconds: int
    description: str


@dataclasses.dataclass
class RunnerConfig:
    app_dir: Path
    state_dir: Path
    db_path: Path
    worktree_root: Path
    artifacts_root: Path
    log_path: Path
    profiles_dir: Path
    job_schema_path: Path
    result_schema_path: Path
    max_changed_files: int
    max_diff_bytes: int
    active_lease_seconds: int
    ollama_endpoint: str
    ollama_model: str
    ollama_timeout_seconds: int
    ollama_max_output_chars: int
    ollama_native_tool_calls_required: bool
    supervisor_codex_path: str
    supervisor_model: str | None
    supervisor_allow_write_tasks: bool
    capabilities: dict[str, bool]
    repos: dict[str, RepoConfig]

    @classmethod
    def load(cls, config_path: Path) -> "RunnerConfig":
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        app_dir = config_path.parent
        runner_cfg = data["runner"]
        ollama_cfg = data["ollama"]
        supervisor_cfg = data["supervisor"]
        raw_capabilities = data.get("capabilities", {})
        if not isinstance(raw_capabilities, dict) or not all(
            isinstance(name, str) and name and isinstance(enabled, bool)
            for name, enabled in raw_capabilities.items()
        ):
            raise RunnerError("invalid_config", "capabilities must be a TOML table of boolean flags")
        repos = {
            repo_id: RepoConfig(
                repo_id=repo_id,
                path=Path(entry["path"]).expanduser().resolve(),
                fetch_remote=entry.get("fetch_remote"),
                test_profiles=tuple(entry.get("test_profiles", [])),
            )
            for repo_id, entry in data.get("repos", {}).items()
        }
        raw_model = str(supervisor_cfg.get("model", "")).strip()
        return cls(
            app_dir=app_dir,
            state_dir=Path(runner_cfg["state_dir"]).expanduser(),
            db_path=Path(runner_cfg["db_path"]).expanduser(),
            worktree_root=Path(runner_cfg["worktree_root"]).expanduser(),
            artifacts_root=Path(runner_cfg["artifacts_dir"]).expanduser(),
            log_path=Path(runner_cfg["log_path"]).expanduser(),
            profiles_dir=Path(runner_cfg["profiles_dir"]).expanduser(),
            job_schema_path=Path(runner_cfg["job_schema_path"]).expanduser(),
            result_schema_path=Path(runner_cfg["result_schema_path"]).expanduser(),
            max_changed_files=int(runner_cfg["max_changed_files"]),
            max_diff_bytes=int(runner_cfg["max_diff_bytes"]),
            active_lease_seconds=int(runner_cfg["active_lease_seconds"]),
            ollama_endpoint=str(ollama_cfg["endpoint"]).rstrip("/"),
            ollama_model=str(ollama_cfg["model"]),
            ollama_timeout_seconds=int(ollama_cfg["timeout_seconds"]),
            ollama_max_output_chars=int(ollama_cfg["max_output_chars"]),
            ollama_native_tool_calls_required=bool(ollama_cfg["native_tool_calls_required"]),
            supervisor_codex_path=str(supervisor_cfg["codex_path"]),
            supervisor_model=raw_model or None,
            supervisor_allow_write_tasks=bool(supervisor_cfg["allow_write_tasks"]),
            capabilities=dict(sorted(raw_capabilities.items())),
            repos=repos,
        )


class Ledger:
    def __init__(self, db_path: Path, log_path: Path) -> None:
        self.db_path = db_path
        self.log_path = log_path
        ensure_parent(db_path)
        ensure_parent(log_path)
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
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
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL,
              attempt INTEGER NOT NULL,
              state TEXT NOT NULL,
              created_at REAL NOT NULL,
              note_json TEXT
            )
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _log(self, event: str, **fields: Any) -> None:
        record = {"ts": utc_now(), "event": event, **fields}
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json_dumps(record) + "\n")

    def insert_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        canonical_payload = json_dumps(payload)
        changed_before = self.conn.total_changes
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO jobs (
                  job_id, attempt, payload_json, status, repo_id, write_enabled,
                  deadline_seconds, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["job_id"],
                    payload["attempt"],
                    canonical_payload,
                    "RECEIVED",
                    payload["repo_id"],
                    1 if payload["write"] else 0,
                    payload["deadline_seconds"],
                    now,
                    now,
                ),
            )
            inserted = self.conn.total_changes > changed_before
            row = self.conn.execute(
                "SELECT payload_json FROM jobs WHERE job_id = ? AND attempt = ?",
                (payload["job_id"], payload["attempt"]),
            ).fetchone()
            if row is None:
                raise RunnerError("ledger_error", "Failed to fetch inserted job")
            if row["payload_json"] != canonical_payload:
                raise RunnerError(
                    "idempotency_conflict",
                    f"Job {payload['job_id']}/{payload['attempt']} already exists with different payload",
                )
            if inserted:
                self.conn.execute(
                    "INSERT INTO job_events (job_id, attempt, state, created_at, note_json) VALUES (?, ?, ?, ?, ?)",
                    (payload["job_id"], payload["attempt"], "RECEIVED", now, None),
                )
        self._log("job_inserted", job_id=payload["job_id"], attempt=payload["attempt"])
        return self.get_job(payload["job_id"], payload["attempt"])  # type: ignore[return-value]

    def get_job(self, job_id: str, attempt: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE job_id = ? AND attempt = ?",
            (job_id, attempt),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["result"] = json.loads(result["result_json"]) if result.get("result_json") else None
        result["error"] = json.loads(result["error_json"]) if result.get("error_json") else None
        result.pop("result_json", None)
        result.pop("error_json", None)
        return result

    def transition(
        self,
        job_id: str,
        attempt: int,
        expected: set[str],
        new_state: str,
        *,
        route: str | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        note: dict[str, Any] | None = None,
        lease_expires: float | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.conn:
            current = self.conn.execute(
                "SELECT status FROM jobs WHERE job_id = ? AND attempt = ?",
                (job_id, attempt),
            ).fetchone()
            if current is None:
                raise RunnerError("job_not_found", f"Unknown job {job_id}/{attempt}")
            if current["status"] not in expected:
                raise RunnerError(
                    "invalid_state_transition",
                    f"Cannot move {job_id}/{attempt} from {current['status']} to {new_state}",
                    details={"expected": sorted(expected)},
                )
            fields: dict[str, Any] = {"status": new_state, "updated_at": now, "lease_expires": lease_expires}
            if route is not None:
                fields["route"] = route
            if result is not None:
                fields["result_json"] = json_dumps(result)
                fields["error_json"] = None
                fields["finished_at"] = now
            if error is not None:
                fields["error_json"] = json_dumps(error)
                if new_state in TERMINAL_STATES:
                    fields["finished_at"] = now
            if new_state == "SUPERVISING":
                fields["started_at"] = now
            assignments = ", ".join(f"{name} = ?" for name in fields)
            values = list(fields.values()) + [job_id, attempt]
            self.conn.execute(f"UPDATE jobs SET {assignments} WHERE job_id = ? AND attempt = ?", values)
            self.conn.execute(
                "INSERT INTO job_events (job_id, attempt, state, created_at, note_json) VALUES (?, ?, ?, ?, ?)",
                (job_id, attempt, new_state, now, json_dumps(note) if note else None),
            )
        self._log("job_transition", job_id=job_id, attempt=attempt, state=new_state, note=note or {})
        return self.get_job(job_id, attempt)  # type: ignore[return-value]

    def mark_stale_active_jobs_failed(self) -> list[dict[str, Any]]:
        now = utc_now()
        stale = self.conn.execute(
            """
            SELECT job_id, attempt
            FROM jobs
            WHERE status IN ('SUPERVISING', 'PREPARING', 'RUNNING')
              AND (lease_expires IS NULL OR lease_expires < ?)
            """,
            (now,),
        ).fetchall()
        failed: list[dict[str, Any]] = []
        for row in stale:
            failed.append(
                self.transition(
                    row["job_id"],
                    int(row["attempt"]),
                    {'SUPERVISING', 'PREPARING', 'RUNNING'},
                    "FAILED",
                    error=RunnerError(
                        "stale_inflight_job",
                        "Runner restarted while job was in progress; marking failed to avoid duplicate side effects.",
                    ).as_dict(),
                )
            )
        return failed

    def list_pending(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT job_id, attempt
            FROM jobs
            WHERE status IN ('RECEIVED', 'VALIDATED')
               OR (status = 'VERIFYING' AND (lease_expires IS NULL OR lease_expires < ?))
            ORDER BY created_at ASC
            """
            , (utc_now(),)
        ).fetchall()
        return [self.get_job(row["job_id"], int(row["attempt"])) for row in rows]  # type: ignore[list-item]

    def queue_counts(self) -> dict[str, int]:
        row = self.conn.execute(
            """
            SELECT
              SUM(CASE WHEN status IN ('SUPERVISING', 'PREPARING', 'RUNNING', 'VERIFYING') THEN 1 ELSE 0 END) AS running,
              SUM(CASE WHEN status IN ('RECEIVED', 'VALIDATED') THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN status = 'VERIFYING' AND (lease_expires IS NULL OR lease_expires < ?) THEN 1 ELSE 0 END) AS retryable
            FROM jobs
            """,
            (utc_now(),),
        ).fetchone()
        return {
            "running": int(row["running"] or 0),
            "pending": int(row["pending"] or 0),
            "retryable": int(row["retryable"] or 0),
        }


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "FileLock":
        ensure_parent(self.path)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise RunnerError("runner_busy", "Another runner process currently holds the task lock.")
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


class ProfileStore:
    def __init__(self, profiles_dir: Path) -> None:
        self.profiles_dir = profiles_dir

    def load(self, name: str) -> TestProfile:
        profile_path = self.profiles_dir / f"{name}.toml"
        if not profile_path.exists():
            raise RunnerError("unknown_test_profile", f"Unknown test profile: {name}")
        data = tomllib.loads(profile_path.read_text(encoding="utf-8"))
        command = data.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise RunnerError("invalid_test_profile", f"Profile {name} must define a non-empty command array")
        return TestProfile(
            name=name,
            command=command,
            timeout_seconds=int(data.get("timeout_seconds", 900)),
            description=str(data.get("description", "")),
        )


class WorktreeManager:
    def __init__(self, config: RunnerConfig) -> None:
        self.config = config

    def repo(self, repo_id: str) -> RepoConfig:
        repo = self.config.repos.get(repo_id)
        if repo is None:
            raise RunnerError("unknown_repo", f"Repo {repo_id} is not in the allowlist")
        return repo

    def resolve_relative_path(self, worktree: Path, raw: str) -> Path:
        if not raw or raw.startswith("/") or raw.startswith("~"):
            raise RunnerError("path_escape", f"Path must be relative: {raw}")
        candidate = (worktree / raw).resolve()
        if candidate != worktree.resolve() and worktree.resolve() not in candidate.parents:
            raise RunnerError("path_escape", f"Path escaped worktree: {raw}")
        return candidate

    def allowed_roots(self, worktree: Path, allowed_paths: list[str]) -> list[Path]:
        return [self.resolve_relative_path(worktree, raw) for raw in allowed_paths]

    def assert_exact_commit(self, repo: RepoConfig, sha: str, deadline: Deadline | None = None) -> None:
        env = safe_subprocess_env()
        timeout = deadline.remaining(30) if deadline else 30
        if repo.fetch_remote:
            run_command(
                ["git", "-C", str(repo.path), "fetch", "--quiet", repo.fetch_remote, sha],
                check=True,
                env=env,
                timeout=timeout,
            )
        resolved = run_command(
            ["git", "-C", str(repo.path), "rev-parse", "--verify", f"{sha}^{{commit}}"],
            env=env,
            timeout=timeout,
        ).stdout.strip()
        if resolved != sha:
            raise RunnerError("mutable_target_ref", f"{sha} did not resolve to an exact immutable commit")

    def prepare(self, job: dict[str, Any], deadline: Deadline) -> Path:
        payload = job["payload"]
        repo = self.repo(payload["repo_id"])
        self.assert_exact_commit(repo, payload["base_sha"], deadline)
        self.assert_exact_commit(repo, payload["target_sha"], deadline)
        worktree = (self.config.worktree_root / payload["job_id"] / str(payload["attempt"])).resolve()
        if self.config.worktree_root.resolve() not in worktree.parents:
            raise RunnerError("worktree_escape", "Resolved worktree escaped the configured root")
        if worktree.exists():
            shutil.rmtree(worktree)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        run_command(
            ["git", "-C", str(repo.path), "worktree", "add", "--detach", str(worktree), payload["target_sha"]],
            env=safe_subprocess_env(),
            timeout=deadline.remaining(60),
        )
        if payload["write"]:
            run_command(
                ["git", "-C", str(worktree), "checkout", "-B", f"job/{payload['job_id']}"],
                env=safe_subprocess_env(),
                timeout=deadline.remaining(30),
            )
        return worktree

    def cleanup(self, repo: RepoConfig, worktree: Path) -> None:
        env = safe_subprocess_env()
        run_command(
            ["git", "-C", str(repo.path), "worktree", "remove", "--force", str(worktree)],
            env=env,
            check=False,
            timeout=30,
        )
        run_command(
            ["git", "-C", str(repo.path), "worktree", "prune"],
            env=env,
            check=False,
            timeout=30,
        )
        shutil.rmtree(worktree, ignore_errors=True)

    def status_lines(self, worktree: Path) -> list[str]:
        output = run_command(
            ["git", "-C", str(worktree), "status", "--porcelain=v1", "--untracked-files=all"],
            env=safe_subprocess_env(),
            timeout=30,
        ).stdout.splitlines()
        return output

    def verify_clean(self, worktree: Path) -> None:
        status = self.status_lines(worktree)
        if status:
            raise RunnerError("readonly_dirty_worktree", "Readonly job left tracked or untracked modifications", details={"status": status})

    def write_gate(self, worktree: Path, allowed_paths: list[str]) -> tuple[int, int, str]:
        roots = self.allowed_roots(worktree, allowed_paths)
        env = safe_subprocess_env()
        status = self.status_lines(worktree)
        changed_files: list[Path] = []
        for line in status:
            rel = line[3:]
            changed_files.append(self.resolve_relative_path(worktree, rel))
        for path in changed_files:
            if not any(root == path or root in path.parents for root in roots):
                raise RunnerError("path_escape", f"Modified path escaped allowlist: {path.relative_to(worktree)}")
        if changed_files:
            run_command(["git", "-C", str(worktree), "add", "-N", "--all"], env=env, timeout=30)
        diff = run_command(
            ["git", "-C", str(worktree), "diff", "--binary", "HEAD"],
            env=env,
            timeout=30,
        ).stdout.encode("utf-8")
        return len(changed_files), len(diff), sha256_hex(diff)

    def commit(self, worktree: Path, job_id: str, deadline: Deadline) -> str | None:
        status = self.status_lines(worktree)
        if not status:
            return None
        env = safe_subprocess_env()
        env.update(
            {
                "GIT_AUTHOR_NAME": "mac-runner",
                "GIT_AUTHOR_EMAIL": "mac-runner@local",
                "GIT_COMMITTER_NAME": "mac-runner",
                "GIT_COMMITTER_EMAIL": "mac-runner@local",
            }
        )
        run_command(["git", "-C", str(worktree), "add", "--all"], env=env, timeout=deadline.remaining(30))
        run_command(
            ["git", "-C", str(worktree), "commit", "-m", f"job/{job_id}"],
            env=env,
            timeout=deadline.remaining(30),
        )
        return run_command(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            env=env,
            timeout=deadline.remaining(10),
        ).stdout.strip()


class HostStatusCollector:
    def __init__(self, config: RunnerConfig) -> None:
        self.config = config

    def collect(self) -> dict[str, Any]:
        disk = shutil.disk_usage(self.config.state_dir if self.config.state_dir.exists() else self.config.app_dir)
        return {
            "disk_free_gb": round(disk.free / (1024 ** 3), 2),
            "memory_pressure": self._memory_pressure(),
            "power": self._power_state(),
            "thermal": self._thermal_state(),
        }

    def _memory_pressure(self) -> str:
        try:
            output = run_command(["memory_pressure"], env=safe_subprocess_env(), timeout=5).stdout
        except Exception:
            return "unknown"
        for line in output.splitlines():
            if line.startswith("System-wide memory free percentage:"):
                try:
                    free_percentage = float(line.split(":", 1)[1].strip().removesuffix("%"))
                except ValueError:
                    break
                if free_percentage < 3:
                    return "critical"
                if free_percentage < 8:
                    return "warning"
                return "normal"
        pages_free = 0
        pages_active = 0
        pages_inactive = 0
        for line in output.splitlines():
            if line.startswith("Pages free:"):
                pages_free = int(line.split(":", 1)[1].strip())
            elif line.startswith("Pages active:"):
                pages_active = int(line.split(":", 1)[1].strip())
            elif line.startswith("Pages inactive:"):
                pages_inactive = int(line.split(":", 1)[1].strip())
        total = pages_free + pages_active + pages_inactive
        if total <= 0:
            return "unknown"
        ratio = pages_free / total
        if ratio < 0.03:
            return "critical"
        if ratio < 0.08:
            return "warning"
        return "normal"

    def _power_state(self) -> str:
        try:
            output = run_command(["pmset", "-g", "batt"], env=safe_subprocess_env(), timeout=5).stdout.lower()
        except Exception:
            return "unknown"
        if "ac power" in output or "ac attached" in output:
            return "ac"
        if "battery power" in output:
            return "battery"
        return "unknown"

    def _thermal_state(self) -> str:
        try:
            output = run_command(["pmset", "-g", "therm"], env=safe_subprocess_env(), timeout=5).stdout.lower()
        except Exception:
            return "unknown"
        if "no thermal warning level" in output and "no performance warning level" in output:
            return "nominal"
        if "critical" in output:
            return "critical"
        if "warning" in output:
            return "warning"
        return "unknown"


class OllamaClient:
    def __init__(self, config: RunnerConfig) -> None:
        self.config = config

    def status(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            request = urllib.request.Request(f"{self.config.ollama_endpoint}/api/tags", method="GET")
            with urllib.request.urlopen(request, timeout=timeout or self.config.ollama_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return {"ready": False, "model": self.config.ollama_model}
        model_names = {item["name"] for item in payload.get("models", []) if isinstance(item, dict) and "name" in item}
        requested = self.config.ollama_model
        ready = requested in model_names or f"{requested}:latest" in model_names
        return {"ready": ready, "model": self.config.ollama_model}

    def chat(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None, deadline: Deadline) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.ollama_model,
            "stream": False,
            "think": False,
            "format": "json",
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        request = urllib.request.Request(
            f"{self.config.ollama_endpoint}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=deadline.remaining(self.config.ollama_timeout_seconds)) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RunnerError("ollama_http_error", f"Ollama returned HTTP {exc.code}") from exc
        except TimeoutError as exc:
            raise RunnerError("ollama_timeout", "Ollama request timed out") from exc
        except urllib.error.URLError as exc:
            raise RunnerError("ollama_unreachable", "Ollama request failed") from exc
        message = data.get("message") or {}
        content = message.get("content", "")
        if isinstance(content, str) and len(content) > self.config.ollama_max_output_chars:
            raise RunnerError("ollama_output_too_large", "Ollama output exceeded configured character limit")
        tool_calls = message.get("tool_calls")
        if tools and self.config.ollama_native_tool_calls_required and tool_calls is None:
            if isinstance(content, str) and "<tool_call>" in content:
                raise RunnerError("ollama_native_tool_calls_missing", "Ollama returned raw tool markup instead of structured tool_calls")
        if tool_calls is not None and not isinstance(tool_calls, list):
            raise RunnerError("ollama_invalid_tool_calls", "Ollama tool_calls must be a list")
        return data


class OrnithToolExecutor:
    def __init__(self, worktree: Path, deadline: Deadline, output_limit: int) -> None:
        self.worktree = worktree
        self.deadline = deadline
        self.output_limit = output_limit

    def execute(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        allowed_names = {item["function"]["name"] for item in READONLY_TOOL_DEFS}
        for index, call in enumerate(tool_calls):
            function = call.get("function")
            if not isinstance(function, dict):
                raise RunnerError("ollama_invalid_tool_calls", "Tool call must include function object")
            name = function.get("name")
            if not isinstance(name, str) or name not in allowed_names:
                raise RunnerError("ollama_invalid_tool_calls", f"Unknown tool name: {name}")
            raw_args = function.get("arguments", {})
            args = self._parse_args(raw_args)
            content = self._dispatch(name, args)
            results.append({"role": "tool", "name": name, "content": json_dumps(content), "tool_call_id": str(index)})
        return results

    def _parse_args(self, raw_args: Any) -> dict[str, Any]:
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                raise RunnerError("ollama_invalid_tool_calls", "Tool call arguments must be valid JSON") from exc
        if not isinstance(raw_args, dict):
            raise RunnerError("ollama_invalid_tool_calls", "Tool call arguments must be an object")
        return raw_args

    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "repo_status":
            if args:
                raise RunnerError("ollama_invalid_tool_calls", "repo_status takes no arguments")
            output = run_command(
                ["git", "-C", str(self.worktree), "status", "--short", "--branch"],
                env=safe_subprocess_env(),
                timeout=self.deadline.remaining(10),
            ).stdout
            return {"status": trim_text(output, self.output_limit)}
        if name == "list_files":
            path = self._relative_path(args.get("path", "."))
            limit = self._bounded_int(args.get("limit", 200), "limit", 1, 500)
            files: list[str] = []
            for entry in sorted(path.rglob("*")):
                if entry.is_file():
                    files.append(str(entry.relative_to(self.worktree)))
                    if len(files) >= limit:
                        break
            return {"files": files}
        if name == "read_file":
            path = self._relative_path(args.get("path"))
            max_chars = self._bounded_int(args.get("max_chars", 4000), "max_chars", 1, 12000)
            with path.open("r", encoding="utf-8") as handle:
                content = handle.read(max_chars + 1)
            truncated = len(content) > max_chars
            if truncated:
                content = content[:max_chars]
            return {
                "path": str(path.relative_to(self.worktree)),
                "content": content,
                "truncated": truncated,
            }
        if name == "rg":
            pattern = args.get("pattern")
            if not isinstance(pattern, str) or not pattern or len(pattern) > 300:
                raise RunnerError("ollama_invalid_tool_calls", "rg pattern must be a non-empty string <= 300 chars")
            path = self._relative_path(args.get("path", "."))
            max_matches = self._bounded_int(args.get("max_matches", 50), "max_matches", 1, 200)
            output = run_command(
                ["rg", "--line-number", "--color", "never", "--max-count", str(max_matches), pattern, str(path)],
                env=safe_subprocess_env(),
                timeout=self.deadline.remaining(10),
                check=False,
            ).stdout
            return {"matches": trim_text(output, self.output_limit)}
        raise RunnerError("ollama_invalid_tool_calls", f"Unsupported tool {name}")

    def _relative_path(self, raw: Any) -> Path:
        if not isinstance(raw, str):
            raise RunnerError("ollama_invalid_tool_calls", "Tool path must be a string")
        if raw == ".":
            return self.worktree
        if not raw or raw.startswith("/") or raw.startswith("~"):
            raise RunnerError("path_escape", f"Tool path must be relative: {raw}")
        candidate = (self.worktree / raw).resolve()
        if candidate != self.worktree.resolve() and self.worktree.resolve() not in candidate.parents:
            raise RunnerError("path_escape", f"Tool path escaped worktree: {raw}")
        return candidate

    def _bounded_int(self, value: Any, field: str, low: int, high: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise RunnerError("ollama_invalid_tool_calls", f"{field} must be an integer")
        if value < low or value > high:
            raise RunnerError("ollama_invalid_tool_calls", f"{field} must be between {low} and {high}")
        return value


class CodexSupervisor:
    def __init__(self, config: RunnerConfig) -> None:
        self.config = config
        self.decision_validator = SimpleSchemaValidator(DECISION_SCHEMA)
        self.acceptance_validator = SimpleSchemaValidator(ACCEPTANCE_SCHEMA)
        self.worker_validator = SimpleSchemaValidator(WORKER_SCHEMA)

    def build_exec_command(
        self,
        *,
        cwd: Path,
        sandbox: str,
        schema_path: Path,
        skip_git_repo_check: bool = False,
    ) -> list[str]:
        command = [
            self.config.supervisor_codex_path,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--cd",
            str(cwd),
            "--sandbox",
            sandbox,
            "--output-schema",
            str(schema_path),
            "--color",
            "never",
            "-",
        ]
        if skip_git_repo_check:
            command.insert(5, "--skip-git-repo-check")
        if self.config.supervisor_model:
            command.extend(["--model", self.config.supervisor_model])
        return command

    def build_env(self) -> dict[str, str]:
        return safe_subprocess_env(allow_credentials=True)

    def decide(self, job: dict[str, Any], status: dict[str, Any], deadline: Deadline) -> dict[str, Any]:
        prompt = textwrap.dedent(
            f"""
            TASK: decide_route
            Decide the execution route for this job.
            Policy:
            - Write jobs should use route "codex".
            - Readonly jobs may use "ornith" when the work is constrained and local health is normal.
            - Return JSON only.

            Job:
            {json_dumps(job['payload'])}

            Runner status:
            {json_dumps(status)}
            """
        ).strip()
        result = self._invoke_json(
            prompt=prompt,
            schema=DECISION_SCHEMA,
            cwd=self.config.app_dir,
            sandbox="read-only",
            deadline=deadline,
            skip_git_repo_check=True,
        )
        self.decision_validator.validate(result)
        return result

    def execute_job(
        self,
        job: dict[str, Any],
        workspace: Path,
        deadline: Deadline,
        *,
        skip_git_repo_check: bool = False,
    ) -> dict[str, Any]:
        payload = job["payload"]
        sandbox = "workspace-write" if payload["write"] else "read-only"
        prompt = textwrap.dedent(
            f"""
            TASK: execute_job
            Return JSON only with a top-level findings array.
            Stay inside the provided git worktree.
            {'You may edit files only under these relative paths: ' + json_dumps(payload['allowed_paths']) if payload['write'] else 'Do not modify any files.'}
            Never push, never merge, never use network credentials.

            Job:
            {json_dumps(payload)}
            """
        ).strip()
        result = self._invoke_json(
            prompt=prompt,
            schema=WORKER_SCHEMA,
            cwd=workspace,
            sandbox=sandbox,
            deadline=deadline,
            skip_git_repo_check=skip_git_repo_check,
        )
        self.worker_validator.validate(result)
        return result

    def accept(self, job: dict[str, Any], worker_result: dict[str, Any], tests: dict[str, Any], deadline: Deadline) -> dict[str, Any]:
        prompt = textwrap.dedent(
            f"""
            TASK: accept_result
            Decide whether the execution result should be accepted.
            Reject when tests failed or the result contains execution errors.
            Return JSON only.

            Job:
            {json_dumps(job['payload'])}

            Worker result:
            {json_dumps(worker_result)}

            Tests:
            {json_dumps(tests)}
            """
        ).strip()
        result = self._invoke_json(
            prompt=prompt,
            schema=ACCEPTANCE_SCHEMA,
            cwd=self.config.app_dir,
            sandbox="read-only",
            deadline=deadline,
            skip_git_repo_check=True,
        )
        self.acceptance_validator.validate(result)
        return result

    def _invoke_json(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        cwd: Path,
        sandbox: str,
        deadline: Deadline,
        skip_git_repo_check: bool = False,
    ) -> dict[str, Any]:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as schema_file:
            schema_file.write(json.dumps(schema))
            schema_path = Path(schema_file.name)
        try:
            proc = run_command(
                self.build_exec_command(
                    cwd=cwd,
                    sandbox=sandbox,
                    schema_path=schema_path,
                    skip_git_repo_check=skip_git_repo_check,
                ),
                env=self.build_env(),
                cwd=cwd,
                timeout=deadline.remaining(120),
                stdin=prompt,
            )
        finally:
            schema_path.unlink(missing_ok=True)
        return self._extract_json(proc.stdout, schema)

    def _extract_json(self, stdout: str, schema: dict[str, Any]) -> dict[str, Any]:
        validator = SimpleSchemaValidator(schema)
        lines = [item for item in stdout.splitlines() if item.strip()]
        for line in reversed(lines):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            for candidate in self._iter_candidates(payload):
                materialized = candidate
                if isinstance(candidate, str):
                    try:
                        materialized = json.loads(candidate)
                    except json.JSONDecodeError:
                        continue
                if not isinstance(materialized, dict):
                    continue
                try:
                    validator.validate(materialized)
                    return materialized
                except RunnerError:
                    continue
        raise RunnerError("codex_output_invalid", "Could not find structured Codex output matching the required schema", details={"stdout": trim_text(stdout, 8000)})

    def _iter_candidates(self, node: Any) -> Iterator[Any]:
        yield node
        if isinstance(node, dict):
            for value in node.values():
                yield from self._iter_candidates(value)
        elif isinstance(node, list):
            for item in node:
                yield from self._iter_candidates(item)


class TestSandbox:
    XCODE_DERIVED_DATA_TOKEN = "{xcode_derived_data}"

    def __init__(self, state_dir: Path, worktree: Path, needs_xcode_derived_data: bool = False) -> None:
        self.state_dir = state_dir
        self.worktree = worktree.resolve()
        self.needs_xcode_derived_data = needs_xcode_derived_data
        self.base_dir = state_dir / "test-sandboxes"
        self.home_dir: Path | None = None
        self.profile_path: Path | None = None
        self.xcode_derived_data: Path | None = None

    def __enter__(self) -> "TestSandbox":
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.home_dir = Path(tempfile.mkdtemp(prefix="job-home-", dir=self.base_dir))
        self.profile_path = self.base_dir / f"{self.home_dir.name}.sb"
        xcode_rules = ""
        if self.needs_xcode_derived_data:
            xcode_root = Path.home() / "Library" / "Developer" / "Xcode" / "DerivedData"
            xcode_root.mkdir(parents=True, exist_ok=True)
            self.xcode_derived_data = Path(tempfile.mkdtemp(prefix="MacRunner-", dir=xcode_root))
        read_roots = [
            Path("/System"),
            Path("/usr"),
            Path("/bin"),
            Path("/sbin"),
            Path("/opt/homebrew"),
            Path("/usr/local"),
            Path("/Library"),
            Path("/Applications/Xcode.app"),
            Path("/private/var/db/timezone"),
            Path("/private/tmp"),
            Path("/dev"),
            self.worktree,
            self.home_dir,
        ]
        if self.xcode_derived_data is not None:
            darwin_temp = Path(os.environ.get("TMPDIR", tempfile.gettempdir())).resolve()
            developer_tools_cache = darwin_temp.parent / "C" / "com.apple.DeveloperTools"
            read_roots.extend(
                [
                    self.xcode_derived_data,
                    darwin_temp,
                    developer_tools_cache,
                    darwin_temp.parent / "C" / "clang",
                    Path.home() / "Library" / "Developer" / "CoreSimulator",
                    Path.home() / "Library" / "Developer" / "DVTDownloads" / "Assets" / "MetalToolchain",
                    Path.home() / "Library" / "Developer" / "Xcode" / "SDKToSimulatorIndexMapping.plist",
                    Path.home() / "Library" / "Developer" / "Xcode" / "XcodeToMetalToolchainIndexMapping.plist",
                    Path.home() / "Library" / "Preferences" / "com.apple.LaunchServices",
                ]
            )
            xcode_rules = textwrap.dedent(
                """
                (allow user-preference-read
                    (preference-domain "kCFPreferencesAnyApplication")
                    (preference-domain "com.apple.dt.xcode")
                    (preference-domain "com.apple.dt.xcodebuild")
                    (preference-domain "com.apple.coresimulator")
                    (preference-domain "com.apple.ibtool")
                    (preference-domain "com.apple.universalaccess"))
                (allow user-preference-write
                    (preference-domain "com.apple.dt.xcodebuild")
                    (preference-domain "com.apple.coresimulator")
                    (preference-domain "com.apple.ibtool"))
                (allow job-creation)
                (allow signal (target children))
                (allow signal (target same-sandbox))
                (allow system-fsctl
                    (fsctl-command (_IO "h" 47))
                    (fsctl-command (_IO "J" 2)))
                (allow ipc-posix-shm-read* ipc-posix-shm-write*
                    (ipc-posix-name-prefix "ibsete"))
                (allow mach-lookup
                    (global-name "com.apple.CoreServices.coreservicesd")
                    (global-name "com.apple.CoreSimulator.simdiskimaged")
                    (global-name "com.apple.DiskArbitration.diskarbitrationd")
                    (global-name "com.apple.FSEvents")
                    (global-name "com.apple.FileCoordination")
                    (global-name "com.apple.SystemConfiguration.configd")
                    (global-name "com.apple.coreservices.quarantine-resolver")
                    (global-name "com.apple.distributed_notifications@Uv3")
                    (global-name "com.apple.lsd.mapdb")
                    (global-name "com.apple.lsd.modifydb")
                    (global-name "com.apple.mobileassetd.v2"))
                """
            ).strip()
        read_rules = " ".join(f"(subpath {json.dumps(str(path))})" for path in read_roots)
        write_roots = [self.worktree, self.home_dir, Path("/private/tmp")]
        if self.xcode_derived_data is not None:
            write_roots.extend(
                [
                    self.xcode_derived_data,
                    darwin_temp,
                    developer_tools_cache,
                    darwin_temp.parent / "C" / "clang",
                ]
            )
        write_rules = " ".join(
            f"(subpath {json.dumps(str(path))})"
            for path in write_roots
        )
        git_marker = json.dumps(str(self.worktree / ".git"))
        profile = textwrap.dedent(
            f"""
            (version 1)
            (deny default)
            (import "system.sb")
            (allow process*)
            (allow file-read-metadata)
            (allow file-read* {read_rules})
            (allow file-write* {write_rules})
            (deny file-write* (literal {git_marker}))
            (deny network*)
            {xcode_rules}
            """
        ).strip()
        self.profile_path.write_text(profile, encoding="utf-8")
        return self

    def wrap(self, command: list[str]) -> list[str]:
        if self.profile_path is None:
            raise RuntimeError("Sandbox not initialized")
        return ["/usr/bin/sandbox-exec", "-f", str(self.profile_path), *command]

    def expand(self, command: list[str]) -> list[str]:
        if self.XCODE_DERIVED_DATA_TOKEN not in command:
            return list(command)
        if self.xcode_derived_data is None:
            raise RuntimeError("Xcode DerivedData directory not initialized")
        return [
            str(self.xcode_derived_data) if item == self.XCODE_DERIVED_DATA_TOKEN else item
            for item in command
        ]

    def env(self) -> dict[str, str]:
        if self.home_dir is None:
            raise RuntimeError("Sandbox not initialized")
        env = safe_subprocess_env(allow_credentials=False, home=str(self.home_dir))
        temp_dir = self.home_dir / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        env["CFFIXED_USER_HOME"] = str(self.home_dir)
        env["TMPDIR"] = f"{temp_dir}/"
        env["PYTHONPYCACHEPREFIX"] = str(self.home_dir / ".cache" / "python")
        return env

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.profile_path is not None:
            self.profile_path.unlink(missing_ok=True)
        if self.home_dir is not None:
            shutil.rmtree(self.home_dir, ignore_errors=True)
        if self.xcode_derived_data is not None:
            shutil.rmtree(self.xcode_derived_data, ignore_errors=True)


class CodexWriteWorkspace:
    def __init__(self, state_dir: Path, worktree: Path, allowed_paths: list[str]) -> None:
        self.state_dir = state_dir
        self.worktree = worktree.resolve()
        self.allowed_paths = allowed_paths
        self.base_dir = state_dir / "codex-workspaces"
        self.workspace: Path | None = None
        self._baseline: dict[str, str] = {}

    def __enter__(self) -> "CodexWriteWorkspace":
        # A copied symlink can still point outside the disposable workspace. Reject
        # symlink-bearing write jobs so Codex cannot use one as an allowed-path or
        # read/write escape channel.
        self._assert_no_symlinks(self.worktree)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        workspace_root = Path(tempfile.mkdtemp(prefix="job-codex-", dir=self.base_dir))
        shutil.rmtree(workspace_root)
        shutil.copytree(self.worktree, workspace_root, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        self.workspace = workspace_root
        self._baseline = self._snapshot_tree(workspace_root)
        self._lock_down(workspace_root)
        return self

    @property
    def path(self) -> Path:
        if self.workspace is None:
            raise RuntimeError("workspace not initialized")
        return self.workspace

    def assert_changes_scoped(self) -> None:
        current = self._snapshot_tree(self.path)
        changed = sorted(path for path in set(self._baseline) | set(current) if self._baseline.get(path) != current.get(path))
        for rel in changed:
            if not self._is_allowed(rel):
                raise RunnerError("path_escape", f"Codex modified a path outside allowed_paths: {rel}")

    def sync_back(self) -> None:
        for raw in self.allowed_paths:
            source = self._resolve_relative(self.path, raw)
            target = self._resolve_relative(self.worktree, raw)
            self._assert_no_symlinks(source)
            self._assert_no_symlinks(target)
            if source.is_dir():
                self._sync_directory(source, target)
            elif source.exists():
                ensure_parent(target)
                shutil.copy2(source, target)
            elif target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()

    def _lock_down(self, workspace_root: Path) -> None:
        workspace_resolved = workspace_root.resolve()
        for root, dirnames, filenames in os.walk(workspace_root):
            root_path = Path(root)
            os.chmod(root_path, 0o555)
            for dirname in dirnames:
                candidate = root_path / dirname
                if not candidate.is_symlink():
                    os.chmod(candidate, 0o555)
            for filename in filenames:
                candidate = root_path / filename
                if not candidate.is_symlink():
                    os.chmod(candidate, 0o444)
        for raw in self.allowed_paths:
            target = self._resolve_relative(workspace_root, raw)
            ancestor = target.parent if target != workspace_root else workspace_root
            while True:
                os.chmod(ancestor, 0o755)
                if ancestor.resolve() == workspace_resolved:
                    break
                ancestor = ancestor.parent
            if target.exists():
                self._chmod_tree(target)

    def _chmod_tree(self, root: Path) -> None:
        if root.is_symlink():
            return
        if root.is_dir():
            for current_root, dirnames, filenames in os.walk(root):
                current_root_path = Path(current_root)
                os.chmod(current_root_path, 0o755)
                for dirname in dirnames:
                    candidate = current_root_path / dirname
                    if not candidate.is_symlink():
                        os.chmod(candidate, 0o755)
                for filename in filenames:
                    candidate = current_root_path / filename
                    if not candidate.is_symlink():
                        os.chmod(candidate, 0o644)
            return
        os.chmod(root, 0o644)

    def _snapshot_tree(self, root: Path) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for current_root, dirnames, filenames in os.walk(root):
            current_root_path = Path(current_root)
            rel_root = current_root_path.relative_to(root)
            if rel_root != Path("."):
                snapshot[str(rel_root)] = "dir"
            dirnames.sort()
            filenames.sort()
            for filename in filenames:
                candidate = current_root_path / filename
                rel = str(candidate.relative_to(root))
                if candidate.is_symlink():
                    snapshot[rel] = f"symlink:{os.readlink(candidate)}"
                else:
                    snapshot[rel] = f"file:{sha256_hex(candidate.read_bytes())}"
        return snapshot

    def _is_allowed(self, rel: str) -> bool:
        candidate = Path(rel)
        for raw in self.allowed_paths:
            allowed = Path(raw)
            if candidate == allowed or allowed in candidate.parents:
                return True
        return False

    def _resolve_relative(self, root: Path, raw: str) -> Path:
        if not raw or raw.startswith("/") or raw.startswith("~"):
            raise RunnerError("path_escape", f"Path must be relative: {raw}")
        root_resolved = root.resolve()
        candidate = (root / raw).resolve()
        if candidate != root_resolved and root_resolved not in candidate.parents:
            raise RunnerError("path_escape", f"Path escaped workspace: {raw}")
        return candidate

    def _assert_no_symlinks(self, root: Path) -> None:
        if root.is_symlink():
            raise RunnerError("path_escape", f"Symlinked path is not supported for Codex write jobs: {root}")
        if not root.exists() or not root.is_dir():
            return
        for current_root, dirnames, filenames in os.walk(root):
            current_root_path = Path(current_root)
            for dirname in dirnames:
                if (current_root_path / dirname).is_symlink():
                    raise RunnerError(
                        "path_escape",
                        f"Symlinked path is not supported for Codex write jobs: {current_root_path / dirname}",
                    )
            for filename in filenames:
                if (current_root_path / filename).is_symlink():
                    raise RunnerError(
                        "path_escape",
                        f"Symlinked path is not supported for Codex write jobs: {current_root_path / filename}",
                    )

    def _sync_directory(self, source: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        source_entries = {entry.name: entry for entry in source.iterdir()}
        target_entries = {entry.name: entry for entry in target.iterdir()} if target.exists() else {}
        for name in sorted(set(target_entries) - set(source_entries)):
            candidate = target_entries[name]
            if candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
        for name, source_entry in source_entries.items():
            target_entry = target / name
            if source_entry.is_dir():
                self._sync_directory(source_entry, target_entry)
            else:
                ensure_parent(target_entry)
                shutil.copy2(source_entry, target_entry)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.workspace is not None:
            shutil.rmtree(self.workspace, ignore_errors=True)


class Runner:
    def __init__(self, config: RunnerConfig) -> None:
        self.config = config
        self.ledger = Ledger(config.db_path, config.log_path)
        self.job_validator = SimpleSchemaValidator(load_json(config.job_schema_path))
        self.result_validator = SimpleSchemaValidator(load_json(config.result_schema_path))
        self.profiles = ProfileStore(config.profiles_dir)
        self.worktrees = WorktreeManager(config)
        self.host = HostStatusCollector(config)
        self.ollama = OllamaClient(config)
        self.supervisor = CodexSupervisor(config)
        self.lock_path = config.state_dir / "runner.lock"
        self.shutdown_requested = False
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        self.shutdown_requested = True
        self.ledger._log("signal_received", signal=signum)

    def close(self) -> None:
        self.ledger.close()

    def _artifact_dir(self, job: dict[str, Any]) -> Path:
        artifact_dir = (self.config.artifacts_root / job["payload"]["job_id"] / str(job["payload"]["attempt"])).resolve()
        if self.config.artifacts_root.resolve() not in artifact_dir.parents:
            raise RunnerError("artifact_path_escape", "Artifact directory escaped configured root")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        return artifact_dir

    def _write_artifact(self, job: dict[str, Any], name: str, payload: dict[str, Any]) -> str:
        artifact_path = self._artifact_path(job, name)
        with tempfile.NamedTemporaryFile("w", dir=artifact_path.parent, delete=False, encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            temp_artifact = Path(handle.name)
        os.chmod(temp_artifact, 0o600)
        os.replace(temp_artifact, artifact_path)
        return str(artifact_path)

    def _artifact_path(self, job: dict[str, Any], name: str) -> Path:
        if not name or any(character not in "abcdefghijklmnopqrstuvwxyz-" for character in name):
            raise RunnerError("artifact_path_escape", f"Invalid artifact name: {name}")
        return self._artifact_dir(job) / f"{name}.json"

    def _read_artifact(self, job: dict[str, Any], name: str) -> dict[str, Any]:
        path = self._artifact_path(job, name)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunnerError(
                "verification_artifact_invalid",
                f"Cannot resume VERIFYING from artifact {name}",
            ) from exc
        if not isinstance(payload, dict):
            raise RunnerError("verification_artifact_invalid", f"Artifact {name} must contain a JSON object")
        return payload

    def _has_verification_artifacts(self, job: dict[str, Any]) -> bool:
        return all(
            self._artifact_path(job, name).is_file()
            for name in ("route-decision", "worker-result", "tests", "result")
        )

    def _assert_not_cancelled(self, job_id: str, attempt: int) -> None:
        current = self.ledger.get_job(job_id, attempt)
        if current and current["status"] == "CANCELLED":
            raise RunnerError("job_cancelled", f"Job {job_id}/{attempt} was cancelled")

    def validate_payload(self, payload: dict[str, Any]) -> None:
        self.job_validator.validate(payload)
        if payload["repo_id"] not in self.config.repos:
            raise RunnerError("unknown_repo", f"Repo {payload['repo_id']} is not in the allowlist")
        self.profiles.load(payload["test_profile"])
        repo = self.worktrees.repo(payload["repo_id"])
        if repo.test_profiles and payload["test_profile"] not in repo.test_profiles:
            raise RunnerError(
                "test_profile_not_allowed",
                f"Test profile {payload['test_profile']} is not allowed for repo {payload['repo_id']}",
                details={"allowed_profiles": list(repo.test_profiles)},
            )
        if payload["write"] and not self.config.supervisor_allow_write_tasks:
            raise RunnerError("write_tasks_disabled", "Write tasks are disabled by config")
        if payload["write"] and not payload["allowed_paths"]:
            raise RunnerError("write_requires_allowed_paths", "Write jobs must declare allowed_paths")
        unavailable_capabilities = sorted(
            capability
            for capability in payload["required_capabilities"]
            if not self.config.capabilities.get(capability, False)
        )
        if unavailable_capabilities:
            raise RunnerError(
                "capability_unavailable",
                "Job requires capabilities that are not enabled for this Runner",
                details={"capabilities": unavailable_capabilities},
            )
        dummy_root = self.config.worktree_root / "validation"
        self.worktrees.allowed_roots(dummy_root, payload["allowed_paths"])
        self.worktrees.assert_exact_commit(repo, payload["base_sha"])
        self.worktrees.assert_exact_commit(repo, payload["target_sha"])

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            self.job_validator.validate(payload)
        except RunnerError as exc:
            return {"status": "REJECTED", "payload": payload, "error": exc.as_dict()}
        try:
            row = self.ledger.insert_job(payload)
        except RunnerError as exc:
            return {"status": "REJECTED", "payload": payload, "error": exc.as_dict()}
        if row["status"] != "RECEIVED":
            return row
        try:
            self.validate_payload(payload)
            return self.ledger.transition(payload["job_id"], payload["attempt"], {"RECEIVED"}, "VALIDATED")
        except RunnerError as exc:
            return self.ledger.transition(
                payload["job_id"],
                payload["attempt"],
                {"RECEIVED"},
                "REJECTED",
                error=exc.as_dict(),
            )

    def get(self, job_id: str, attempt: int) -> dict[str, Any]:
        job = self.ledger.get_job(job_id, attempt)
        if not job:
            raise RunnerError("job_not_found", f"Unknown job {job_id}/{attempt}")
        return job

    def cancel(self, job_id: str, attempt: int) -> dict[str, Any]:
        job = self.get(job_id, attempt)
        if job["status"] in TERMINAL_STATES:
            return job
        return self.ledger.transition(job_id, attempt, set(STATE_SEQUENCE) - TERMINAL_STATES, "CANCELLED")

    def status(self) -> dict[str, Any]:
        return {
            "capabilities": self.config.capabilities,
            "host": self.host.collect(),
            "ollama": self.ollama.status(timeout=5),
            "queue": self.ledger.queue_counts(),
            "git": {"worktrees": len(list(self.config.worktree_root.glob("*/*"))), "dirty_outside_jobs": False},
        }

    def serve(self, *, poll_seconds: int, heartbeat_seconds: int, busy_summary_seconds: int) -> None:
        last_status_hash: str | None = None
        last_heartbeat = 0.0
        last_busy_summary = 0.0
        while not self.shutdown_requested:
            now = utc_now()
            status = self.status()
            status_hash = sha256_hex(json_dumps(status).encode("utf-8"))
            if status_hash != last_status_hash:
                self.ledger._log("status_changed", status=status)
                last_status_hash = status_hash
            running = status["queue"]["running"]
            pending = status["queue"]["pending"]
            retryable = status["queue"]["retryable"]
            if retryable > 0 and self.config.repos:
                self.ledger._log("serve_retry_verifying", retryable=retryable)
                self.execute(None, None)
                continue
            if pending > 0 and running == 0 and self.config.repos:
                self.ledger._log("serve_execute_pending", pending=pending)
                self.execute(None, None)
                continue
            if pending > 0 and not self.config.repos:
                self.ledger._log("serve_monitor_only", reason="no_repos_configured")
            if now - last_heartbeat >= heartbeat_seconds:
                self.ledger._log("heartbeat", status=status)
                last_heartbeat = now
            if (running > 0 or pending > 0) and now - last_busy_summary >= busy_summary_seconds:
                self.ledger._log("busy_summary", status=status)
                last_busy_summary = now
            sleep_until = utc_now() + poll_seconds
            while not self.shutdown_requested and utc_now() < sleep_until:
                time.sleep(min(1.0, max(0.1, sleep_until - utc_now())))

    def execute(self, job_id: str | None, attempt: int | None) -> dict[str, Any]:
        with FileLock(self.lock_path):
            self.ledger.mark_stale_active_jobs_failed()
            job = self._select_job(job_id, attempt)
            if job["status"] in TERMINAL_STATES:
                return job
            deadline = Deadline(job["payload"]["deadline_seconds"])
            lease = utc_now() + min(self.config.active_lease_seconds, job["payload"]["deadline_seconds"])
            repo = self.worktrees.repo(job["payload"]["repo_id"])
            worktree: Path | None = None
            route: dict[str, Any] | None = None
            worker_result: dict[str, Any] | None = None
            tests: dict[str, Any] | None = None
            artifacts: dict[str, str] = {}
            try:
                if job["status"] == "VERIFYING":
                    return self._resume_verifying(job, deadline)
                job = self.ledger.transition(job["job_id"], job["attempt"], {"VALIDATED"}, "SUPERVISING", lease_expires=lease)
                route = self.supervisor.decide(job, self.status(), deadline)
                artifacts["route_decision"] = self._write_artifact(job, "route-decision", route)
                self._assert_not_cancelled(job["job_id"], job["attempt"])
                job = self.ledger.transition(
                    job["job_id"],
                    job["attempt"],
                    {"SUPERVISING"},
                    "PREPARING",
                    route=route["route"],
                    note=route,
                    lease_expires=lease,
                )
                worktree = self.worktrees.prepare(job, deadline)
                self._assert_not_cancelled(job["job_id"], job["attempt"])
                job = self.ledger.transition(job["job_id"], job["attempt"], {"PREPARING"}, "RUNNING", lease_expires=lease)
                worker_result = self._run_worker(job, worktree, deadline)
                artifacts["worker_result"] = self._write_artifact(job, "worker-result", worker_result)
                if job["payload"]["write"]:
                    self._pretest_write_gate(job, worktree)
                self._assert_not_cancelled(job["job_id"], job["attempt"])
                job = self.ledger.transition(job["job_id"], job["attempt"], {"RUNNING"}, "VERIFYING", lease_expires=lease)
                tests = self._run_tests(job, worktree, deadline)
                artifacts["tests"] = self._write_artifact(job, "tests", tests)
                result = self._finalize(job, worktree, worker_result, tests, deadline, route=route, artifacts=artifacts)
                artifacts["acceptance"] = str(self._artifact_path(job, "acceptance"))
                artifacts["result"] = str(self._artifact_path(job, "result"))
                result["artifacts"] = dict(sorted(artifacts.items()))
                self.result_validator.validate(result)
                self._write_artifact(job, "result", result)
                acceptance = self.supervisor.accept(job, worker_result, tests, deadline)
                artifacts["acceptance"] = self._write_artifact(job, "acceptance", acceptance)
                result["supervisor"]["acceptance"] = acceptance
                result["artifacts"] = dict(sorted(artifacts.items()))
                self.result_validator.validate(result)
                self._write_artifact(job, "result", result)
                if not acceptance["accepted"]:
                    return self.ledger.transition(
                        job["job_id"],
                        job["attempt"],
                        {"VERIFYING"},
                        "FAILED",
                        error=RunnerError("supervisor_rejected", "Supervisor rejected execution result", details=acceptance).as_dict(),
                        result=result,
                    )
                return self.ledger.transition(job["job_id"], job["attempt"], {"VERIFYING"}, "DONE", result=result)
            except RunnerError as exc:
                current = self.ledger.get_job(job["job_id"], job["attempt"]) if job else None
                if current and current["status"] in TERMINAL_STATES:
                    return current
                if (
                    current
                    and current["status"] == "VERIFYING"
                    and exc.code != "verification_artifact_invalid"
                    and self._has_verification_artifacts(current)
                ):
                    retry_delay = min(30, self.config.active_lease_seconds)
                    return self.ledger.transition(
                        current["job_id"],
                        current["attempt"],
                        {"VERIFYING"},
                        "VERIFYING",
                        error=exc.as_dict(),
                        note={"retry": "supervisor_acceptance", "error_code": exc.code},
                        lease_expires=utc_now() + retry_delay,
                    )
                if current and current["status"] not in TERMINAL_STATES:
                    return self.ledger.transition(
                        current["job_id"],
                        current["attempt"],
                        set(STATE_SEQUENCE) - TERMINAL_STATES,
                        "FAILED" if current["status"] != "RECEIVED" else "REJECTED",
                        error=exc.as_dict(),
                    )
                raise
            finally:
                if worktree is not None:
                    self.worktrees.cleanup(repo, worktree)

    def _resume_verifying(self, job: dict[str, Any], deadline: Deadline) -> dict[str, Any]:
        route = self._read_artifact(job, "route-decision")
        worker_result = self._read_artifact(job, "worker-result")
        tests = self._read_artifact(job, "tests")
        result = self._read_artifact(job, "result")
        try:
            self.supervisor.decision_validator.validate(route)
            self.supervisor.worker_validator.validate({"findings": worker_result.get("findings")})
            if worker_result.get("route") not in {"ornith", "codex"} or not isinstance(worker_result.get("errors"), list):
                raise RunnerError("schema_validation_failed", "Worker artifact wrapper is invalid")
            if not isinstance(tests.get("exit_code"), int):
                raise RunnerError("schema_validation_failed", "Tests artifact has no integer exit_code")
            self.result_validator.validate(result)
        except RunnerError as exc:
            raise RunnerError("verification_artifact_invalid", "Saved VERIFYING artifacts failed validation") from exc
        acceptance = self.supervisor.accept(job, worker_result, tests, deadline)
        result["supervisor"]["acceptance"] = acceptance
        result["artifacts"]["acceptance"] = self._write_artifact(job, "acceptance", acceptance)
        self.result_validator.validate(result)
        self._write_artifact(job, "result", result)
        if not acceptance["accepted"]:
            return self.ledger.transition(
                job["job_id"],
                job["attempt"],
                {"VERIFYING"},
                "FAILED",
                error=RunnerError("supervisor_rejected", "Supervisor rejected execution result", details=acceptance).as_dict(),
                result=result,
            )
        return self.ledger.transition(job["job_id"], job["attempt"], {"VERIFYING"}, "DONE", result=result)

    def _select_job(self, job_id: str | None, attempt: int | None) -> dict[str, Any]:
        if job_id is not None and attempt is not None:
            return self.get(job_id, attempt)
        pending = self.ledger.list_pending()
        if not pending:
            raise RunnerError("no_pending_jobs", "No VALIDATED jobs are pending")
        return pending[0]

    def _run_worker(self, job: dict[str, Any], worktree: Path, deadline: Deadline) -> dict[str, Any]:
        if job["route"] == "codex":
            with contextlib.ExitStack() as stack:
                workspace = worktree
                skip_git_repo_check = False
                codex_workspace: CodexWriteWorkspace | None = None
                if job["payload"]["write"]:
                    codex_workspace = stack.enter_context(
                        CodexWriteWorkspace(self.config.state_dir, worktree, job["payload"]["allowed_paths"])
                    )
                    workspace = codex_workspace.path
                    skip_git_repo_check = True
                result = self.supervisor.execute_job(
                    job,
                    workspace,
                    deadline,
                    skip_git_repo_check=skip_git_repo_check,
                )
                if codex_workspace is not None:
                    codex_workspace.assert_changes_scoped()
                    codex_workspace.sync_back()
            return {"route": "codex", "findings": result["findings"], "errors": []}
        payload = job["payload"]
        prompt = textwrap.dedent(
            f"""
            You are a readonly code reviewer.
            Return JSON only with a top-level findings array.
            Use the provided tools when needed.
            Task type: {payload['task_type']}
            Focus: {', '.join(payload['focus'])}
            """
        ).strip()
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        tools = OrnithToolExecutor(worktree, deadline, self.config.ollama_max_output_chars)
        for _ in range(6):
            response = self.ollama.chat(messages, tools=READONLY_TOOL_DEFS, deadline=deadline)
            message = response.get("message") or {}
            tool_calls = message.get("tool_calls")
            content = message.get("content", "")
            if tool_calls:
                if not isinstance(tool_calls, list):
                    raise RunnerError("ollama_invalid_tool_calls", "tool_calls must be a list")
                messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
                messages.extend(tools.execute(tool_calls))
                continue
            if not isinstance(content, str) or not content.strip():
                raise RunnerError("ollama_invalid_json", "Ollama returned neither findings JSON nor executable tool_calls")
            try:
                parsed = parse_json_object_text(content)
            except RunnerError as exc:
                raise RunnerError("ollama_invalid_json", "Ollama final response must be valid findings JSON") from exc
            SimpleSchemaValidator(WORKER_SCHEMA).validate(parsed)
            return {"route": "ornith", "findings": parsed["findings"], "errors": []}
        messages.append(
            {
                "role": "user",
                "content": (
                    "The readonly tool budget is exhausted. Return the final JSON object now with exactly "
                    "one top-level findings array. Do not call tools."
                ),
            }
        )
        response = self.ollama.chat(messages, tools=None, deadline=deadline)
        message = response.get("message") or {}
        if message.get("tool_calls"):
            raise RunnerError(
                "ollama_tool_round_limit",
                "Ollama attempted another tool call after the readonly tool budget was exhausted",
            )
        content = message.get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise RunnerError(
                "ollama_tool_round_limit",
                "Ollama exhausted the readonly tool budget without returning final findings JSON",
            )
        try:
            parsed = parse_json_object_text(content)
            SimpleSchemaValidator(WORKER_SCHEMA).validate(parsed)
        except RunnerError as exc:
            raise RunnerError(
                "ollama_tool_round_limit",
                "Ollama exhausted the readonly tool budget and returned invalid final findings JSON",
            ) from exc
        return {"route": "ornith", "findings": parsed["findings"], "errors": []}

    def _pretest_write_gate(self, job: dict[str, Any], worktree: Path) -> None:
        changed_files, diff_bytes, _ = self.worktrees.write_gate(worktree, job["payload"]["allowed_paths"])
        if changed_files > self.config.max_changed_files:
            raise RunnerError("diff_too_large", "Changed file count exceeded configured limit")
        if diff_bytes > self.config.max_diff_bytes:
            raise RunnerError("diff_too_large", "Diff bytes exceeded configured limit")

    def _run_tests(self, job: dict[str, Any], worktree: Path, deadline: Deadline) -> dict[str, Any]:
        profile = self.profiles.load(job["payload"]["test_profile"])
        needs_xcode_derived_data = TestSandbox.XCODE_DERIVED_DATA_TOKEN in profile.command
        with TestSandbox(self.config.state_dir, worktree, needs_xcode_derived_data) as sandbox:
            env = sandbox.env()
            timeout = min(profile.timeout_seconds, int(deadline.remaining(profile.timeout_seconds)))
            proc = run_command(
                sandbox.wrap(sandbox.expand(profile.command)),
                cwd=worktree,
                env=env,
                timeout=timeout,
                check=False,
            )
            for key in SECRET_ENV_KEYS:
                if key in env:
                    raise RunnerError("secret_env_leak", f"Secret env leaked into test subprocess: {key}")
            if proc.returncode != 0:
                raise RunnerError(
                    "test_profile_failed",
                    f"Test profile {profile.name} exited with code {proc.returncode}",
                    details={"stdout": trim_text(proc.stdout, 4000), "stderr": trim_text(proc.stderr, 4000)},
                )
        return {"profile": profile.name, "exit_code": 0}

    def _finalize(
        self,
        job: dict[str, Any],
        worktree: Path,
        worker_result: dict[str, Any],
        tests: dict[str, Any],
        deadline: Deadline,
        *,
        route: dict[str, Any] | None,
        artifacts: dict[str, str],
    ) -> dict[str, Any]:
        payload = job["payload"]
        commit_sha: str | None = None
        if payload["write"]:
            changed_files, diff_bytes, diff_hash = self.worktrees.write_gate(worktree, payload["allowed_paths"])
            if changed_files > self.config.max_changed_files:
                raise RunnerError("diff_too_large", "Changed file count exceeded configured limit")
            if diff_bytes > self.config.max_diff_bytes:
                raise RunnerError("diff_too_large", "Diff bytes exceeded configured limit")
            commit_sha = self.worktrees.commit(worktree, payload["job_id"], deadline)
        else:
            self.worktrees.verify_clean(worktree)
            diff_hash = sha256_hex(b"")
        result = {
            "job_id": payload["job_id"],
            "attempt": payload["attempt"],
            "status": "DONE",
            "route": job["route"],
            "findings": worker_result["findings"],
            "test_exit_code": tests.get("exit_code"),
            "diff_hash": diff_hash,
            "commit_sha": commit_sha,
            "duration_seconds": round(deadline.elapsed(), 3),
            "errors": list(worker_result.get("errors", [])),
            "supervisor": {
                "decision": route or {"route": job["route"], "reason": "decision artifact missing"},
                "acceptance": {"accepted": False, "summary": "pending", "errors": []},
            },
            "artifacts": dict(sorted(artifacts.items())),
        }
        return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.toml"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit = subparsers.add_parser("submit")
    submit.add_argument("--file", type=Path)
    get = subparsers.add_parser("get")
    get.add_argument("--job-id", required=True)
    get.add_argument("--attempt", required=True, type=int)
    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("--job-id", required=True)
    cancel.add_argument("--attempt", required=True, type=int)
    execute = subparsers.add_parser("execute")
    execute.add_argument("--job-id")
    execute.add_argument("--attempt", type=int)
    subparsers.add_parser("status")
    serve = subparsers.add_parser("serve")
    serve.add_argument("--poll-seconds", type=int, default=15)
    serve.add_argument("--heartbeat-seconds", type=int, default=300)
    serve.add_argument("--busy-summary-seconds", type=int, default=60)
    return parser.parse_args(argv)


def read_payload(path: Path | None) -> dict[str, Any]:
    if path is None:
        raw = sys.stdin.read()
    else:
        raw = path.read_text(encoding="utf-8")
    return json.loads(raw)


def emit(payload: Any, *, exit_code: int = 0) -> int:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    config = RunnerConfig.load(args.config)
    runner = Runner(config)
    try:
        if args.command == "submit":
            return emit(runner.submit(read_payload(args.file)))
        if args.command == "get":
            return emit(runner.get(args.job_id, args.attempt))
        if args.command == "cancel":
            return emit(runner.cancel(args.job_id, args.attempt))
        if args.command == "status":
            return emit(runner.status())
        if args.command == "execute":
            return emit(runner.execute(args.job_id, args.attempt))
        if args.command == "serve":
            runner.serve(
                poll_seconds=args.poll_seconds,
                heartbeat_seconds=args.heartbeat_seconds,
                busy_summary_seconds=args.busy_summary_seconds,
            )
            return 0
        raise AssertionError("unreachable")
    except RunnerError as exc:
        return emit({"error": exc.as_dict()}, exit_code=1)
    except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError) as exc:
        return emit(
            {"error": {"code": "invalid_input", "message": f"Invalid runner input: {type(exc).__name__}"}},
            exit_code=1,
        )
    finally:
        runner.close()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterator

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 compatibility
    import tomli as tomllib


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
GIT_BIN = "/usr/bin/git"
SYNC_TEST_PROFILE = "git-sync-verify"
JOB_SCHEMA_VERSION = "mac-job/v1"
POLICY_V2 = 2
WIRE_PAYLOAD_MAX_BYTES = 48 * 1024
PERMISSION_PROFILES = {"observe", "standard-worktree", "operational", "privileged"}
SCOPE_ROOTS = {"metadata-only", "worktree", "registered-checkout"}
NETWORK_MODES = {"none", "relay-only", "declared-remotes-and-registries"}
OPERATIONAL_CAPABILITIES = {
    "prepare-registered-repo",
    "sync-registered-repo",
    "push-task-branch",
    "manage-pr",
    "install-user-tool",
    "restart-user-service",
}
DEFAULT_ALLOWED_SERVICE_LABELS: tuple[str, ...] = ()
DEFAULT_PROTECTED_BRANCH_PREFIXES = ("main", "master", "release/", "prod/", "production/")
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


def canonical_json_bytes(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


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


def parse_iso8601(value: str) -> dt.datetime:
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RunnerError("invalid_owner_approval", "owner_approval.approved_at must be ISO-8601 with timezone") from exc
    if parsed.tzinfo is None:
        raise RunnerError("invalid_owner_approval", "owner_approval.approved_at must include timezone")
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
    sync_enabled: bool
    canonical_remote_url: str | None
    sync_remote: str | None
    sync_branch: str | None
    prepare_enabled: bool = False
    prepare_backup_root: Path | None = None
    prepare_expected_status_sha256: str | None = None
    prepare_expected_untracked_count: int | None = None
    prepare_allowed_remote_urls: tuple[str, ...] = ()
    tool_registry: tuple[str, ...] = ()
    sensitive_paths: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class CapabilityConfig:
    enabled: bool
    fixed_name: str | None = None
    fixed_source: str | None = None
    fixed_version: str | None = None
    service_labels: tuple[str, ...] = ()
    remote: str | None = None
    allowed_branch_prefixes: tuple[str, ...] = ()
    protected_branch_prefixes: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class TestProfile:
    name: str
    command: list[str]
    timeout_seconds: int
    description: str


@dataclasses.dataclass(frozen=True)
class VerificationRun:
    profile: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""


@dataclasses.dataclass
class RunnerConfig:
    config_path: Path
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
    service_label: str | None
    owner_pubkey: str | None
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
    capability_config: dict[str, CapabilityConfig]

    @classmethod
    def load(cls, config_path: Path) -> "RunnerConfig":
        config_path = config_path.expanduser().resolve()
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        app_dir = config_path.parent
        runner_cfg = data["runner"]
        ollama_cfg = data["ollama"]
        supervisor_cfg = data["supervisor"]
        raw_capabilities = data.get("capabilities", {})
        raw_capability_config = data.get("capability_bindings", {})
        if not isinstance(raw_capabilities, dict) or not all(
            isinstance(name, str) and name and isinstance(enabled, bool)
            for name, enabled in raw_capabilities.items()
        ):
            raise RunnerError("invalid_config", "capabilities must be a TOML table of boolean flags")
        if raw_capability_config and not isinstance(raw_capability_config, dict):
            raise RunnerError("invalid_config", "capability_bindings must be a TOML table")
        repos: dict[str, RepoConfig] = {}
        for repo_id, entry in data.get("repos", {}).items():
            repo_path = Path(entry["path"]).expanduser().resolve()
            sync_enabled = entry.get("sync_enabled", False)
            if not isinstance(sync_enabled, bool):
                raise RunnerError("invalid_config", f"repos.{repo_id}.sync_enabled must be boolean")
            canonical_remote_url = entry.get("canonical_remote_url")
            sync_remote = entry.get("sync_remote")
            sync_branch = entry.get("sync_branch")
            if sync_enabled:
                required_sync_values = {
                    "canonical_remote_url": canonical_remote_url,
                    "sync_remote": sync_remote,
                    "sync_branch": sync_branch,
                }
                missing = sorted(name for name, value in required_sync_values.items() if not isinstance(value, str) or not value)
                if missing:
                    raise RunnerError(
                        "invalid_config",
                        f"repos.{repo_id} is sync-enabled but missing: {', '.join(missing)}",
                    )
                for label, value in (("sync_remote", sync_remote), ("sync_branch", sync_branch)):
                    assert isinstance(value, str)
                    if value.startswith("-") or ".." in value or not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
                        raise RunnerError("invalid_config", f"repos.{repo_id}.{label} is not a safe fixed Git name")
            prepare_enabled = entry.get("prepare_enabled", False)
            if not isinstance(prepare_enabled, bool):
                raise RunnerError("invalid_config", f"repos.{repo_id}.prepare_enabled must be boolean")
            prepare_backup_root: Path | None = None
            prepare_expected_status_sha256: str | None = None
            prepare_expected_untracked_count: int | None = None
            prepare_allowed_remote_urls: tuple[str, ...] = ()
            if prepare_enabled:
                if not sync_enabled:
                    raise RunnerError("invalid_config", f"repos.{repo_id} prepare requires sync_enabled=true")
                raw_backup_root = entry.get("prepare_backup_root")
                if not isinstance(raw_backup_root, str) or not raw_backup_root.strip():
                    raise RunnerError("invalid_config", f"repos.{repo_id}.prepare_backup_root is required")
                prepare_backup_root = Path(raw_backup_root).expanduser().resolve()
                if prepare_backup_root == repo_path or repo_path in prepare_backup_root.parents:
                    raise RunnerError("invalid_config", f"repos.{repo_id}.prepare_backup_root must be outside the checkout")
                raw_status_sha = entry.get("prepare_expected_status_sha256")
                if not isinstance(raw_status_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", raw_status_sha):
                    raise RunnerError(
                        "invalid_config",
                        f"repos.{repo_id}.prepare_expected_status_sha256 must be lowercase SHA-256",
                    )
                prepare_expected_status_sha256 = raw_status_sha
                raw_count = entry.get("prepare_expected_untracked_count")
                if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 1:
                    raise RunnerError(
                        "invalid_config",
                        f"repos.{repo_id}.prepare_expected_untracked_count must be a positive integer",
                    )
                prepare_expected_untracked_count = raw_count
                raw_urls = entry.get("prepare_allowed_remote_urls")
                if not isinstance(raw_urls, list) or not raw_urls or not all(
                    isinstance(item, str) and item for item in raw_urls
                ):
                    raise RunnerError(
                        "invalid_config",
                        f"repos.{repo_id}.prepare_allowed_remote_urls must be a non-empty string array",
                    )
                prepare_allowed_remote_urls = tuple(raw_urls)
                if canonical_remote_url not in prepare_allowed_remote_urls:
                    raise RunnerError(
                        "invalid_config",
                        f"repos.{repo_id}.prepare_allowed_remote_urls must include canonical_remote_url",
                    )
            repos[repo_id] = RepoConfig(
                repo_id=repo_id,
                path=repo_path,
                fetch_remote=entry.get("fetch_remote"),
                test_profiles=tuple(entry.get("test_profiles", [])),
                sync_enabled=sync_enabled,
                canonical_remote_url=canonical_remote_url if isinstance(canonical_remote_url, str) else None,
                sync_remote=sync_remote if isinstance(sync_remote, str) else None,
                sync_branch=sync_branch if isinstance(sync_branch, str) else None,
                prepare_enabled=prepare_enabled,
                prepare_backup_root=prepare_backup_root,
                prepare_expected_status_sha256=prepare_expected_status_sha256,
                prepare_expected_untracked_count=prepare_expected_untracked_count,
                prepare_allowed_remote_urls=prepare_allowed_remote_urls,
                tool_registry=tuple(str(item) for item in entry.get("tool_registry", [])),
                sensitive_paths=tuple(cls._validate_sensitive_patterns(repo_id, entry.get("sensitive_paths", []))),
            )
        capability_config: dict[str, CapabilityConfig] = {}
        for name in OPERATIONAL_CAPABILITIES:
            binding = raw_capability_config.get(name, {}) if isinstance(raw_capability_config, dict) else {}
            if binding and not isinstance(binding, dict):
                raise RunnerError("invalid_config", f"capability_bindings.{name} must be a TOML table")
            if name == "restart-user-service":
                raw_service_labels = binding.get("service_labels", DEFAULT_ALLOWED_SERVICE_LABELS)
                if not isinstance(raw_service_labels, (list, tuple)) or not all(
                    isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9.-]{1,255}", item)
                    for item in raw_service_labels
                ):
                    raise RunnerError(
                        "invalid_config",
                        "capability_bindings.restart-user-service.service_labels must contain safe LaunchAgent labels",
                    )
                service_labels = tuple(raw_service_labels)
                capability_config[name] = CapabilityConfig(
                    enabled=bool(raw_capabilities.get(name, False)),
                    service_labels=service_labels,
                )
            elif name == "install-user-tool":
                capability_config[name] = CapabilityConfig(
                    enabled=bool(raw_capabilities.get(name, False)),
                    fixed_name=str(binding.get("fixed_name", "")).strip() or None,
                    fixed_source=str(binding.get("fixed_source", "")).strip() or None,
                    fixed_version=str(binding.get("fixed_version", "")).strip() or None,
                )
            elif name in {"push-task-branch", "manage-pr"}:
                prefixes = tuple(str(item) for item in binding.get("allowed_branch_prefixes", ["job/"]))
                protected = tuple(str(item) for item in binding.get("protected_branch_prefixes", DEFAULT_PROTECTED_BRANCH_PREFIXES))
                capability_config[name] = CapabilityConfig(
                    enabled=bool(raw_capabilities.get(name, False)),
                    remote=str(binding.get("remote", "")).strip() or None,
                    allowed_branch_prefixes=prefixes,
                    protected_branch_prefixes=protected,
                )
            else:
                capability_config[name] = CapabilityConfig(enabled=bool(raw_capabilities.get(name, False)))
        raw_model = str(supervisor_cfg.get("model", "")).strip()
        service_label = str(runner_cfg.get("service_label", "")).strip() or None
        if service_label is not None and not re.fullmatch(r"[A-Za-z0-9.-]{1,255}", service_label):
            raise RunnerError("invalid_config", "runner.service_label must be a safe LaunchAgent label")
        owner_pubkey = str(runner_cfg.get("owner_pubkey", "")).strip().lower() or None
        if owner_pubkey is not None:
            if owner_pubkey == "0" * 64 or not re.fullmatch(r"[0-9a-f]{64}", owner_pubkey):
                raise RunnerError("invalid_config", "runner.owner_pubkey must be a real 64-hex owner pubkey")
        return cls(
            config_path=config_path,
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
            service_label=service_label,
            owner_pubkey=owner_pubkey,
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
            capability_config=capability_config,
        )

    @staticmethod
    def _validate_sensitive_patterns(repo_id: str, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise RunnerError("invalid_config", f"repos.{repo_id}.sensitive_paths must be an array")
        patterns: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise RunnerError("invalid_config", f"repos.{repo_id}.sensitive_paths must contain non-empty strings")
            pattern = item.strip()
            if pattern.startswith("/") or pattern.startswith("~") or ".." in pattern.split("/"):
                raise RunnerError("invalid_config", f"repos.{repo_id}.sensitive_paths may not use absolute or parent-relative patterns")
            patterns.append(pattern)
        return patterns


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
              wire_payload_json TEXT,
              wire_payload_hash TEXT,
              policy_version INTEGER,
              permission_profile TEXT,
              capabilities_json TEXT,
              scope_json TEXT,
              network_mode TEXT,
              verification_profiles_json TEXT,
              owner_approval_json TEXT,
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
        self._ensure_column("jobs", "wire_payload_json", "TEXT")
        self._ensure_column("jobs", "wire_payload_hash", "TEXT")
        self._ensure_column("jobs", "policy_version", "INTEGER")
        self._ensure_column("jobs", "permission_profile", "TEXT")
        self._ensure_column("jobs", "capabilities_json", "TEXT")
        self._ensure_column("jobs", "scope_json", "TEXT")
        self._ensure_column("jobs", "network_mode", "TEXT")
        self._ensure_column("jobs", "verification_profiles_json", "TEXT")
        self._ensure_column("jobs", "owner_approval_json", "TEXT")
        self._backfill_legacy_jobs()
        self.conn.commit()

    def _ensure_column(self, table: str, name: str, declared_type: str) -> None:
        columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declared_type}")

    def _backfill_legacy_jobs(self) -> None:
        rows = self.conn.execute(
            """
            SELECT job_id, attempt, payload_json, wire_payload_json, policy_version
            FROM jobs
            WHERE wire_payload_json IS NULL OR wire_payload_hash IS NULL OR policy_version IS NULL
            """
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            wire_payload = payload
            if not isinstance(payload, dict):
                raise RunnerError("ledger_error", f"Job {row['job_id']}/{row['attempt']} payload_json is not an object")
            canonical, wire_meta = normalize_job_payload(wire_payload)
            self.conn.execute(
                """
                UPDATE jobs
                SET payload_json = ?,
                    wire_payload_json = ?,
                    wire_payload_hash = ?,
                    policy_version = ?,
                    permission_profile = ?,
                    capabilities_json = ?,
                    scope_json = ?,
                    network_mode = ?,
                    verification_profiles_json = ?,
                    owner_approval_json = ?,
                    write_enabled = ?,
                    repo_id = ?,
                    deadline_seconds = ?
                WHERE job_id = ? AND attempt = ?
                """,
                (
                    json_dumps(canonical),
                    json_dumps(wire_meta["wire_payload"]),
                    wire_meta["wire_payload_hash"],
                    canonical.get("policy_version"),
                    canonical.get("permission_profile"),
                    json_dumps(canonical.get("capabilities", [])),
                    json_dumps(canonical.get("scope", {})),
                    canonical.get("network", {}).get("mode") if isinstance(canonical.get("network"), dict) else None,
                    json_dumps(canonical.get("verification_profiles", [])),
                    json_dumps(canonical.get("owner_approval")) if canonical.get("owner_approval") is not None else None,
                    1 if canonical.get("write") else 0,
                    canonical["repo_id"],
                    canonical["deadline_seconds"],
                    row["job_id"],
                    int(row["attempt"]),
                ),
            )

    def close(self) -> None:
        self.conn.close()

    def _log(self, event: str, **fields: Any) -> None:
        record = {"ts": utc_now(), "event": event, **fields}
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json_dumps(record) + "\n")

    def insert_job(self, payload: dict[str, Any], *, wire_payload: dict[str, Any], wire_payload_hash: str) -> dict[str, Any]:
        now = utc_now()
        canonical_payload = json_dumps(payload)
        wire_payload_json = json_dumps(wire_payload)
        changed_before = self.conn.total_changes
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO jobs (
                  job_id, attempt, payload_json, wire_payload_json, wire_payload_hash,
                  policy_version, permission_profile, capabilities_json, scope_json, network_mode,
                  verification_profiles_json, owner_approval_json, status, repo_id, write_enabled,
                  deadline_seconds, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["job_id"],
                    payload["attempt"],
                    canonical_payload,
                    wire_payload_json,
                    wire_payload_hash,
                    payload.get("policy_version"),
                    payload.get("permission_profile"),
                    json_dumps(payload.get("capabilities", [])),
                    json_dumps(payload.get("scope", {})),
                    payload.get("network", {}).get("mode") if isinstance(payload.get("network"), dict) else None,
                    json_dumps(payload.get("verification_profiles", [])),
                    json_dumps(payload.get("owner_approval")) if payload.get("owner_approval") is not None else None,
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
                "SELECT payload_json, wire_payload_json FROM jobs WHERE job_id = ? AND attempt = ?",
                (payload["job_id"], payload["attempt"]),
            ).fetchone()
            if row is None:
                raise RunnerError("ledger_error", "Failed to fetch inserted job")
            if row["payload_json"] != canonical_payload:
                raise RunnerError(
                    "idempotency_conflict",
                    f"Job {payload['job_id']}/{payload['attempt']} already exists with different payload",
                )
            if row["wire_payload_json"] and row["wire_payload_json"] != wire_payload_json:
                raise RunnerError(
                    "idempotency_conflict",
                    f"Job {payload['job_id']}/{payload['attempt']} already exists with different wire payload",
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
        result["wire_payload"] = json.loads(result["wire_payload_json"]) if result.get("wire_payload_json") else result["payload"]
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
    def __init__(
        self,
        path: Path,
        *,
        busy_code: str = "runner_busy",
        busy_message: str = "Another runner process currently holds the task lock.",
    ) -> None:
        self.path = path
        self.busy_code = busy_code
        self.busy_message = busy_message
        self.handle: Any = None

    def __enter__(self) -> "FileLock":
        ensure_parent(self.path)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise RunnerError(self.busy_code, self.busy_message)
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


def _string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise RunnerError("schema_validation_failed", f"{field} must be an array of non-empty strings")
    return list(value)


def normalize_job_payload(raw_payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    wire_payload = json.loads(json.dumps(raw_payload))
    wire_bytes = canonical_json_bytes(wire_payload)
    if len(wire_bytes) > WIRE_PAYLOAD_MAX_BYTES:
        raise RunnerError("payload_too_large", f"Wire payload exceeds {WIRE_PAYLOAD_MAX_BYTES} bytes")

    common: dict[str, Any] = {
        "schema": wire_payload.get("schema"),
        "job_id": wire_payload.get("job_id"),
        "attempt": wire_payload.get("attempt"),
        "repo_id": wire_payload.get("repo_id"),
        "base_sha": wire_payload.get("base_sha"),
        "target_sha": wire_payload.get("target_sha"),
        "task_type": wire_payload.get("task_type"),
        "focus": list(wire_payload.get("focus", [])),
        "deadline_seconds": wire_payload.get("deadline_seconds"),
        "supervisor": wire_payload.get("supervisor"),
        "execution_route": wire_payload.get("execution_route"),
        "preferred_worker": wire_payload.get("preferred_worker"),
        "required_capabilities": list(wire_payload.get("required_capabilities", [])),
        "summary": wire_payload.get("summary"),
        "instructions": wire_payload.get("instructions"),
        "acceptance_criteria": list(wire_payload.get("acceptance_criteria", [])) if "acceptance_criteria" in wire_payload else [],
        "metadata": dict(wire_payload.get("metadata", {})) if isinstance(wire_payload.get("metadata"), dict) else {},
        "context": dict(wire_payload.get("context", {})) if isinstance(wire_payload.get("context"), dict) else {},
        "extensions": dict(wire_payload.get("extensions", {})) if isinstance(wire_payload.get("extensions"), dict) else {},
    }
    if common["schema"] != JOB_SCHEMA_VERSION:
        raise RunnerError("schema_validation_failed", f"$.schema must be {JOB_SCHEMA_VERSION}")

    has_policy_v2 = wire_payload.get("policy_version") == POLICY_V2 or "permission_profile" in wire_payload
    if has_policy_v2:
        permission_profile = wire_payload.get("permission_profile")
        if permission_profile not in PERMISSION_PROFILES:
            raise RunnerError("schema_validation_failed", "$.permission_profile must be a known permission profile")
        scope = wire_payload.get("scope")
        network = wire_payload.get("network")
        if not isinstance(scope, dict) or scope.get("root") not in SCOPE_ROOTS:
            raise RunnerError("schema_validation_failed", "$.scope.root must be a known scope root")
        if not isinstance(network, dict) or network.get("mode") not in NETWORK_MODES:
            raise RunnerError("schema_validation_failed", "$.network.mode must be a known network mode")
        capabilities = _string_list(wire_payload.get("capabilities", []), field="$.capabilities")
        verification_profiles = _string_list(wire_payload.get("verification_profiles", []), field="$.verification_profiles")
        owner_approval = wire_payload.get("owner_approval")
        if permission_profile == "privileged":
            if not isinstance(owner_approval, dict):
                raise RunnerError("missing_owner_approval", "privileged jobs require owner_approval")
            approved_by = owner_approval.get("approved_by")
            approval_ref = owner_approval.get("approval_ref")
            approved_at = owner_approval.get("approved_at")
            summary = owner_approval.get("summary")
            if not isinstance(approved_by, str) or not re.fullmatch(r"[0-9a-f]{64}", approved_by):
                raise RunnerError("invalid_owner_approval", "owner_approval.approved_by must be a 64-hex pubkey")
            if not isinstance(approval_ref, str) or not approval_ref:
                raise RunnerError("invalid_owner_approval", "owner_approval.approval_ref must be a non-empty string")
            if not isinstance(summary, str) or not summary:
                raise RunnerError("invalid_owner_approval", "owner_approval.summary must be a non-empty string")
            parse_iso8601(str(approved_at))
        canonical = {
            **common,
            "policy_version": POLICY_V2,
            "permission_profile": permission_profile,
            "capabilities": capabilities,
            "scope": {
                "root": scope["root"],
                "paths": _string_list(scope.get("paths", []), field="$.scope.paths") if "paths" in scope else [],
            },
            "network": {"mode": network["mode"]},
            "verification_profiles": verification_profiles,
            "owner_approval": owner_approval if isinstance(owner_approval, dict) else None,
            "write": permission_profile == "standard-worktree",
            "allowed_paths": list(scope.get("paths", [])) if isinstance(scope, dict) else [],
            "test_profile": verification_profiles[0] if verification_profiles else None,
        }
        return canonical, {"wire_payload": wire_payload, "wire_payload_hash": sha256_hex(wire_bytes)}

    write_enabled = wire_payload.get("write")
    if not isinstance(write_enabled, bool):
        raise RunnerError("schema_validation_failed", "$.write is required for legacy policy")
    allowed_paths = _string_list(wire_payload.get("allowed_paths", []), field="$.allowed_paths")
    test_profile = wire_payload.get("test_profile")
    if not isinstance(test_profile, str) or not test_profile:
        raise RunnerError("schema_validation_failed", "$.test_profile is required for legacy policy")
    canonical = {
        **common,
        "policy_version": 1,
        "permission_profile": "standard-worktree" if write_enabled else "observe",
        "capabilities": [],
        "scope": {"root": "worktree", "paths": allowed_paths},
        "network": {"mode": "none"},
        "verification_profiles": [test_profile],
        "owner_approval": None,
        "write": write_enabled,
        "allowed_paths": allowed_paths,
        "test_profile": test_profile,
    }
    return canonical, {"wire_payload": wire_payload, "wire_payload_hash": sha256_hex(wire_bytes)}


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
        if not allowed_paths:
            return [worktree.resolve()]
        return [self.resolve_relative_path(worktree, raw) for raw in allowed_paths]

    def is_sensitive_path(self, repo: RepoConfig, rel_path: str) -> bool:
        normalized = rel_path.strip("/")
        if not normalized:
            return False
        return any(fnmatch(normalized, pattern) for pattern in repo.sensitive_paths)

    def assert_no_sensitive_paths(
        self,
        repo: RepoConfig,
        sha: str,
        deadline: Deadline | None = None,
        *,
        network_mode: str = "none",
    ) -> None:
        if not repo.sensitive_paths:
            return
        self.assert_exact_commit(repo, sha, deadline, network_mode=network_mode)
        env = safe_subprocess_env()
        timeout = deadline.remaining(30) if deadline else 30
        output = run_command(
            ["git", "-C", str(repo.path), "ls-tree", "-r", "--name-only", sha],
            env=env,
            timeout=timeout,
        ).stdout
        matches = [path for path in output.splitlines() if self.is_sensitive_path(repo, path)]
        if matches:
            raise RunnerError(
                "sensitive_path_present",
                f"Sensitive tracked paths are present in target commit {sha}",
                details={"paths": matches[:32]},
            )

    def assert_exact_commit(
        self,
        repo: RepoConfig,
        sha: str,
        deadline: Deadline | None = None,
        *,
        network_mode: str = "none",
    ) -> None:
        env = safe_subprocess_env()
        timeout = deadline.remaining(30) if deadline else 30
        if network_mode == "declared-remotes-and-registries" and repo.fetch_remote:
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
        network_mode = payload.get("network", {}).get("mode", "none")
        self.assert_exact_commit(repo, payload["base_sha"], deadline, network_mode=network_mode)
        self.assert_exact_commit(repo, payload["target_sha"], deadline, network_mode=network_mode)
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

    def write_gate(self, repo: RepoConfig, worktree: Path, allowed_paths: list[str]) -> tuple[int, int, str]:
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
            if self.is_sensitive_path(repo, str(path.relative_to(worktree))):
                raise RunnerError("sensitive_path_present", f"Modified path matches sensitive allowlist: {path.relative_to(worktree)}")
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


class GitSyncManager:
    def __init__(self, config: RunnerConfig) -> None:
        self.config = config

    def lock_path(self, repo: RepoConfig) -> Path:
        return self.config.state_dir / "repo-locks" / f"{repo.repo_id}.sync.lock"

    def lock(self, repo: RepoConfig) -> FileLock:
        return FileLock(
            self.lock_path(repo),
            busy_code="repo_sync_busy",
            busy_message=f"Repository {repo.repo_id} is already being synchronized.",
        )

    def assert_configured(self, repo: RepoConfig) -> None:
        if not repo.sync_enabled:
            raise RunnerError("sync_not_allowed", f"Repository {repo.repo_id} is not enabled for synchronization")
        if not repo.canonical_remote_url or not repo.sync_remote or not repo.sync_branch:
            raise RunnerError("sync_config_incomplete", f"Repository {repo.repo_id} has incomplete synchronization config")

    def assert_prepare_configured(self, repo: RepoConfig) -> None:
        self.assert_configured(repo)
        if not repo.prepare_enabled:
            raise RunnerError("prepare_not_allowed", f"Repository {repo.repo_id} is not enabled for preparation")
        if (
            repo.prepare_backup_root is None
            or repo.prepare_expected_status_sha256 is None
            or repo.prepare_expected_untracked_count is None
            or not repo.prepare_allowed_remote_urls
        ):
            raise RunnerError("prepare_config_incomplete", f"Repository {repo.repo_id} has incomplete preparation config")

    def prepare_registered_checkout(
        self,
        repo: RepoConfig,
        payload: dict[str, Any],
        deadline: Deadline,
    ) -> dict[str, Any]:
        self.assert_prepare_configured(repo)
        assert repo.prepare_backup_root is not None
        assert repo.prepare_expected_status_sha256 is not None
        assert repo.prepare_expected_untracked_count is not None
        assert repo.canonical_remote_url is not None
        assert repo.sync_remote is not None
        assert repo.sync_branch is not None

        with self.lock(repo):
            inside = self._git(repo, ["rev-parse", "--is-inside-work-tree"], deadline, check=False)
            if inside.returncode != 0 or inside.stdout.strip() != "true":
                raise RunnerError("not_git_checkout", f"Configured path for {repo.repo_id} is not a Git worktree")
            top = Path(self._git(repo, ["rev-parse", "--show-toplevel"], deadline).stdout.strip()).resolve()
            if top != repo.path:
                raise RunnerError("wrong_checkout", f"Configured path for {repo.repo_id} is not the worktree top level")
            branch = self._git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], deadline, check=False)
            if branch.returncode != 0 or branch.stdout.strip() != repo.sync_branch:
                raise RunnerError(
                    "wrong_branch",
                    f"Repository {repo.repo_id} is not on its configured preparation branch",
                    details={"expected": repo.sync_branch},
                )
            head = self._rev_parse(repo, "HEAD", deadline)
            if head != payload["base_sha"] or payload["target_sha"] != payload["base_sha"]:
                raise RunnerError(
                    "base_mismatch",
                    f"Repository {repo.repo_id} preparation requires target_sha=base_sha=current HEAD",
                    details={"expected": payload["base_sha"], "actual": head},
                )
            remote_urls = self._git(
                repo,
                ["remote", "get-url", "--all", repo.sync_remote],
                deadline,
                check=False,
            )
            urls = [line for line in remote_urls.stdout.splitlines() if line]
            if remote_urls.returncode != 0 or len(urls) != 1 or urls[0] not in repo.prepare_allowed_remote_urls:
                raise RunnerError(
                    "wrong_remote",
                    f"Configured remote for {repo.repo_id} is outside the preparation allowlist",
                    details={"allowed": list(repo.prepare_allowed_remote_urls)},
                )
            original_remote = urls[0]

            status = self._git(repo, ["status", "--porcelain=v1", "--untracked-files=all"], deadline)
            status_text = status.stdout
            status_hash = sha256_hex(status_text.encode("utf-8"))
            status_lines = status_text.splitlines()
            if status_hash != repo.prepare_expected_status_sha256:
                raise RunnerError(
                    "prep_status_mismatch",
                    "Repository status does not match the configured immutable preparation snapshot",
                    details={"expected": repo.prepare_expected_status_sha256, "actual": status_hash},
                )
            if len(status_lines) != repo.prepare_expected_untracked_count:
                raise RunnerError(
                    "prep_status_mismatch",
                    "Repository untracked count does not match the configured preparation snapshot",
                    details={"expected": repo.prepare_expected_untracked_count, "actual": len(status_lines)},
                )
            if any(not line.startswith("?? ") for line in status_lines):
                raise RunnerError("prep_tracked_changes", "Preparation refuses staged or tracked changes")
            ignored = self._git(
                repo,
                ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
                deadline,
            )
            ignored_paths = [path for path in ignored.stdout.split("\0") if path]
            if ignored_paths:
                raise RunnerError(
                    "prep_ignored_files",
                    "Preparation refuses ignored files that are absent from the immutable backup snapshot",
                    details={"entry_count": len(ignored_paths)},
                )

            status_z = self._git(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], deadline)
            records = [record for record in status_z.stdout.split("\0") if record]
            if len(records) != repo.prepare_expected_untracked_count or any(
                not record.startswith("?? ") for record in records
            ):
                raise RunnerError("prep_status_mismatch", "NUL-delimited status does not match the preparation snapshot")

            sources: list[tuple[str, Path]] = []
            manifest_entries: list[dict[str, Any]] = []
            for record in records:
                relative = record[3:]
                if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
                    raise RunnerError("path_escape", f"Preparation path is not repository-relative: {relative}")
                unresolved = repo.path / relative
                if unresolved.is_symlink():
                    raise RunnerError("prep_special_file", f"Preparation refuses symlink: {relative}")
                source = unresolved.resolve()
                if repo.path not in source.parents or not source.is_file():
                    raise RunnerError("prep_special_file", f"Preparation accepts only contained regular files: {relative}")
                digest = hashlib.sha256()
                try:
                    with source.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    source_size = source.stat().st_size
                except OSError as exc:
                    raise RunnerError("prep_file_unreadable", f"Preparation cannot read: {relative}") from exc
                sources.append((relative, source))
                manifest_entries.append(
                    {"path": relative, "size": source_size, "sha256": digest.hexdigest()}
                )
            manifest_entries.sort(key=lambda item: item["path"])
            sources.sort(key=lambda item: item[0])
            manifest_data = canonical_json_bytes({"schema": "mac-runner/prep-manifest-v1", "files": manifest_entries})
            manifest_hash = sha256_hex(manifest_data)
            total_bytes = sum(int(item["size"]) for item in manifest_entries)

            backup_root = repo.prepare_backup_root.resolve()
            backup_dir = (backup_root / f"{payload['job_id']}-attempt-{payload['attempt']}").resolve()
            if backup_dir.parent != backup_root or backup_dir.exists():
                raise RunnerError("prep_backup_exists", "Preparation backup directory already exists or escaped its root")

            moved: list[tuple[Path, Path]] = []
            remote_changed = False
            try:
                backup_dir.mkdir(parents=True, exist_ok=False)
                (backup_dir / "manifest.json").write_bytes(manifest_data + b"\n")
                for relative, source in sources:
                    destination = (backup_dir / relative).resolve()
                    if backup_dir not in destination.parents or destination.exists():
                        raise RunnerError("prep_backup_collision", f"Backup destination is unsafe: {relative}")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    moved.append((source, destination))
                    shutil.move(str(source), str(destination))

                for entry in manifest_entries:
                    destination = (backup_dir / str(entry["path"])).resolve()
                    if not destination.is_file() or destination.stat().st_size != entry["size"]:
                        raise RunnerError("prep_backup_verify_failed", f"Backup size mismatch: {entry['path']}")
                    digest = hashlib.sha256()
                    with destination.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if digest.hexdigest() != entry["sha256"]:
                        raise RunnerError("prep_backup_verify_failed", f"Backup hash mismatch: {entry['path']}")

                after_move = self._git(repo, ["status", "--porcelain=v1", "--untracked-files=all"], deadline)
                if after_move.stdout:
                    raise RunnerError("prep_checkout_not_clean", "Checkout is not clean after verified backup")

                if original_remote != repo.canonical_remote_url:
                    self._git(
                        repo,
                        ["remote", "set-url", repo.sync_remote, repo.canonical_remote_url],
                        deadline,
                    )
                    remote_changed = True
                final_urls = self._git(
                    repo,
                    ["remote", "get-url", "--all", repo.sync_remote],
                    deadline,
                )
                if [line for line in final_urls.stdout.splitlines() if line] != [repo.canonical_remote_url]:
                    raise RunnerError("prep_remote_verify_failed", "Canonical remote repair did not verify exactly")
                if self._rev_parse(repo, "HEAD", deadline) != head:
                    raise RunnerError("prep_head_changed", "Preparation unexpectedly changed HEAD")
                return {
                    "repo_id": repo.repo_id,
                    "branch": repo.sync_branch,
                    "head": head,
                    "backup_path": str(backup_dir),
                    "file_count": len(manifest_entries),
                    "total_bytes": total_bytes,
                    "manifest_sha256": manifest_hash,
                    "status_sha256_before": status_hash,
                    "status_sha256_after": sha256_hex(b""),
                    "remote_before": original_remote,
                    "remote_after": repo.canonical_remote_url,
                }
            except (OSError, RunnerError) as exc:
                rollback_errors: list[str] = []
                if remote_changed:
                    try:
                        self._git(repo, ["remote", "set-url", repo.sync_remote, original_remote], deadline)
                        restored_remote = self._git(
                            repo,
                            ["remote", "get-url", "--all", repo.sync_remote],
                            deadline,
                        )
                        if [line for line in restored_remote.stdout.splitlines() if line] != [original_remote]:
                            rollback_errors.append("remote URL did not restore")
                    except Exception as rollback_exc:  # pragma: no cover - catastrophic host failure
                        rollback_errors.append(f"remote: {type(rollback_exc).__name__}")
                for source, destination in reversed(moved):
                    try:
                        if source.exists() and not destination.exists():
                            continue
                        if source.exists() or not destination.exists():
                            raise OSError(f"rollback collision or missing backup for {source}")
                        source.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(destination), str(source))
                    except OSError as rollback_exc:
                        rollback_errors.append(f"file:{source.relative_to(repo.path)}:{rollback_exc}")
                if not rollback_errors and backup_dir.exists():
                    try:
                        shutil.rmtree(backup_dir)
                    except OSError as rollback_exc:
                        rollback_errors.append(f"backup cleanup: {rollback_exc}")
                try:
                    restored = self._git(
                        repo,
                        ["status", "--porcelain=v1", "--untracked-files=all"],
                        deadline,
                        check=False,
                    )
                    if sha256_hex(restored.stdout.encode("utf-8")) != status_hash:
                        rollback_errors.append("status hash did not restore")
                except RunnerError as rollback_exc:
                    rollback_errors.append(f"status verification: {rollback_exc.code}")
                for entry in manifest_entries:
                    restored_source = (repo.path / str(entry["path"])).resolve()
                    try:
                        digest = hashlib.sha256()
                        with restored_source.open("rb") as handle:
                            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                                digest.update(chunk)
                        if (
                            restored_source.stat().st_size != entry["size"]
                            or digest.hexdigest() != entry["sha256"]
                        ):
                            rollback_errors.append(f"content did not restore: {entry['path']}")
                    except OSError as rollback_exc:
                        rollback_errors.append(f"content verification:{entry['path']}:{rollback_exc}")
                if rollback_errors:
                    raise RunnerError(
                        "prep_rollback_failed",
                        "Preparation failed and rollback could not restore the exact preflight state",
                        details={"errors": rollback_errors, "backup_path": str(backup_dir)},
                    ) from exc
                if isinstance(exc, RunnerError):
                    raise
                raise RunnerError("prep_failed", "Preparation failed before completion and was rolled back") from exc

    def preflight(self, repo: RepoConfig, payload: dict[str, Any], deadline: Deadline) -> dict[str, Any]:
        self.assert_configured(repo)
        snapshot = self._verify_checkout(repo, payload, deadline, allowed_heads={payload["base_sha"]})
        self._fetch(repo, deadline)
        remote_head = self._remote_head(repo, deadline)
        if remote_head != payload["target_sha"]:
            raise RunnerError(
                "target_mismatch",
                "Configured remote branch does not match the requested immutable target SHA",
                details={"expected": payload["target_sha"], "actual": remote_head},
            )
        self._assert_ancestor(repo, payload["base_sha"], payload["target_sha"], deadline)
        return {**snapshot, "remote_head": remote_head}

    def apply_and_verify(
        self,
        repo: RepoConfig,
        payload: dict[str, Any],
        deadline: Deadline,
        *,
        prepared: bool,
    ) -> dict[str, Any]:
        self.assert_configured(repo)
        if not prepared:
            self._verify_checkout(
                repo,
                payload,
                deadline,
                allowed_heads={payload["base_sha"], payload["target_sha"]},
            )
            self._fetch(repo, deadline)
        remote_head = self._remote_head(repo, deadline)
        if remote_head != payload["target_sha"]:
            raise RunnerError(
                "target_mismatch",
                "Configured remote branch does not match the requested immutable target SHA",
                details={"expected": payload["target_sha"], "actual": remote_head},
            )
        snapshot = self._verify_checkout(
            repo,
            payload,
            deadline,
            allowed_heads={payload["base_sha"], payload["target_sha"]},
        )
        before_sha = snapshot["head"]
        if before_sha == payload["base_sha"]:
            self._assert_ancestor(repo, before_sha, payload["target_sha"], deadline)
            merge = self._git(
                repo,
                [
                    "-c",
                    "core.hooksPath=/dev/null",
                    "merge",
                    "--ff-only",
                    "--no-edit",
                    "--",
                    payload["target_sha"],
                ],
                deadline,
                check=False,
            )
            if merge.returncode != 0:
                raise RunnerError(
                    "fast_forward_failed",
                    "Git refused the fixed fast-forward-only update",
                    details={"returncode": merge.returncode},
                )
        final = self._verify_checkout(repo, payload, deadline, allowed_heads={payload["target_sha"]})
        final_remote_head = self._remote_head(repo, deadline)
        if final_remote_head != payload["target_sha"]:
            raise RunnerError("sync_postcondition_failed", "Remote-tracking ref changed during final verification")
        return {
            "repo_id": repo.repo_id,
            "branch": repo.sync_branch,
            "remote": repo.sync_remote,
            "before_sha": before_sha,
            "after_sha": final["head"],
            "target_sha": payload["target_sha"],
            "status": [],
            "fast_forwarded": before_sha != final["head"],
        }

    def _verify_checkout(
        self,
        repo: RepoConfig,
        payload: dict[str, Any],
        deadline: Deadline,
        *,
        allowed_heads: set[str],
    ) -> dict[str, Any]:
        if not repo.path.is_dir():
            raise RunnerError("not_git_checkout", f"Configured checkout for {repo.repo_id} does not exist")
        inside = self._git(repo, ["rev-parse", "--is-inside-work-tree"], deadline, check=False)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            raise RunnerError("not_git_checkout", f"Configured path for {repo.repo_id} is not a Git worktree")
        top = self._git(repo, ["rev-parse", "--show-toplevel"], deadline).stdout.strip()
        try:
            top_path = Path(top).resolve()
        except OSError as exc:
            raise RunnerError("not_git_checkout", f"Configured path for {repo.repo_id} has an invalid top level") from exc
        if top_path != repo.path:
            raise RunnerError("wrong_checkout", f"Configured path for {repo.repo_id} is not the worktree top level")
        remote_urls = self._git(repo, ["remote", "get-url", "--all", repo.sync_remote or ""], deadline, check=False)
        urls = [line for line in remote_urls.stdout.splitlines() if line]
        if remote_urls.returncode != 0 or urls != [repo.canonical_remote_url]:
            raise RunnerError("wrong_remote", f"Configured remote for {repo.repo_id} does not match its canonical allowlist URL")
        branch = self._git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], deadline, check=False)
        if branch.returncode != 0 or branch.stdout.strip() != repo.sync_branch:
            raise RunnerError(
                "wrong_branch",
                f"Repository {repo.repo_id} is not on its configured synchronization branch",
                details={"expected": repo.sync_branch},
            )
        status = self._git(repo, ["status", "--porcelain=v1", "--untracked-files=all"], deadline)
        if status.stdout.splitlines():
            raise RunnerError(
                "dirty_worktree",
                f"Repository {repo.repo_id} has tracked or untracked changes",
                details={"entry_count": len(status.stdout.splitlines())},
            )
        head = self._rev_parse(repo, "HEAD", deadline)
        if head not in allowed_heads:
            raise RunnerError(
                "base_mismatch",
                f"Repository {repo.repo_id} HEAD is not the expected immutable base",
                details={"expected": payload["base_sha"], "actual": head},
            )
        return {"head": head, "branch": branch.stdout.strip(), "status": []}

    def _fetch(self, repo: RepoConfig, deadline: Deadline) -> None:
        assert repo.sync_remote and repo.sync_branch
        refspec = f"refs/heads/{repo.sync_branch}:refs/remotes/{repo.sync_remote}/{repo.sync_branch}"
        result = self._git(
            repo,
            ["fetch", "--no-tags", "--no-prune", repo.sync_remote, refspec],
            deadline,
            check=False,
        )
        if result.returncode != 0:
            raise RunnerError(
                "fetch_failed",
                f"Fetch from the configured remote for {repo.repo_id} failed",
                details={"returncode": result.returncode},
            )

    def _remote_head(self, repo: RepoConfig, deadline: Deadline) -> str:
        assert repo.sync_remote and repo.sync_branch
        return self._rev_parse(repo, f"refs/remotes/{repo.sync_remote}/{repo.sync_branch}", deadline)

    def _assert_ancestor(self, repo: RepoConfig, base_sha: str, target_sha: str, deadline: Deadline) -> None:
        result = self._git(repo, ["merge-base", "--is-ancestor", base_sha, target_sha], deadline, check=False)
        if result.returncode == 1:
            raise RunnerError(
                "non_fast_forward",
                "Current HEAD is not an ancestor of the requested target SHA",
                details={"base_sha": base_sha, "target_sha": target_sha},
            )
        if result.returncode != 0:
            raise RunnerError(
                "git_verification_failed",
                "Git could not verify the configured fast-forward relationship",
                details={"returncode": result.returncode},
            )

    def _rev_parse(self, repo: RepoConfig, ref: str, deadline: Deadline) -> str:
        result = self._git(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"], deadline, check=False)
        value = result.stdout.strip()
        if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", value):
            raise RunnerError("git_verification_failed", f"Git could not resolve a required immutable commit for {repo.repo_id}")
        return value

    def _git(
        self,
        repo: RepoConfig,
        args: list[str],
        deadline: Deadline,
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        env = safe_subprocess_env()
        env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
        return run_command(
            [GIT_BIN, "-C", str(repo.path), *args],
            env=env,
            timeout=deadline.remaining(120),
            check=check,
        )


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
        self.worktree = worktree.resolve()
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
        if not raw or raw.startswith("~"):
            raise RunnerError("path_escape", f"Tool path must be relative: {raw}")
        supplied = Path(raw)
        candidate = supplied.resolve() if supplied.is_absolute() else (self.worktree / supplied).resolve()
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
        read_roots.extend(self._git_metadata_read_roots())
        darwin_temp = Path(os.environ.get("TMPDIR", tempfile.gettempdir())).resolve()
        developer_tools_cache = darwin_temp.parent / "C" / "com.apple.DeveloperTools"
        if sys.platform == "darwin":
            read_roots.append(developer_tools_cache)
        if self.xcode_derived_data is not None:
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
        if sys.platform == "darwin":
            write_roots.append(developer_tools_cache)
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

    def _git_metadata_read_roots(self) -> list[Path]:
        marker = self.worktree / ".git"
        if marker.is_dir():
            return [marker.resolve()]
        if not marker.is_file():
            raise RunnerError("invalid_git_worktree", "Test worktree has no Git metadata marker")
        try:
            line = marker.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RunnerError("invalid_git_worktree", "Test worktree Git marker is unreadable") from exc
        if not line.startswith("gitdir: "):
            raise RunnerError("invalid_git_worktree", "Test worktree Git marker is invalid")
        raw_git_dir = Path(line.removeprefix("gitdir: "))
        git_dir = raw_git_dir.resolve() if raw_git_dir.is_absolute() else (self.worktree / raw_git_dir).resolve()
        if not git_dir.is_dir():
            raise RunnerError("invalid_git_worktree", "Test worktree Git directory is unavailable")
        roots = [git_dir]
        commondir = git_dir / "commondir"
        if commondir.is_file():
            try:
                raw_common = Path(commondir.read_text(encoding="utf-8").strip())
            except OSError as exc:
                raise RunnerError("invalid_git_worktree", "Test worktree common Git directory is unreadable") from exc
            common = raw_common.resolve() if raw_common.is_absolute() else (git_dir / raw_common).resolve()
            if not common.is_dir():
                raise RunnerError("invalid_git_worktree", "Test worktree common Git directory is unavailable")
            roots.append(common)
        return list(dict.fromkeys(roots))

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
            target_resolved = target.resolve()
            ancestor = target if target_resolved == workspace_resolved else target.parent
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
            if name == ".git":
                continue
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
        self.sync = GitSyncManager(config)
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

    def _effective_allowed_paths(self, payload: dict[str, Any]) -> list[str]:
        paths = list(payload.get("allowed_paths") or payload.get("scope", {}).get("paths", []) or [])
        if payload.get("permission_profile") in {"standard-worktree", "operational", "privileged"} and not paths:
            return ["."]
        return paths

    def _effective_verification_profiles(self, payload: dict[str, Any]) -> list[str]:
        verification_profiles = payload.get("verification_profiles") or []
        if verification_profiles:
            return list(verification_profiles)
        test_profile = payload.get("test_profile")
        return [test_profile] if isinstance(test_profile, str) and test_profile else []

    def _is_operational_job(self, payload: dict[str, Any]) -> bool:
        return payload.get("permission_profile") in {"operational", "privileged"}

    def _is_sync_capability_job(self, payload: dict[str, Any]) -> bool:
        return payload.get("task_type") == "sync" or payload.get("capabilities") == ["sync-registered-repo"]

    def _require_capability_enabled(self, name: str) -> CapabilityConfig:
        binding = self.config.capability_config.get(name, CapabilityConfig(enabled=False))
        if not binding.enabled:
            raise RunnerError("capability_unavailable", f"Capability {name} is not enabled for this Runner")
        return binding

    def _assert_owner_approval(self, payload: dict[str, Any], capability: str) -> None:
        approval = payload.get("owner_approval")
        if payload.get("permission_profile") != "privileged":
            return
        if not self.config.owner_pubkey:
            raise RunnerError("owner_approval_not_configured", "privileged jobs require a configured owner pubkey on the Mac")
        if not isinstance(approval, dict):
            raise RunnerError("missing_owner_approval", "privileged jobs require owner_approval")
        if approval.get("approved_by") != self.config.owner_pubkey:
            raise RunnerError("invalid_owner_approval", "owner_approval.approved_by does not match configured owner")
        approved_at = parse_iso8601(str(approval.get("approved_at", "")))
        now = dt.datetime.now(dt.timezone.utc)
        if approved_at > now + dt.timedelta(minutes=5):
            raise RunnerError("invalid_owner_approval", "owner_approval.approved_at is in the future")
        if approved_at < now - dt.timedelta(days=30):
            raise RunnerError("invalid_owner_approval", "owner_approval.approved_at is too old")
        if not str(approval.get("approval_ref", "")).strip():
            raise RunnerError("invalid_owner_approval", "owner_approval.approval_ref must be non-empty")
        binding = self.config.capability_config.get(capability, CapabilityConfig(enabled=False))
        expected_summary = self._owner_approval_summary(payload, capability, binding)
        if approval.get("summary") != expected_summary:
            raise RunnerError("invalid_owner_approval", "owner_approval.summary does not exactly match the approved deterministic action")

    def _owner_approval_summary(self, payload: dict[str, Any], capability: str, binding: CapabilityConfig) -> str:
        parts = [
            f"capability={capability}",
            f"repo={payload['repo_id']}",
            f"job={payload['job_id']}",
            f"attempt={payload['attempt']}",
        ]
        if capability == "restart-user-service" and len(binding.service_labels) == 1:
            parts.append(f"service={binding.service_labels[0]}")
        if capability == "install-user-tool" and binding.fixed_name:
            parts.append(f"tool={binding.fixed_name}")
        if capability in {"push-task-branch", "manage-pr"}:
            parts.append(f"branch=job/{payload['job_id']}")
        return ";".join(parts)

    def _validate_policy_constraints(self, payload: dict[str, Any]) -> None:
        permission_profile = payload["permission_profile"]
        scope = payload["scope"]
        network = payload["network"]
        if permission_profile == "observe":
            if scope["root"] not in {"metadata-only", "worktree"}:
                raise RunnerError("invalid_scope", "observe jobs may only use metadata-only or worktree scope")
            if network["mode"] != "none":
                raise RunnerError("network_not_allowed", "observe jobs must use network.mode=none")
        elif permission_profile == "standard-worktree":
            if scope["root"] != "worktree":
                raise RunnerError("invalid_scope", "standard-worktree jobs must use scope.root=worktree")
        elif permission_profile in {"operational", "privileged"}:
            if scope["root"] != "registered-checkout":
                raise RunnerError("invalid_scope", f"{permission_profile} jobs must use scope.root=registered-checkout")
            capabilities = payload["capabilities"]
            if len(capabilities) != 1:
                raise RunnerError("capability_required", f"{permission_profile} jobs must authorize exactly one capability")
            capability = capabilities[0]
            if capability not in OPERATIONAL_CAPABILITIES:
                raise RunnerError("capability_unavailable", f"Capability {capability} is not allowlisted")
            self._require_capability_enabled(capability)
            if payload["required_capabilities"] and any(item in OPERATIONAL_CAPABILITIES for item in payload["required_capabilities"]):
                raise RunnerError("capability_field_conflict", "required_capabilities may not be used as execution authorization")
            if capability in {"prepare-registered-repo", "restart-user-service"} and network["mode"] != "none":
                raise RunnerError("network_not_allowed", f"{capability} must use network.mode=none")
            if capability in {"sync-registered-repo", "push-task-branch", "manage-pr", "install-user-tool"} and network["mode"] != "declared-remotes-and-registries":
                raise RunnerError("network_not_allowed", f"{capability} requires network.mode=declared-remotes-and-registries")
            if permission_profile == "privileged":
                self._assert_owner_approval(payload, capability)

    def validate_payload(self, payload: dict[str, Any]) -> None:
        if payload["repo_id"] not in self.config.repos:
            raise RunnerError("unknown_repo", f"Repo {payload['repo_id']} is not in the allowlist")
        repo = self.worktrees.repo(payload["repo_id"])
        verification_profiles = self._effective_verification_profiles(payload)
        for test_profile in verification_profiles:
            self.profiles.load(test_profile)
        if repo.test_profiles and any(profile not in repo.test_profiles for profile in verification_profiles):
            disallowed = [profile for profile in verification_profiles if profile not in repo.test_profiles]
            raise RunnerError(
                "test_profile_not_allowed",
                f"Test profile {disallowed[0]} is not allowed for repo {payload['repo_id']}",
                details={"allowed_profiles": list(repo.test_profiles), "requested_profiles": verification_profiles},
            )
        self._validate_policy_constraints(payload)
        if payload["permission_profile"] == "standard-worktree" and not self.config.supervisor_allow_write_tasks:
            raise RunnerError("write_tasks_disabled", "Write tasks are disabled by config")
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
        self.worktrees.allowed_roots(dummy_root, self._effective_allowed_paths(payload))
        if self._is_sync_capability_job(payload):
            self.sync.assert_configured(repo)
            if verification_profiles != [SYNC_TEST_PROFILE]:
                raise RunnerError("sync_profile_required", f"Sync jobs must use only {SYNC_TEST_PROFILE}")
            if payload["permission_profile"] not in {"operational", "privileged"} and not payload["write"]:
                raise RunnerError("sync_write_required", "Legacy sync jobs must explicitly enable the controlled checkout update")
            if self._effective_allowed_paths(payload) != ["."]:
                raise RunnerError("sync_scope_invalid", "Sync jobs must declare the repository root as their only allowed path")
            if payload["policy_version"] == POLICY_V2:
                if payload["capabilities"] != ["sync-registered-repo"]:
                    raise RunnerError("sync_capability_required", "policy-v2 sync jobs must authorize sync-registered-repo")
            elif payload["required_capabilities"] != ["git"]:
                raise RunnerError("sync_capability_required", "Sync jobs must require exactly the allowlisted git capability")
            return
        if payload.get("capabilities") == ["prepare-registered-repo"]:
            self.sync.assert_prepare_configured(repo)
            if payload["task_type"] != "prepare":
                raise RunnerError("task_type_mismatch", "prepare-registered-repo requires task_type=prepare")
            if payload["base_sha"] != payload["target_sha"]:
                raise RunnerError("base_mismatch", "prepare-registered-repo requires target_sha=base_sha")
            if self._effective_allowed_paths(payload) != ["."]:
                raise RunnerError("prepare_scope_invalid", "Preparation must target only the registered repository root")
            self.worktrees.assert_exact_commit(repo, payload["base_sha"], network_mode="none")
            self.worktrees.assert_no_sensitive_paths(repo, payload["base_sha"], network_mode="none")
            return
        network_mode = payload.get("network", {}).get("mode", "none")
        self.worktrees.assert_exact_commit(repo, payload["base_sha"], network_mode=network_mode)
        self.worktrees.assert_exact_commit(repo, payload["target_sha"], network_mode=network_mode)
        self.worktrees.assert_no_sensitive_paths(repo, payload["target_sha"], network_mode=network_mode)
        if self._is_operational_job(payload):
            return
        if payload["execution_route"] == "ornith-then-codex":
            raise RunnerError("execution_route_not_allowed", "ornith-then-codex is accepted only for deterministic sync audit compatibility")
        if SYNC_TEST_PROFILE in verification_profiles:
            raise RunnerError("test_profile_task_mismatch", f"{SYNC_TEST_PROFILE} is reserved for sync jobs")

    def validate_dry_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.job_validator.validate(payload)
        normalized, wire_meta = normalize_job_payload(payload)
        self.validate_payload(normalized)
        return {"status": "VALIDATED", "dry_run": True, "payload": normalized, **wire_meta}

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            self.job_validator.validate(payload)
            normalized, wire_meta = normalize_job_payload(payload)
        except RunnerError as exc:
            return {"status": "REJECTED", "payload": payload, "error": exc.as_dict()}
        try:
            row = self.ledger.insert_job(
                normalized,
                wire_payload=wire_meta["wire_payload"],
                wire_payload_hash=wire_meta["wire_payload_hash"],
            )
        except RunnerError as exc:
            return {"status": "REJECTED", "payload": normalized, "error": exc.as_dict()}
        if row["status"] != "RECEIVED":
            return row
        try:
            self.validate_payload(normalized)
            return self.ledger.transition(normalized["job_id"], normalized["attempt"], {"RECEIVED"}, "VALIDATED")
        except RunnerError as exc:
            return self.ledger.transition(
                normalized["job_id"],
                normalized["attempt"],
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
        task_types = self.job_validator.schema["properties"]["task_type"]["enum"]
        repos = {
            repo_id: {
                "path": str(repo.path),
                "test_profiles": list(repo.test_profiles),
                "sync": {
                    "enabled": repo.sync_enabled,
                    "remote_url": repo.canonical_remote_url if repo.sync_enabled else None,
                    "remote": repo.sync_remote if repo.sync_enabled else None,
                    "branch": repo.sync_branch if repo.sync_enabled else None,
                },
                "prepare": {
                    "enabled": repo.prepare_enabled,
                    "backup_root": str(repo.prepare_backup_root) if repo.prepare_backup_root else None,
                    "expected_status_sha256": repo.prepare_expected_status_sha256,
                    "expected_untracked_count": repo.prepare_expected_untracked_count,
                },
            }
            for repo_id, repo in sorted(self.config.repos.items())
        }
        return {
            "capabilities": self.config.capabilities,
            "host": self.host.collect(),
            "ollama": self.ollama.status(timeout=5),
            "queue": self.ledger.queue_counts(),
            "git": {"worktrees": len(list(self.config.worktree_root.glob("*/*"))), "dirty_outside_jobs": False},
            "runner": {"write_enabled": self.config.supervisor_allow_write_tasks, "task_types": task_types},
            "repos": repos,
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
            if self._is_sync_capability_job(job["payload"]):
                return self._execute_sync(job)
            if self._is_operational_job(job["payload"]):
                return self._execute_capability(job)
            config_sha256_before = self._config_sha256()
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
                if job["payload"]["permission_profile"] == "standard-worktree":
                    self._pretest_write_gate(job, worktree)
                self._assert_not_cancelled(job["job_id"], job["attempt"])
                job = self.ledger.transition(job["job_id"], job["attempt"], {"RUNNING"}, "VERIFYING", lease_expires=lease)
                tests = self._run_tests(job, worktree, deadline)
                tests = self._with_config_immutability(tests, config_sha256_before)
                artifacts["tests"] = self._write_artifact(job, "tests", tests)
                if not tests["config_immutability"]["unchanged"]:
                    raise RunnerError(
                        "config_changed_during_execution",
                        "Runner configuration changed during job execution",
                        details=tests["config_immutability"],
                    )
                if tests.get("exit_code"):
                    failing = next((item for item in tests.get("profiles", []) if item.get("exit_code")), None)
                    raise RunnerError(
                        "test_profile_failed",
                        f"Test profile {failing['profile'] if failing else 'unknown'} exited with code {tests['exit_code']}",
                        details={"tests": tests},
                    )
                result = self._finalize(job, worktree, worker_result, tests, deadline, route=route, artifacts=artifacts)
                artifacts["acceptance"] = str(self._artifact_path(job, "acceptance"))
                artifacts["result"] = str(self._artifact_path(job, "result"))
                result["artifacts"] = dict(sorted(artifacts.items()))
                self.result_validator.validate(result)
                self._write_artifact(job, "result", result)
                tests = self._with_config_immutability(tests, config_sha256_before)
                artifacts["tests"] = self._write_artifact(job, "tests", tests)
                if not tests["config_immutability"]["unchanged"]:
                    raise RunnerError(
                        "config_changed_during_execution",
                        "Runner configuration changed during job execution",
                        details=tests["config_immutability"],
                    )
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
                    and exc.code not in {"verification_artifact_invalid", "config_changed_during_execution"}
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

    def _execute_sync(self, job: dict[str, Any]) -> dict[str, Any]:
        deadline = Deadline(job["payload"]["deadline_seconds"])
        lease = utc_now() + min(self.config.active_lease_seconds, job["payload"]["deadline_seconds"])
        repo = self.worktrees.repo(job["payload"]["repo_id"])
        prepared = False
        try:
            with self.sync.lock(repo):
                if job["status"] == "VALIDATED":
                    route = {"route": "sync", "reason": "Deterministic allowlisted repository synchronization"}
                    job = self.ledger.transition(
                        job["job_id"],
                        job["attempt"],
                        {"VALIDATED"},
                        "SUPERVISING",
                        route="sync",
                        note=route,
                        lease_expires=lease,
                    )
                    job = self.ledger.transition(
                        job["job_id"],
                        job["attempt"],
                        {"SUPERVISING"},
                        "PREPARING",
                        route="sync",
                        lease_expires=lease,
                    )
                    preflight = self.sync.preflight(repo, job["payload"], deadline)
                    job = self.ledger.transition(
                        job["job_id"],
                        job["attempt"],
                        {"PREPARING"},
                        "RUNNING",
                        route="sync",
                        note={"preflight": preflight},
                        lease_expires=lease,
                    )
                    job = self.ledger.transition(
                        job["job_id"],
                        job["attempt"],
                        {"RUNNING"},
                        "VERIFYING",
                        route="sync",
                        note={"phase": "apply_and_verify"},
                        lease_expires=lease,
                    )
                    prepared = True
                elif job["status"] != "VERIFYING":
                    raise RunnerError("invalid_state_transition", f"Cannot execute sync job from {job['status']}")
                outcome = self.sync.apply_and_verify(repo, job["payload"], deadline, prepared=prepared)
                route = {"route": "sync", "reason": "Deterministic allowlisted repository synchronization"}
                findings = [
                    {
                        "severity": "info",
                        "title": "Repository synchronized",
                        "detail": (
                            f"{outcome['repo_id']} {outcome['branch']} moved from {outcome['before_sha']} "
                            f"to {outcome['after_sha']} using a fixed fast-forward-only operation."
                        ),
                    }
                ]
                worker_result = {"route": "sync", "findings": findings, "errors": []}
                tests = {"profile": SYNC_TEST_PROFILE, "exit_code": 0, "sync": outcome}
                acceptance = {
                    "accepted": True,
                    "summary": "Configured checkout, branch, remote, target SHA, fast-forward relation, and clean final status verified.",
                    "errors": [],
                }
                artifacts = {
                    "route_decision": str(self._artifact_path(job, "route-decision")),
                    "worker_result": str(self._artifact_path(job, "worker-result")),
                    "tests": str(self._artifact_path(job, "tests")),
                    "acceptance": str(self._artifact_path(job, "acceptance")),
                    "result": str(self._artifact_path(job, "result")),
                }
                result = {
                    "job_id": job["payload"]["job_id"],
                    "attempt": job["payload"]["attempt"],
                    "status": "DONE",
                    "route": "sync",
                    "findings": findings,
                    "test_exit_code": 0,
                    "diff_hash": sha256_hex(
                        json_dumps({"before_sha": outcome["before_sha"], "after_sha": outcome["after_sha"]}).encode("utf-8")
                    ),
                    "commit_sha": outcome["after_sha"],
                    "duration_seconds": round(deadline.elapsed(), 3),
                    "errors": [],
                    "supervisor": {"decision": route, "acceptance": acceptance},
                    "artifacts": artifacts,
                }
                self.result_validator.validate(result)
                self._write_artifact(job, "route-decision", route)
                self._write_artifact(job, "worker-result", worker_result)
                self._write_artifact(job, "tests", tests)
                self._write_artifact(job, "acceptance", acceptance)
                self._write_artifact(job, "result", result)
                return self.ledger.transition(job["job_id"], job["attempt"], {"VERIFYING"}, "DONE", result=result)
        except RunnerError as exc:
            current = self.ledger.get_job(job["job_id"], job["attempt"])
            if current and current["status"] in TERMINAL_STATES:
                return current
            if current:
                return self.ledger.transition(
                    current["job_id"],
                    current["attempt"],
                    set(STATE_SEQUENCE) - TERMINAL_STATES,
                    "FAILED" if current["status"] != "RECEIVED" else "REJECTED",
                    error=exc.as_dict(),
                )
            raise

    def _execute_capability(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job["payload"]
        capability = payload["capabilities"][0]
        profile_route = "privileged" if payload["permission_profile"] == "privileged" else "operational"
        deadline = Deadline(payload["deadline_seconds"])
        lease = utc_now() + min(self.config.active_lease_seconds, payload["deadline_seconds"])
        repo = self.worktrees.repo(payload["repo_id"])
        try:
            if job["status"] == "VALIDATED":
                route = {"route": profile_route, "reason": f"Deterministic capability handler for {capability}"}
                job = self.ledger.transition(job["job_id"], job["attempt"], {"VALIDATED"}, "SUPERVISING", route=profile_route, note=route, lease_expires=lease)
                self._write_artifact(job, "route-decision", route)
                job = self.ledger.transition(job["job_id"], job["attempt"], {"SUPERVISING"}, "PREPARING", route=profile_route, lease_expires=lease)
                tests = self._run_capability_verification(job, repo, deadline)
                self._write_artifact(job, "tests", tests)
                self._assert_not_cancelled(job["job_id"], job["attempt"])
                if tests.get("exit_code"):
                    failing = next((item for item in tests.get("profiles", []) if item.get("exit_code")), None)
                    raise RunnerError(
                        "test_profile_failed",
                        f"Test profile {failing['profile'] if failing else 'unknown'} exited with code {tests['exit_code']}",
                        details={"tests": tests},
                    )
                self._assert_not_cancelled(job["job_id"], job["attempt"])
                job = self.ledger.transition(job["job_id"], job["attempt"], {"PREPARING"}, "RUNNING", route=profile_route, note={"capability": capability}, lease_expires=lease)
                self._assert_not_cancelled(job["job_id"], job["attempt"])
                outcome = self._dispatch_capability(capability, repo, payload, deadline)
                findings = [outcome["finding"]]
                worker_result = {"route": profile_route, "findings": findings, "errors": [], "outcome": outcome["result"]}
                self._write_artifact(job, "worker-result", worker_result)
                job = self.ledger.transition(job["job_id"], job["attempt"], {"RUNNING"}, "VERIFYING", route=profile_route, lease_expires=lease)
            elif job["status"] == "VERIFYING":
                route = self._read_artifact(job, "route-decision")
                worker_result = self._read_artifact(job, "worker-result")
                tests = self._read_artifact(job, "tests")
                findings = worker_result["findings"]
            else:
                raise RunnerError("invalid_state_transition", f"Cannot execute capability job from {job['status']}")
            outcome_data = worker_result.get("outcome", {})
            acceptance = {"accepted": True, "summary": f"{capability} completed deterministically", "errors": []}
            result = {
                "job_id": payload["job_id"],
                "attempt": payload["attempt"],
                "status": "DONE",
                "route": profile_route,
                "findings": findings,
                "test_exit_code": tests.get("exit_code"),
                "diff_hash": sha256_hex(canonical_json_bytes(outcome_data)),
                "commit_sha": outcome_data.get("commit_sha"),
                "duration_seconds": round(deadline.elapsed(), 3),
                "errors": [],
                "supervisor": {"decision": route, "acceptance": acceptance},
                "artifacts": {
                    "route_decision": str(self._artifact_path(job, "route-decision")),
                    "worker_result": str(self._artifact_path(job, "worker-result")),
                    "tests": str(self._artifact_path(job, "tests")),
                    "acceptance": str(self._artifact_path(job, "acceptance")),
                    "result": str(self._artifact_path(job, "result")),
                },
            }
            self.result_validator.validate(result)
            self._write_artifact(job, "acceptance", acceptance)
            self._write_artifact(job, "result", result)
            return self.ledger.transition(job["job_id"], job["attempt"], {"VERIFYING"}, "DONE", result=result)
        except RunnerError as exc:
            current = self.ledger.get_job(job["job_id"], job["attempt"])
            if current and current["status"] in TERMINAL_STATES:
                return current
            if current:
                return self.ledger.transition(
                    current["job_id"],
                    current["attempt"],
                    set(STATE_SEQUENCE) - TERMINAL_STATES,
                    "FAILED" if current["status"] != "RECEIVED" else "REJECTED",
                    error=exc.as_dict(),
                )
            raise

    def _run_capability_verification(self, job: dict[str, Any], repo: RepoConfig, deadline: Deadline) -> dict[str, Any]:
        profiles = self._effective_verification_profiles(job["payload"])
        if not profiles:
            return {"profile": None, "exit_code": 0}
        worktree = self.worktrees.prepare({**job, "payload": {**job["payload"], "write": False}}, deadline)
        try:
            return self._run_tests(job, worktree, deadline)
        finally:
            self.worktrees.cleanup(repo, worktree)

    def _dispatch_capability(self, capability: str, repo: RepoConfig, payload: dict[str, Any], deadline: Deadline) -> dict[str, Any]:
        if capability == "prepare-registered-repo":
            outcome = self.sync.prepare_registered_checkout(repo, payload, deadline)
            return {
                "finding": {
                    "severity": "info",
                    "title": "Registered repository prepared",
                    "detail": (
                        f"Backed up {outcome['file_count']} untracked files for {repo.repo_id}, "
                        "verified the manifest, and repaired only the configured canonical remote."
                    ),
                },
                "result": outcome,
            }
        if capability == "push-task-branch":
            return self._capability_push_task_branch(repo, payload, deadline)
        if capability == "manage-pr":
            return self._capability_manage_pr(repo, payload, deadline)
        if capability == "install-user-tool":
            return self._capability_install_user_tool(repo, payload, deadline)
        if capability == "restart-user-service":
            return self._capability_restart_user_service(payload, deadline)
        raise RunnerError("capability_unavailable", f"Unsupported capability: {capability}")

    def _assert_remote_matches_canonical(self, repo: RepoConfig, remote: str, deadline: Deadline) -> None:
        remote_urls = self.sync._git(repo, ["remote", "get-url", "--all", remote], deadline, check=False)
        urls = [line for line in remote_urls.stdout.splitlines() if line]
        if remote_urls.returncode != 0 or urls != [repo.canonical_remote_url]:
            raise RunnerError("wrong_remote", f"Configured remote for {repo.repo_id} does not match its canonical allowlist URL")

    def _capability_push_task_branch(self, repo: RepoConfig, payload: dict[str, Any], deadline: Deadline) -> dict[str, Any]:
        binding = self._require_capability_enabled("push-task-branch")
        remote = binding.remote or repo.sync_remote
        if not remote:
            raise RunnerError("capability_not_configured", "push-task-branch requires a configured remote binding")
        self._assert_remote_matches_canonical(repo, remote, deadline)
        branch = f"job/{payload['job_id']}"
        if not any(branch.startswith(prefix) for prefix in (binding.allowed_branch_prefixes or ("job/",))):
            raise RunnerError("protected_branch", "Derived task branch is outside the configured push allowlist")
        if any(branch == prefix.rstrip("/") or branch.startswith(prefix) for prefix in binding.protected_branch_prefixes):
            raise RunnerError("protected_branch", "Derived task branch matches a protected branch prefix")
        self.worktrees.assert_exact_commit(repo, payload["target_sha"], deadline)
        env = safe_subprocess_env()
        env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
        existing = run_command([GIT_BIN, "-C", str(repo.path), "ls-remote", "--heads", remote, branch], env=env, timeout=deadline.remaining(30), check=False)
        if existing.returncode != 0:
            raise RunnerError("remote_query_failed", f"Could not query remote branch state for {branch}")
        line = existing.stdout.strip()
        if line:
            remote_sha = line.split()[0]
            run_command(
                [GIT_BIN, "-C", str(repo.path), "fetch", "--no-tags", "--no-prune", remote, f"refs/heads/{branch}:refs/remotes/{remote}/{branch}"],
                env=env,
                timeout=deadline.remaining(30),
                check=False,
            )
            ancestor = run_command([GIT_BIN, "-C", str(repo.path), "merge-base", "--is-ancestor", remote_sha, payload["target_sha"]], env=env, timeout=deadline.remaining(30), check=False)
            if ancestor.returncode == 1:
                raise RunnerError("push_requires_force", "Remote task branch would require force push")
            if ancestor.returncode != 0:
                raise RunnerError("git_verification_failed", "Unable to verify no-force push ancestry")
        run_command([GIT_BIN, "-C", str(repo.path), "push", "--no-verify", remote, f"{payload['target_sha']}:refs/heads/{branch}"], env=env, timeout=deadline.remaining(60))
        after = run_command([GIT_BIN, "-C", str(repo.path), "ls-remote", "--heads", remote, branch], env=env, timeout=deadline.remaining(30), check=False)
        after_parts = after.stdout.strip().split()
        if after.returncode != 0 or not after_parts or after_parts[0] != payload["target_sha"]:
            raise RunnerError("push_postcondition_failed", "Remote task branch did not end at the requested immutable target SHA")
        return {
            "finding": {
                "severity": "info",
                "title": "Task branch pushed",
                "detail": f"Pushed immutable commit {payload['target_sha']} to {remote}/{branch} without force.",
            },
            "result": {"remote": remote, "branch": branch, "commit_sha": payload["target_sha"]},
        }

    def _capability_manage_pr(self, repo: RepoConfig, payload: dict[str, Any], deadline: Deadline) -> dict[str, Any]:
        binding = self._require_capability_enabled("manage-pr")
        remote = binding.remote or repo.sync_remote
        if not remote or not repo.sync_branch:
            raise RunnerError("capability_not_configured", "manage-pr requires a configured remote and base branch")
        self._assert_remote_matches_canonical(repo, remote, deadline)
        branch = f"job/{payload['job_id']}"
        if not any(branch.startswith(prefix) for prefix in (binding.allowed_branch_prefixes or ("job/",))):
            raise RunnerError("protected_branch", "Derived task branch is outside the configured PR allowlist")
        gh_bin = shutil.which("gh")
        if not gh_bin:
            raise RunnerError("command_not_found", "manage-pr requires gh to be installed on the Mac")
        if not repo.canonical_remote_url or "github.com" not in repo.canonical_remote_url:
            raise RunnerError("capability_not_configured", "manage-pr currently supports only configured GitHub remotes")
        title = f"{branch}"
        body = f"Source channel: {payload.get('metadata', {}).get('source_channel_id', 'unknown')}\nSource event: {payload.get('metadata', {}).get('source_event_id', 'unknown')}"
        env = safe_subprocess_env()
        branch_ref = run_command(
            [GIT_BIN, "-C", str(repo.path), "ls-remote", "--heads", remote, branch],
            env=env,
            timeout=deadline.remaining(30),
            check=False,
        )
        branch_parts = branch_ref.stdout.strip().split()
        if branch_ref.returncode != 0 or not branch_parts or branch_parts[0] != payload["target_sha"]:
            raise RunnerError("pr_branch_mismatch", "Task branch on the configured remote does not point to the requested immutable target SHA")
        existing = run_command(
            [gh_bin, "pr", "view", branch, "--repo", repo.canonical_remote_url, "--json", "number,url,headRefName,baseRefName"],
            env=env,
            timeout=deadline.remaining(30),
            check=False,
        )
        if existing.returncode == 0:
            pr_data = json.loads(existing.stdout or "{}")
            if pr_data.get("headRefName") != branch or pr_data.get("baseRefName") != repo.sync_branch:
                raise RunnerError("pr_state_invalid", "Existing PR does not match the deterministic task branch/base branch")
            run_command(
                [gh_bin, "pr", "edit", str(pr_data["number"]), "--repo", repo.canonical_remote_url, "--title", title, "--body", body],
                env=env,
                timeout=deadline.remaining(60),
            )
        else:
            created = run_command(
                [gh_bin, "pr", "create", "--repo", repo.canonical_remote_url, "--head", branch, "--base", repo.sync_branch, "--title", title, "--body", body],
                env=env,
                timeout=deadline.remaining(60),
            )
            url = created.stdout.strip().splitlines()[-1]
        viewed = run_command(
            [gh_bin, "pr", "view", branch, "--repo", repo.canonical_remote_url, "--json", "number,url,headRefName,baseRefName"],
            env=env,
            timeout=deadline.remaining(30),
        )
        final_pr = json.loads(viewed.stdout or "{}")
        if (
            final_pr.get("headRefName") != branch
            or final_pr.get("baseRefName") != repo.sync_branch
            or not final_pr.get("url")
            or not final_pr.get("number")
        ):
            raise RunnerError("pr_postcondition_failed", "Final PR state did not match the deterministic branch/base/identity requirements")
        if existing.returncode != 0 and final_pr.get("url") != url:
            raise RunnerError("pr_postcondition_failed", "Created PR URL did not match the final PR view")
        return {
            "finding": {
                "severity": "info",
                "title": "Task PR managed",
                "detail": f"Ensured a PR exists for {branch} targeting {repo.sync_branch}; merge was not attempted.",
            },
            "result": {"branch": branch, "base_branch": repo.sync_branch, "pr_number": final_pr.get("number"), "pr_url": final_pr.get("url"), "commit_sha": payload["target_sha"]},
        }

    def _capability_install_user_tool(self, repo: RepoConfig, payload: dict[str, Any], deadline: Deadline) -> dict[str, Any]:
        _ = repo
        _ = payload
        binding = self._require_capability_enabled("install-user-tool")
        if not binding.fixed_name or not binding.fixed_source or not binding.fixed_version:
            raise RunnerError("capability_not_configured", "install-user-tool requires fixed_name, fixed_source, and fixed_version bindings")
        if binding.fixed_source == "pipx":
            pipx_bin = shutil.which("pipx")
            if not pipx_bin:
                raise RunnerError("command_not_found", "pipx is required for install-user-tool")
            args = [pipx_bin, "install", f"{binding.fixed_name}=={binding.fixed_version}"]
        else:
            raise RunnerError("capability_not_configured", "install-user-tool currently supports only fixed pipx installations")
        env = safe_subprocess_env()
        env["CI"] = "1"
        run_command(args, env=env, timeout=deadline.remaining(300))
        listed = run_command([pipx_bin, "list", "--json"], env=env, timeout=deadline.remaining(30))
        listed_json = json.loads(listed.stdout or "{}")
        venvs = listed_json.get("venvs", {})
        details = venvs.get(binding.fixed_name)
        if not isinstance(details, dict):
            raise RunnerError("install_postcondition_failed", f"pipx did not report installed tool {binding.fixed_name}")
        metadata = details.get("metadata", {})
        package_version = metadata.get("package_version")
        if binding.fixed_version and package_version != binding.fixed_version:
            raise RunnerError("install_postcondition_failed", "Installed package version did not match the configured fixed_version")
        executable = shutil.which(binding.fixed_name)
        if not executable:
            raise RunnerError("install_postcondition_failed", "Installed tool is not on PATH after installation")
        return {
            "finding": {
                "severity": "info",
                "title": "User tool installed",
                "detail": f"Installed fixed user tool {binding.fixed_name} from {binding.fixed_source} without sudo.",
            },
            "result": {"tool": binding.fixed_name, "version": binding.fixed_version, "source": binding.fixed_source, "path": executable},
        }

    def _capability_restart_user_service(self, payload: dict[str, Any], deadline: Deadline) -> dict[str, Any]:
        _ = payload
        binding = self._require_capability_enabled("restart-user-service")
        if len(binding.service_labels) != 1:
            raise RunnerError("capability_not_configured", "restart-user-service requires exactly one configured service label")
        label = binding.service_labels[0]
        if self.config.service_label and label == self.config.service_label:
            raise RunnerError(
                "capability_not_configured",
                f"restart-user-service is fail-closed for the Runner service {label} until a one-shot helper is implemented",
            )
        uid = str(os.getuid())
        env = safe_subprocess_env()
        before = run_command(["launchctl", "print", f"gui/{uid}/{label}"], env=env, timeout=deadline.remaining(20), check=False)
        before_pid = self._launchctl_pid(before.stdout)
        if before.returncode != 0 or before_pid is None:
            raise RunnerError("service_not_running", f"launchctl did not report a running PID for {label} before restart")
        run_command(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], env=env, timeout=deadline.remaining(30))
        post = run_command(["launchctl", "print", f"gui/{uid}/{label}"], env=env, timeout=deadline.remaining(20), check=False)
        after_pid = self._launchctl_pid(post.stdout)
        if post.returncode != 0 or after_pid is None:
            raise RunnerError("service_restart_failed", f"launchctl could not verify restarted service {label}")
        if after_pid == before_pid:
            raise RunnerError("service_restart_failed", f"launchctl reported the same PID for {label} after restart")
        return {
            "finding": {
                "severity": "info",
                "title": "User service restarted",
                "detail": f"Restarted configured LaunchAgent {label} and verified launchctl status.",
            },
            "result": {"service": label, "before_pid": before_pid, "after_pid": after_pid, "canary": "launchctl-print-running"},
        }

    def _launchctl_pid(self, output: str) -> int | None:
        for pattern in (r"\bpid = (\d+)\b", r"\bPID = (\d+)\b"):
            match = re.search(pattern, output)
            if match:
                value = int(match.group(1))
                return value if value > 0 else None
        if "state = running" in output.lower():
            return 0
        return None

    def _resume_verifying(self, job: dict[str, Any], deadline: Deadline) -> dict[str, Any]:
        route = self._read_artifact(job, "route-decision")
        worker_result = self._read_artifact(job, "worker-result")
        tests = self._read_artifact(job, "tests")
        result = self._read_artifact(job, "result")
        config_immutability = tests.get("config_immutability", {})
        config_sha256_before = config_immutability.get("sha256_before")
        if isinstance(config_sha256_before, str):
            tests = self._with_config_immutability(tests, config_sha256_before)
            self._write_artifact(job, "tests", tests)
            if not tests["config_immutability"]["unchanged"]:
                error = RunnerError(
                    "config_changed_during_execution",
                    "Runner configuration changed during job execution",
                    details=tests["config_immutability"],
                )
                return self.ledger.transition(
                    job["job_id"],
                    job["attempt"],
                    {"VERIFYING"},
                    "FAILED",
                    error=error.as_dict(),
                    result=result,
                )
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

    def _config_sha256(self) -> str:
        try:
            return sha256_hex(self.config.config_path.read_bytes())
        except OSError as exc:
            raise RunnerError("config_unreadable", "Runner configuration could not be hashed") from exc

    def _with_config_immutability(self, tests: dict[str, Any], sha256_before: str) -> dict[str, Any]:
        sha256_after = self._config_sha256()
        materialized = dict(tests)
        materialized["config_immutability"] = {
            "sha256_before": sha256_before,
            "sha256_after": sha256_after,
            "unchanged": sha256_before == sha256_after,
        }
        return materialized

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
                if job["payload"]["permission_profile"] == "standard-worktree":
                    codex_workspace = stack.enter_context(
                        CodexWriteWorkspace(self.config.state_dir, worktree, self._effective_allowed_paths(job["payload"]))
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
            Every tool path argument must be repository-relative. Use "." for the repository root and never send an absolute path.
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
        repo = self.worktrees.repo(job["payload"]["repo_id"])
        changed_files, diff_bytes, _ = self.worktrees.write_gate(repo, worktree, self._effective_allowed_paths(job["payload"]))
        if changed_files > self.config.max_changed_files:
            raise RunnerError("diff_too_large", "Changed file count exceeded configured limit")
        if diff_bytes > self.config.max_diff_bytes:
            raise RunnerError("diff_too_large", "Diff bytes exceeded configured limit")

    def _run_tests(self, job: dict[str, Any], worktree: Path, deadline: Deadline) -> dict[str, Any]:
        verification_profiles = self._effective_verification_profiles(job["payload"])
        if not verification_profiles:
            raise RunnerError("test_profile_missing", "Job does not declare a verification profile")
        runs: list[VerificationRun] = []
        overall_exit_code = 0
        for name in verification_profiles:
            profile = self.profiles.load(name)
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
                runs.append(
                    VerificationRun(
                        profile=profile.name,
                        exit_code=proc.returncode,
                        stdout=trim_text(proc.stdout or "", 4000),
                        stderr=trim_text(proc.stderr or "", 4000),
                    )
                )
                if proc.returncode != 0 and overall_exit_code == 0:
                    overall_exit_code = proc.returncode
                    break
        tests_payload = {
            "profile": verification_profiles[0] if len(verification_profiles) == 1 else None,
            "profiles": [
                {
                    "profile": run.profile,
                    "exit_code": run.exit_code,
                    "stdout": run.stdout,
                    "stderr": run.stderr,
                }
                for run in runs
            ],
            "exit_code": overall_exit_code,
        }
        return tests_payload

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
        repo = self.worktrees.repo(payload["repo_id"])
        if payload["permission_profile"] == "standard-worktree":
            changed_files, diff_bytes, diff_hash = self.worktrees.write_gate(repo, worktree, self._effective_allowed_paths(payload))
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
    submit.add_argument("--dry-run", action="store_true")
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
            payload = read_payload(args.file)
            return emit(runner.validate_dry_run(payload) if args.dry_run else runner.submit(payload))
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

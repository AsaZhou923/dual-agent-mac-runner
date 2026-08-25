#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 compatibility
    import tomli as tomllib


SETTINGS_PATH = Path(
    os.environ.get("BUZZ_ACP_SETTINGS_PATH", "~/.config/buzz-acp/settings.toml")
).expanduser()
HEX_ID = re.compile(r"^[0-9a-f]{64}$")
CHANNEL_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
PUBLIC_STATES = {
    "VERIFYING": "VERIFYING",
    "DONE": "DONE",
    "FAILED": "FAILED",
    "REJECTED": "FAILED",
    "CANCELLED": "FAILED",
}


class PublisherError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def fail(message: str) -> None:
    print(f"state publisher disabled: {message}", file=sys.stderr)
    raise SystemExit(78)


def read_keychain_secret(account: str, service: str, label: str) -> str:
    try:
        secret = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-w",
                "-a",
                account,
                "-s",
                service,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        fail(f"Keychain entry is missing for {label}")
    if not secret:
        fail(f"Keychain returned an empty {label}")
    return secret


def validate_auth_tag(raw: str, expected_owner: str) -> str:
    try:
        tag = json.loads(raw)
    except json.JSONDecodeError:
        fail("BUZZ_AUTH_TAG is not valid JSON")
    if not isinstance(tag, list) or len(tag) != 4 or not all(
        isinstance(item, str) for item in tag
    ):
        fail("BUZZ_AUTH_TAG must be a four-string JSON array")
    label, owner, conditions, signature = tag
    if label != "auth" or owner != expected_owner or conditions != "":
        fail("BUZZ_AUTH_TAG does not match the configured owner authorization")
    if not re.fullmatch(r"[0-9a-f]{128}", signature):
        fail("BUZZ_AUTH_TAG signature must be 128-character lowercase hex")
    return json.dumps(tag, separators=(",", ":"))


def setting_int(
    settings: dict[str, object],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"{key} must be an integer")
    if value < minimum or value > maximum:
        fail(f"{key} must be between {minimum} and {maximum}")
    return value


def relay_http_url(relay_url: str) -> str:
    parsed = urlparse(relay_url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        fail("relay_url must be an explicit ws:// or wss:// URL")
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunparse(parsed._replace(scheme=scheme))


@dataclasses.dataclass(frozen=True)
class PublisherConfig:
    runner_db_path: Path
    state_db_path: Path
    log_path: Path
    buzz_cli_executable: Path
    relay_url: str
    lead_public_key: str
    poll_seconds: int
    send_timeout_seconds: int
    private_key: str
    auth_tag: str | None

    @classmethod
    def load(cls, settings_path: Path) -> "PublisherConfig":
        try:
            settings = tomllib.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            fail(f"could not read settings: {type(exc).__name__}")
        if settings.get("state_publisher_enabled") is not True:
            fail("state_publisher_enabled=false")

        runner_db_path = Path(
            str(settings.get("runner_db_path", "~/.local/state/mac-runner/runner.db"))
        ).expanduser()
        if not runner_db_path.is_file():
            fail("runner_db_path is missing")
        state_db_path = Path(
            str(settings.get("state_publisher_db_path", "~/.local/state/buzz-acp/state-publisher.db"))
        ).expanduser()
        log_path = Path(
            str(settings.get("state_publisher_log_path", "~/.local/state/buzz-acp/state-publisher.jsonl"))
        ).expanduser()
        buzz_cli_executable = Path(
            str(settings.get("buzz_cli_executable", ""))
        ).expanduser()
        if not buzz_cli_executable.is_file() or not os.access(buzz_cli_executable, os.X_OK):
            fail("buzz_cli_executable is missing or not executable")

        allowlist = settings.get("respond_to_allowlist", [])
        if not isinstance(allowlist, list) or len(allowlist) != 1:
            fail("respond_to_allowlist must contain exactly one Windows Lead public key")
        lead_public_key = allowlist[0]
        if not isinstance(lead_public_key, str) or not HEX_ID.fullmatch(lead_public_key):
            fail("respond_to_allowlist must contain a lowercase 64-hex public key")

        relay_url = relay_http_url(str(settings.get("relay_url", "")))
        poll_seconds = setting_int(
            settings, "state_publisher_poll_seconds", 1, minimum=1, maximum=60
        )
        send_timeout_seconds = setting_int(
            settings, "state_publisher_send_timeout_seconds", 30, minimum=1, maximum=300
        )
        private_key = read_keychain_secret(
            str(settings["keychain_account"]),
            str(settings["keychain_service"]),
            "private key",
        )
        auth_tag: str | None = None
        if settings.get("auth_tag_enabled", False) is True:
            owner_public_key = str(settings.get("owner_public_key", ""))
            if not HEX_ID.fullmatch(owner_public_key):
                fail("owner_public_key must be a lowercase 64-hex public key")
            auth_tag = validate_auth_tag(
                read_keychain_secret(
                    str(settings["auth_tag_keychain_account"]),
                    str(settings["auth_tag_keychain_service"]),
                    "NIP-OA auth tag",
                ),
                owner_public_key,
            )
        if settings.get("relay_observer", False) is True and auth_tag is None:
            fail("relay_observer requires an enabled NIP-OA auth tag")

        return cls(
            runner_db_path=runner_db_path,
            state_db_path=state_db_path,
            log_path=log_path,
            buzz_cli_executable=buzz_cli_executable,
            relay_url=relay_url,
            lead_public_key=lead_public_key,
            poll_seconds=poll_seconds,
            send_timeout_seconds=send_timeout_seconds,
            private_key=private_key,
            auth_tag=auth_tag,
        )


@dataclasses.dataclass(frozen=True)
class RunnerEvent:
    event_id: int
    job_id: str
    attempt: int
    runner_state: str
    public_state: str
    source_channel_id: str | None
    source_event_id: str | None


class PublisherStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS publisher_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS publications (
              runner_event_id INTEGER PRIMARY KEY,
              job_id TEXT NOT NULL,
              attempt INTEGER NOT NULL,
              runner_state TEXT NOT NULL,
              public_state TEXT NOT NULL,
              source_channel_id TEXT,
              source_event_id TEXT,
              status TEXT NOT NULL,
              buzz_event_id TEXT,
              error_code TEXT,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS publications_attempt_status
              ON publications(job_id, attempt, status);
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def cursor(self) -> int | None:
        row = self.conn.execute(
            "SELECT value FROM publisher_meta WHERE key = 'runner_event_cursor'"
        ).fetchone()
        return int(row["value"]) if row else None

    def set_cursor(self, event_id: int) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO publisher_meta(key, value) VALUES ('runner_event_cursor', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(event_id),),
            )

    def recover_pending(self) -> int:
        now = time.time()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE publications
                SET status = 'SEND_UNCERTAIN', error_code = 'publisher_restarted_after_send_started', updated_at = ?
                WHERE status = 'PENDING'
                """,
                (now,),
            )
        return int(cursor.rowcount)

    def publication(self, event_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM publications WHERE runner_event_id = ?", (event_id,)
        ).fetchone()

    def attempt_is_uncertain(self, job_id: str, attempt: int) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM publications
            WHERE job_id = ? AND attempt = ? AND status = 'SEND_UNCERTAIN'
            LIMIT 1
            """,
            (job_id, attempt),
        ).fetchone()
        return row is not None

    def begin(self, event: RunnerEvent) -> None:
        now = time.time()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO publications(
                  runner_event_id, job_id, attempt, runner_state, public_state,
                  source_channel_id, source_event_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    event.event_id,
                    event.job_id,
                    event.attempt,
                    event.runner_state,
                    event.public_state,
                    event.source_channel_id,
                    event.source_event_id,
                    now,
                    now,
                ),
            )

    def finish(
        self,
        event: RunnerEvent,
        status: str,
        *,
        buzz_event_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        now = time.time()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO publications(
                  runner_event_id, job_id, attempt, runner_state, public_state,
                  source_channel_id, source_event_id, status, buzz_event_id,
                  error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(runner_event_id) DO UPDATE SET
                  status = excluded.status,
                  buzz_event_id = excluded.buzz_event_id,
                  error_code = excluded.error_code,
                  updated_at = excluded.updated_at
                """,
                (
                    event.event_id,
                    event.job_id,
                    event.attempt,
                    event.runner_state,
                    event.public_state,
                    event.source_channel_id,
                    event.source_event_id,
                    status,
                    buzz_event_id,
                    error_code,
                    now,
                    now,
                ),
            )


class BuzzClient:
    def __init__(self, config: PublisherConfig) -> None:
        self.config = config

    @staticmethod
    def _event_id(value: Any) -> str | None:
        if isinstance(value, dict):
            for key in ("event_id", "eventId", "id"):
                candidate = value.get(key)
                if isinstance(candidate, str) and HEX_ID.fullmatch(candidate):
                    return candidate
            for nested in value.values():
                candidate = BuzzClient._event_id(nested)
                if candidate is not None:
                    return candidate
        elif isinstance(value, list):
            for nested in value:
                candidate = BuzzClient._event_id(nested)
                if candidate is not None:
                    return candidate
        return None

    def send(self, event: RunnerEvent) -> str:
        assert event.source_channel_id is not None
        assert event.source_event_id is not None
        content = f"{event.public_state} {event.job_id} {event.attempt}"
        args = [
            str(self.config.buzz_cli_executable),
            "--relay",
            self.config.relay_url,
            "--format",
            "json",
            "messages",
            "send",
            "--channel",
            event.source_channel_id,
            "--content",
            content,
            "--reply-to",
            event.source_event_id,
            "--mention",
            self.config.lead_public_key,
        ]
        child_env: dict[str, str] = {
            "BUZZ_PRIVATE_KEY": self.config.private_key,
            "PATH": "/usr/bin:/bin",
        }
        for key in ("HOME", "LANG", "LC_ALL", "TMPDIR"):
            value = os.environ.get(key)
            if value:
                child_env[key] = value
        if self.config.auth_tag is not None:
            child_env["BUZZ_AUTH_TAG"] = self.config.auth_tag
        try:
            result = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.config.send_timeout_seconds,
                env=child_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise PublisherError("buzz_send_timeout", "Buzz send timed out") from exc
        except OSError as exc:
            raise PublisherError("buzz_exec_failed", "Buzz CLI could not be executed") from exc
        if result.returncode != 0:
            raise PublisherError(f"buzz_exit_{result.returncode}", "Buzz CLI did not confirm publication")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PublisherError("buzz_receipt_invalid", "Buzz CLI returned invalid JSON") from exc
        event_id = self._event_id(payload)
        if event_id is None:
            raise PublisherError("buzz_receipt_missing", "Buzz CLI did not return a valid event id")
        return event_id


class StatePublisher:
    def __init__(self, config: PublisherConfig, client: BuzzClient | Any | None = None) -> None:
        self.config = config
        self.client = client or BuzzClient(config)
        self.store = PublisherStore(config.state_db_path)
        self.shutdown_requested = False

    def close(self) -> None:
        self.store.close()

    def request_shutdown(self, _signum: int, _frame: Any) -> None:
        self.shutdown_requested = True

    def _log(self, event: str, **fields: Any) -> None:
        self.config.log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), "event": event, **fields}
        with self.config.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")

    def _runner_connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.config.runner_db_path.as_uri()}?mode=ro",
            uri=True,
            timeout=30,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _runner_max_event_id(self) -> int:
        with self._runner_connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(id), 0) AS value FROM job_events").fetchone()
        return int(row["value"])

    def _events_after(self, cursor: int) -> list[RunnerEvent]:
        placeholders = ",".join("?" for _ in PUBLIC_STATES)
        with self._runner_connect() as connection:
            rows = connection.execute(
                f"""
                SELECT e.id, e.job_id, e.attempt, e.state,
                       COALESCE(
                         json_extract(j.payload_json, '$.metadata.source_channel_id'),
                         json_extract(j.payload_json, '$.context.metadata.source_channel_id')
                       ) AS source_channel_id,
                       COALESCE(
                         json_extract(j.payload_json, '$.metadata.source_event_id'),
                         json_extract(j.payload_json, '$.context.metadata.source_event_id')
                       ) AS source_event_id
                FROM job_events AS e
                JOIN jobs AS j ON j.job_id = e.job_id AND j.attempt = e.attempt
                WHERE e.id > ? AND e.state IN ({placeholders})
                ORDER BY e.id ASC
                LIMIT 100
                """,
                (cursor, *PUBLIC_STATES.keys()),
            ).fetchall()
        return [
            RunnerEvent(
                event_id=int(row["id"]),
                job_id=str(row["job_id"]),
                attempt=int(row["attempt"]),
                runner_state=str(row["state"]),
                public_state=PUBLIC_STATES[str(row["state"])],
                source_channel_id=row["source_channel_id"],
                source_event_id=row["source_event_id"],
            )
            for row in rows
        ]

    @staticmethod
    def _route_is_valid(event: RunnerEvent) -> bool:
        return bool(
            isinstance(event.source_channel_id, str)
            and CHANNEL_ID.fullmatch(event.source_channel_id)
            and isinstance(event.source_event_id, str)
            and HEX_ID.fullmatch(event.source_event_id)
        )

    def _process_event(self, event: RunnerEvent) -> None:
        existing = self.store.publication(event.event_id)
        if existing is not None:
            return
        if not self._route_is_valid(event):
            self.store.finish(event, "UNROUTABLE", error_code="source_reference_missing_or_invalid")
            self._log(
                "publication_unroutable",
                runner_event_id=event.event_id,
                job_id=event.job_id,
                attempt=event.attempt,
                state=event.public_state,
            )
            return
        if self.store.attempt_is_uncertain(event.job_id, event.attempt):
            self.store.finish(event, "SUPPRESSED", error_code="earlier_send_uncertain")
            self._log(
                "publication_suppressed",
                runner_event_id=event.event_id,
                job_id=event.job_id,
                attempt=event.attempt,
                state=event.public_state,
            )
            return

        self.store.begin(event)
        try:
            buzz_event_id = self.client.send(event)
        except PublisherError as exc:
            self.store.finish(event, "SEND_UNCERTAIN", error_code=exc.code)
            self._log(
                "publication_send_uncertain",
                runner_event_id=event.event_id,
                job_id=event.job_id,
                attempt=event.attempt,
                state=event.public_state,
                error_code=exc.code,
            )
            return
        self.store.finish(event, "SENT", buzz_event_id=buzz_event_id)
        self._log(
            "publication_sent",
            runner_event_id=event.event_id,
            job_id=event.job_id,
            attempt=event.attempt,
            state=event.public_state,
            buzz_event_id=buzz_event_id,
        )

    def process_once(self) -> int:
        recovered = self.store.recover_pending()
        if recovered:
            self._log("pending_marked_send_uncertain", count=recovered)
        cursor = self.store.cursor()
        if cursor is None:
            cursor = self._runner_max_event_id()
            self.store.set_cursor(cursor)
            self._log("publisher_bootstrapped", runner_event_cursor=cursor)
            return 0
        processed = 0
        for event in self._events_after(cursor):
            self._process_event(event)
            self.store.set_cursor(event.event_id)
            cursor = event.event_id
            processed += 1
        return processed

    def serve(self) -> None:
        self._log("publisher_started")
        while not self.shutdown_requested:
            try:
                processed = self.process_once()
            except (OSError, sqlite3.Error) as exc:
                self._log("publisher_poll_failed", error_code=type(exc).__name__)
                processed = 0
            if processed:
                continue
            sleep_until = time.monotonic() + self.config.poll_seconds
            while not self.shutdown_requested and time.monotonic() < sleep_until:
                time.sleep(min(0.5, max(0.05, sleep_until - time.monotonic())))
        self._log("publisher_stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish Runner verification and terminal states to Buzz")
    parser.add_argument("--once", action="store_true", help="process one bounded ledger batch and exit")
    parser.add_argument("--check", action="store_true", help="validate configuration and dependencies without publishing")
    args = parser.parse_args()

    config = PublisherConfig.load(SETTINGS_PATH)
    publisher = StatePublisher(config)
    try:
        if args.check:
            publisher._runner_max_event_id()
            print(json.dumps({"status": "ok", "publishing": False}, sort_keys=True))
            return
        if args.once:
            print(json.dumps({"processed": publisher.process_once()}, sort_keys=True))
            return
        signal.signal(signal.SIGTERM, publisher.request_shutdown)
        signal.signal(signal.SIGINT, publisher.request_shutdown)
        publisher.serve()
    finally:
        publisher.close()


if __name__ == "__main__":
    main()

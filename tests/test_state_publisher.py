from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLISHER_PATH = REPOSITORY_ROOT / "integrations" / "codex" / "run-state-publisher.py"
SPEC = importlib.util.spec_from_file_location("runner_state_publisher", PUBLISHER_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import bootstrap guard
    raise RuntimeError("could not load state publisher")
state_publisher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = state_publisher
SPEC.loader.exec_module(state_publisher)


CHANNEL_ID = "123e4567-e89b-12d3-a456-426614174000"
SOURCE_EVENT_ID = "a" * 64
LEAD_PUBLIC_KEY = "b" * 64
BUZZ_EVENT_ID = "c" * 64


class RecordingClient:
    def __init__(self, *, failure: state_publisher.PublisherError | None = None) -> None:
        self.failure = failure
        self.events: list[state_publisher.RunnerEvent] = []

    def send(self, event: state_publisher.RunnerEvent) -> str:
        self.events.append(event)
        if self.failure is not None:
            raise self.failure
        return BUZZ_EVENT_ID


class StatePublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.runner_db = self.root / "runner.db"
        self.state_db = self.root / "publisher.db"
        self.log_path = self.root / "publisher.jsonl"
        self.buzz = self.root / "buzz"
        self.buzz.write_text("placeholder\n", encoding="utf-8")
        self.buzz.chmod(0o700)
        connection = sqlite3.connect(self.runner_db)
        connection.executescript(
            """
            CREATE TABLE jobs (
              job_id TEXT NOT NULL,
              attempt INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              PRIMARY KEY(job_id, attempt)
            );
            CREATE TABLE job_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL,
              attempt INTEGER NOT NULL,
              state TEXT NOT NULL,
              created_at REAL NOT NULL,
              note_json TEXT
            );
            """
        )
        connection.commit()
        connection.close()
        self.config = state_publisher.PublisherConfig(
            runner_db_path=self.runner_db,
            state_db_path=self.state_db,
            log_path=self.log_path,
            buzz_cli_executable=self.buzz,
            relay_url="https://relay.example.invalid",
            lead_public_key=LEAD_PUBLIC_KEY,
            poll_seconds=1,
            send_timeout_seconds=30,
            private_key="secret-private-key",
            auth_tag=None,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _insert_job(
        self,
        job_id: str = "job-1",
        attempt: int = 1,
        *,
        channel_id: str | None = CHANNEL_ID,
        source_event_id: str | None = SOURCE_EVENT_ID,
        legacy_context: bool = False,
    ) -> None:
        reference = {
            key: value
            for key, value in {
                "source_channel_id": channel_id,
                "source_event_id": source_event_id,
            }.items()
            if value is not None
        }
        payload = (
            {"context": {"metadata": reference}}
            if legacy_context
            else {"metadata": reference}
        )
        with sqlite3.connect(self.runner_db) as connection:
            connection.execute(
                "INSERT INTO jobs(job_id, attempt, payload_json) VALUES (?, ?, ?)",
                (job_id, attempt, json.dumps(payload)),
            )

    def _insert_event(self, state: str, job_id: str = "job-1", attempt: int = 1) -> int:
        with sqlite3.connect(self.runner_db) as connection:
            cursor = connection.execute(
                "INSERT INTO job_events(job_id, attempt, state, created_at) VALUES (?, ?, ?, 1)",
                (job_id, attempt, state),
            )
            return int(cursor.lastrowid)

    def _publication_statuses(self) -> list[tuple[int, str, str | None]]:
        with sqlite3.connect(self.state_db) as connection:
            return connection.execute(
                "SELECT runner_event_id, status, error_code FROM publications ORDER BY runner_event_id"
            ).fetchall()

    def test_first_start_bootstraps_without_replaying_existing_terminal_jobs(self) -> None:
        self._insert_job()
        self._insert_event("VERIFYING")
        self._insert_event("FAILED")
        client = RecordingClient()
        publisher = state_publisher.StatePublisher(self.config, client)
        try:
            self.assertEqual(publisher.process_once(), 0)
        finally:
            publisher.close()
        self.assertEqual(client.events, [])
        self.assertEqual(self._publication_statuses(), [])

    def test_publishes_verifying_and_terminal_in_runner_event_order(self) -> None:
        self._insert_job()
        client = RecordingClient()
        publisher = state_publisher.StatePublisher(self.config, client)
        try:
            self.assertEqual(publisher.process_once(), 0)
            verifying_id = self._insert_event("VERIFYING")
            failed_id = self._insert_event("FAILED")
            self.assertEqual(publisher.process_once(), 2)
        finally:
            publisher.close()
        self.assertEqual(
            [(event.event_id, event.public_state) for event in client.events],
            [(verifying_id, "VERIFYING"), (failed_id, "FAILED")],
        )
        self.assertEqual(
            self._publication_statuses(),
            [(verifying_id, "SENT", None), (failed_id, "SENT", None)],
        )

    def test_legacy_context_source_reference_is_supported(self) -> None:
        self._insert_job(legacy_context=True)
        client = RecordingClient()
        publisher = state_publisher.StatePublisher(self.config, client)
        try:
            publisher.process_once()
            self._insert_event("DONE")
            self.assertEqual(publisher.process_once(), 1)
        finally:
            publisher.close()
        self.assertEqual(client.events[0].source_channel_id, CHANNEL_ID)
        self.assertEqual(client.events[0].source_event_id, SOURCE_EVENT_ID)

    def test_uncertain_send_is_terminal_and_suppresses_later_state(self) -> None:
        self._insert_job()
        client = RecordingClient(
            failure=state_publisher.PublisherError("buzz_send_timeout", "timed out")
        )
        publisher = state_publisher.StatePublisher(self.config, client)
        try:
            publisher.process_once()
            verifying_id = self._insert_event("VERIFYING")
            publisher.process_once()
            failed_id = self._insert_event("FAILED")
            publisher.process_once()
        finally:
            publisher.close()
        self.assertEqual(len(client.events), 1)
        self.assertEqual(
            self._publication_statuses(),
            [
                (verifying_id, "SEND_UNCERTAIN", "buzz_send_timeout"),
                (failed_id, "SUPPRESSED", "earlier_send_uncertain"),
            ],
        )

    def test_restart_with_pending_send_marks_uncertain_without_resending(self) -> None:
        self._insert_job()
        first = state_publisher.StatePublisher(self.config, RecordingClient())
        try:
            first.process_once()
            event_id = self._insert_event("VERIFYING")
            event = first._events_after(0)[0]
            first.store.begin(event)
        finally:
            first.close()

        client = RecordingClient()
        second = state_publisher.StatePublisher(self.config, client)
        try:
            self.assertEqual(second.process_once(), 1)
        finally:
            second.close()
        self.assertEqual(client.events, [])
        self.assertEqual(
            self._publication_statuses(),
            [(event_id, "SEND_UNCERTAIN", "publisher_restarted_after_send_started")],
        )

    def test_missing_source_reference_is_recorded_without_payload_or_send(self) -> None:
        self._insert_job(channel_id=None, source_event_id=None)
        client = RecordingClient()
        publisher = state_publisher.StatePublisher(self.config, client)
        try:
            publisher.process_once()
            event_id = self._insert_event("FAILED")
            publisher.process_once()
        finally:
            publisher.close()
        self.assertEqual(client.events, [])
        self.assertEqual(
            self._publication_statuses(),
            [(event_id, "UNROUTABLE", "source_reference_missing_or_invalid")],
        )
        log = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn("payload", log)
        self.assertNotIn("secret-private-key", log)

    def test_buzz_client_uses_exact_prefix_reply_and_secret_only_in_environment(self) -> None:
        event = state_publisher.RunnerEvent(
            event_id=1,
            job_id="job-1",
            attempt=2,
            runner_state="VERIFYING",
            public_state="VERIFYING",
            source_channel_id=CHANNEL_ID,
            source_event_id=SOURCE_EVENT_ID,
        )
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"event": {"id": BUZZ_EVENT_ID}}), stderr=""
        )
        with mock.patch.object(state_publisher.subprocess, "run", return_value=completed) as run:
            receipt = state_publisher.BuzzClient(self.config).send(event)
        self.assertEqual(receipt, BUZZ_EVENT_ID)
        args = run.call_args.args[0]
        child_env = run.call_args.kwargs["env"]
        self.assertIn("VERIFYING job-1 2", args)
        self.assertEqual(args[args.index("--reply-to") + 1], SOURCE_EVENT_ID)
        self.assertEqual(args[args.index("--mention") + 1], LEAD_PUBLIC_KEY)
        self.assertNotIn("secret-private-key", args)
        self.assertEqual(child_env["BUZZ_PRIVATE_KEY"], "secret-private-key")

    def test_buzz_success_without_event_receipt_is_send_uncertain(self) -> None:
        event = state_publisher.RunnerEvent(
            event_id=1,
            job_id="job-1",
            attempt=1,
            runner_state="DONE",
            public_state="DONE",
            source_channel_id=CHANNEL_ID,
            source_event_id=SOURCE_EVENT_ID,
        )
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
        with (
            mock.patch.object(state_publisher.subprocess, "run", return_value=completed),
            self.assertRaisesRegex(state_publisher.PublisherError, "valid event id"),
        ):
            state_publisher.BuzzClient(self.config).send(event)


if __name__ == "__main__":
    unittest.main()

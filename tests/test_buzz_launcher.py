from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = REPOSITORY_ROOT / "integrations" / "codex" / "run-buzz-acp.py"
SYSTEM_PROMPT_PATH = REPOSITORY_ROOT / "integrations" / "codex" / "mac-supervisor-system-prompt.md"
SPEC = importlib.util.spec_from_file_location("buzz_acp_launcher", LAUNCHER_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import bootstrap guard
    raise RuntimeError("could not load Buzz ACP launcher")
buzz_launcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = buzz_launcher
SPEC.loader.exec_module(buzz_launcher)


class BuzzLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.executable = self.root / "buzz-acp"
        self.executable.write_text("placeholder\n", encoding="utf-8")
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("Supervisor prompt\n", encoding="utf-8")
        self.settings_path = self.root / "settings.toml"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _settings(self, **overrides: object) -> str:
        values: dict[str, object] = {
            "enabled": True,
            "relay_url": "wss://relay.example.invalid",
            "respond_to_allowlist": ["1" * 64],
            "event_kinds": [9],
            "subscribe": "mentions",
            "system_prompt_file": str(self.prompt),
            "buzz_acp_executable": str(self.executable),
            "codex_acp_command": "/opt/example/bin/codex-acp",
            "child_path": "/opt/example/bin:/usr/bin:/bin",
            "keychain_account": "agent",
            "keychain_service": "private-key",
            "auth_tag_enabled": True,
            "auth_tag_keychain_account": "owner",
            "auth_tag_keychain_service": "auth-tag",
            "owner_public_key": "2" * 64,
            "relay_observer": True,
            "agents": 1,
            "heartbeat_interval": 900,
            "idle_timeout_seconds": 900,
            "max_turn_duration_seconds": 7200,
            "turn_liveness_seconds": 10,
            "lazy_pool": True,
            "idle_pool_sleep_seconds": 300,
            "exit_after_inactivity_seconds": 0,
            "permission_mode": "default",
        }
        values.update(overrides)
        lines: list[str] = []
        for key, value in values.items():
            if isinstance(value, bool):
                encoded = "true" if value else "false"
            elif isinstance(value, str):
                encoded = json.dumps(value)
            elif isinstance(value, list):
                encoded = json.dumps(value)
            else:
                encoded = str(value)
            lines.append(f"{key} = {encoded}")
        return "\n".join(lines) + "\n"

    def test_main_builds_dynamic_queue_lazy_environment_without_static_channels(self) -> None:
        self.settings_path.write_text(self._settings(), encoding="utf-8")
        auth_tag = json.dumps(["auth", "2" * 64, "", "3" * 128])
        with (
            mock.patch.object(buzz_launcher, "SETTINGS_PATH", self.settings_path),
            mock.patch.object(
                buzz_launcher,
                "read_keychain_secret",
                side_effect=["test-private-key", auth_tag],
            ),
            mock.patch.object(buzz_launcher.os, "execve") as execve,
        ):
            buzz_launcher.main()

        executable, argv, child_env = execve.call_args.args
        self.assertEqual(executable, str(self.executable))
        self.assertEqual(argv, ["buzz-acp"])
        self.assertEqual(child_env["BUZZ_PRIVATE_KEY"], "test-private-key")
        self.assertEqual(child_env["BUZZ_ACP_AGENT_COMMAND"], "/opt/example/bin/codex-acp")
        self.assertEqual(child_env["BUZZ_ACP_KINDS"], "9")
        self.assertEqual(child_env["BUZZ_ACP_DEDUP"], "queue")
        self.assertEqual(child_env["BUZZ_ACP_MULTIPLE_EVENT_HANDLING"], "queue")
        self.assertEqual(child_env["BUZZ_ACP_LAZY_POOL"], "true")
        self.assertEqual(child_env["BUZZ_ACP_HEARTBEAT_INTERVAL"], "900")
        self.assertEqual(child_env["BUZZ_ACP_PERMISSION_MODE"], "default")
        self.assertNotIn("BUZZ_ACP_CHANNELS", child_env)
        self.assertNotIn("BUZZ_ACP_MCP_COMMAND", child_env)

    def test_permission_bypass_and_boolean_integer_are_fail_closed(self) -> None:
        for overrides in (
            {"permission_mode": "bypassPermissions"},
            {"heartbeat_interval": True},
        ):
            with self.subTest(overrides=overrides):
                self.settings_path.write_text(self._settings(**overrides), encoding="utf-8")
                with (
                    mock.patch.object(buzz_launcher, "SETTINGS_PATH", self.settings_path),
                    self.assertRaisesRegex(SystemExit, "78"),
                ):
                    buzz_launcher.main()

    def test_relay_observer_requires_owner_authorization_tag(self) -> None:
        self.settings_path.write_text(
            self._settings(auth_tag_enabled=False, relay_observer=True),
            encoding="utf-8",
        )
        with (
            mock.patch.object(buzz_launcher, "SETTINGS_PATH", self.settings_path),
            mock.patch.object(buzz_launcher, "read_keychain_secret", return_value="test-private-key"),
            self.assertRaisesRegex(SystemExit, "78"),
        ):
            buzz_launcher.main()

    def test_supervisor_prompt_requires_ledger_recordable_state_prefixes(self) -> None:
        prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        self.assertIn("first reply", prompt)
        self.assertIn("`ACK <job_id> <attempt>`", prompt)
        self.assertIn("`RUNNING <job_id> <attempt>`", prompt)
        self.assertIn("`VERIFYING <job_id> <attempt>`", prompt)
        self.assertIn("`DONE <job_id> <attempt>`", prompt)
        self.assertIn("`FAILED <job_id> <attempt>`", prompt)
        self.assertIn("never slash or colon forms", prompt)
        self.assertIn("backfill ACK", prompt)


if __name__ == "__main__":
    unittest.main()

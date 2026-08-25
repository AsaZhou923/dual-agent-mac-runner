#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 compatibility
    import tomli as tomllib


SETTINGS_PATH = Path(
    os.environ.get("BUZZ_ACP_SETTINGS_PATH", "~/.config/buzz-acp/settings.toml")
).expanduser()
DEFAULT_HEARTBEAT_PROMPT_PATH = Path(__file__).with_name(
    "mac-supervisor-heartbeat-prompt.md"
)
HEX_PUBKEY = re.compile(r"^[0-9a-f]{64}$")


def fail(message: str) -> None:
    print(f"buzz-acp disabled: {message}", file=sys.stderr)
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
    if label != "auth":
        fail('BUZZ_AUTH_TAG label must be "auth"')
    if owner != expected_owner:
        fail("BUZZ_AUTH_TAG owner does not match owner_public_key")
    if conditions != "":
        fail("BUZZ_AUTH_TAG conditions must be empty for profile and observer events")
    if not re.fullmatch(r"[0-9a-f]{128}", signature):
        fail("BUZZ_AUTH_TAG signature must be 128-character lowercase hex")
    return json.dumps(tag, separators=(",", ":"))


def setting_int(
    settings: dict[str, object],
    key: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = 86_400,
) -> int:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"{key} must be an integer")
    if value < minimum or value > maximum:
        fail(f"{key} must be between {minimum} and {maximum}")
    return value


def main() -> None:
    settings = tomllib.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    if settings.get("enabled") is not True:
        fail("settings.toml has enabled=false")

    buzz_acp_executable = Path(
        str(settings.get("buzz_acp_executable", ""))
    ).expanduser()
    if not buzz_acp_executable.is_file():
        fail("registered Buzz ACP application executable is missing")

    relay_url = str(settings.get("relay_url", ""))
    parsed = urlparse(relay_url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        fail("relay_url must be an explicit ws:// or wss:// URL")

    allowlist = settings.get("respond_to_allowlist", [])
    if not isinstance(allowlist, list) or not allowlist:
        fail("respond_to_allowlist must contain the Windows Lead public key")
    if not all(isinstance(item, str) and HEX_PUBKEY.fullmatch(item) for item in allowlist):
        fail("every allowlist entry must be a 64-character lowercase hex public key")

    event_kinds = settings.get("event_kinds", [])
    if event_kinds != [9]:
        fail("event_kinds must be exactly [9] for the production mention-only intake")

    subscribe = str(settings.get("subscribe", ""))
    if subscribe != "mentions":
        fail("production subscription must remain mentions-only")

    system_prompt_file = Path(str(settings.get("system_prompt_file", ""))).expanduser()
    if not system_prompt_file.is_file():
        fail("system_prompt_file is missing")
    heartbeat_prompt_file = Path(
        str(settings.get("heartbeat_prompt_file", DEFAULT_HEARTBEAT_PROMPT_PATH))
    ).expanduser()
    if not heartbeat_prompt_file.is_file():
        fail("heartbeat_prompt_file is missing")

    agents = setting_int(settings, "agents", 1, minimum=1, maximum=1)
    heartbeat_interval = setting_int(settings, "heartbeat_interval", 900)
    idle_timeout_seconds = setting_int(settings, "idle_timeout_seconds", 900, minimum=1)
    max_turn_duration_seconds = setting_int(
        settings, "max_turn_duration_seconds", 7200, minimum=1
    )
    turn_liveness_seconds = setting_int(
        settings, "turn_liveness_seconds", 10, minimum=1
    )
    idle_pool_sleep_seconds = setting_int(
        settings, "idle_pool_sleep_seconds", 300, minimum=1
    )
    exit_after_inactivity_seconds = setting_int(
        settings, "exit_after_inactivity_seconds", 0
    )
    permission_mode = str(settings.get("permission_mode", "default"))
    if permission_mode != "default":
        fail("permission_mode must remain default until Runner-only tool and network gates pass")
    if settings.get("lazy_pool", True) is not True:
        fail("lazy_pool must remain enabled")

    secret = read_keychain_secret(
        str(settings["keychain_account"]),
        str(settings["keychain_service"]),
        "private key",
    )

    auth_tag = None
    auth_tag_enabled = settings.get("auth_tag_enabled", False) is True
    relay_observer = settings.get("relay_observer", False) is True
    if auth_tag_enabled:
        expected_owner = str(settings.get("owner_public_key", ""))
        if not HEX_PUBKEY.fullmatch(expected_owner):
            fail("owner_public_key must be a 64-character lowercase hex public key")
        raw_auth_tag = read_keychain_secret(
            str(settings["auth_tag_keychain_account"]),
            str(settings["auth_tag_keychain_service"]),
            "NIP-OA auth tag",
        )
        auth_tag = validate_auth_tag(raw_auth_tag, expected_owner)
    if relay_observer and auth_tag is None:
        fail("relay_observer requires an enabled NIP-OA auth tag")

    child_env: dict[str, str] = {}
    for key in ("HOME", "LANG", "LC_ALL", "TMPDIR"):
        value = os.environ.get(key)
        if value:
            child_env[key] = value
    child_env["PATH"] = str(
        settings.get(
            "child_path",
            "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        )
    )
    child_env.update(
        {
            "BUZZ_PRIVATE_KEY": secret,
            "BUZZ_RELAY_URL": relay_url,
            "BUZZ_ACP_AGENT_COMMAND": str(settings.get("codex_acp_command", "codex-acp")),
            "BUZZ_ACP_AGENT_ARGS": "",
            "BUZZ_ACP_AGENTS": str(agents),
            "BUZZ_ACP_HEARTBEAT_INTERVAL": str(heartbeat_interval),
            "BUZZ_ACP_HEARTBEAT_PROMPT_FILE": str(heartbeat_prompt_file),
            "BUZZ_ACP_IDLE_TIMEOUT": str(idle_timeout_seconds),
            "BUZZ_ACP_MAX_TURN_DURATION": str(max_turn_duration_seconds),
            "BUZZ_ACP_RESPOND_TO": "allowlist",
            "BUZZ_ACP_RESPOND_TO_ALLOWLIST": ",".join(allowlist),
            "BUZZ_ACP_ALLOWED_RESPOND_TO": "owner-only,allowlist",
            "BUZZ_ACP_SUBSCRIBE": subscribe,
            "BUZZ_ACP_KINDS": ",".join(str(item) for item in event_kinds),
            "BUZZ_ACP_SYSTEM_PROMPT_FILE": str(system_prompt_file),
            "BUZZ_ACP_DEDUP": "queue",
            "BUZZ_ACP_MULTIPLE_EVENT_HANDLING": "queue",
            "BUZZ_ACP_TURN_LIVENESS_SECS": str(turn_liveness_seconds),
            "BUZZ_ACP_NO_MEMORY": "true",
            "BUZZ_ACP_SESSION_TITLE": "Mac Codex Supervisor",
            "BUZZ_ACP_RELAY_OBSERVER": "true" if relay_observer else "false",
            "BUZZ_ACP_LAZY_POOL": "true",
            "BUZZ_ACP_IDLE_POOL_SLEEP": str(idle_pool_sleep_seconds),
            "BUZZ_ACP_EXIT_AFTER_INACTIVITY": str(exit_after_inactivity_seconds),
            "BUZZ_ACP_PERMISSION_MODE": permission_mode,
        }
    )
    if auth_tag is not None:
        child_env["BUZZ_AUTH_TAG"] = auth_tag
    os.execve(str(buzz_acp_executable), ["buzz-acp"], child_env)


if __name__ == "__main__":
    main()

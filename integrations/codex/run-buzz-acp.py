#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse


SETTINGS_PATH = Path(
    os.environ.get("BUZZ_ACP_SETTINGS_PATH", "~/.config/buzz-acp/settings.toml")
).expanduser()
HEX_PUBKEY = re.compile(r"^[0-9a-f]{64}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


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

    channels = settings.get("channel_ids", [])
    if not isinstance(channels, list) or not channels:
        fail("channel_ids must contain the production coordination channel")
    if not all(isinstance(item, str) and UUID.fullmatch(item) for item in channels):
        fail("every channel_id must be a lowercase UUID")

    event_kinds = settings.get("event_kinds", [])
    if not isinstance(event_kinds, list) or not event_kinds:
        fail("event_kinds must be explicit")
    if not all(isinstance(item, int) and 0 <= item <= 65535 for item in event_kinds):
        fail("every event kind must be an integer from 0 through 65535")

    subscribe = str(settings.get("subscribe", ""))
    if subscribe != "mentions":
        fail("production subscription must remain mentions-only")

    system_prompt_file = Path(str(settings.get("system_prompt_file", ""))).expanduser()
    if not system_prompt_file.is_file():
        fail("system_prompt_file is missing")

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
            "BUZZ_ACP_AGENTS": str(int(settings.get("agents", 1))),
            "BUZZ_ACP_HEARTBEAT_INTERVAL": str(int(settings.get("heartbeat_interval", 0))),
            "BUZZ_ACP_IDLE_TIMEOUT": str(int(settings.get("idle_timeout_seconds", 620))),
            "BUZZ_ACP_MAX_TURN_DURATION": str(int(settings.get("max_turn_duration_seconds", 7200))),
            "BUZZ_ACP_RESPOND_TO": "allowlist",
            "BUZZ_ACP_RESPOND_TO_ALLOWLIST": ",".join(allowlist),
            "BUZZ_ACP_ALLOWED_RESPOND_TO": "owner-only,allowlist",
            "BUZZ_ACP_SUBSCRIBE": subscribe,
            "BUZZ_ACP_KINDS": ",".join(str(item) for item in event_kinds),
            "BUZZ_ACP_CHANNELS": ",".join(channels),
            "BUZZ_ACP_SYSTEM_PROMPT_FILE": str(system_prompt_file),
            "BUZZ_ACP_DEDUP": "queue",
            "BUZZ_ACP_MULTIPLE_EVENT_HANDLING": "queue",
            "BUZZ_ACP_TURN_LIVENESS_SECS": "10",
            "BUZZ_ACP_NO_MEMORY": "true",
            "BUZZ_ACP_SESSION_TITLE": "Mac Codex Supervisor",
            "BUZZ_ACP_RELAY_OBSERVER": "true" if relay_observer else "false",
        }
    )
    if auth_tag is not None:
        child_env["BUZZ_AUTH_TAG"] = auth_tag
    os.execve(str(buzz_acp_executable), ["buzz-acp"], child_env)


if __name__ == "__main__":
    main()

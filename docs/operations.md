# Operations

## Runtime separation

Keep these locations outside the repository:

- state and SQLite: `~/.local/state/mac-runner`
- artifacts and worktrees: `~/.local/share/mac-runner`
- real Runner and Buzz settings: a user-owned configuration directory
- Ollama models, manifests, history, logs, and databases: Ollama-owned storage

The templates under `launchd/` and `integrations/codex/` contain placeholders.
Generate installed plists from them; never commit an installed plist containing
real usernames, paths, identity values, or credentials.

## Safe cutover

Use this order when replacing a running installation:

1. Copy source to the new repository; do not move the live directory.
2. Run unit tests, schema checks, plist lint, and dependency preflight.
3. Generate real configuration outside Git.
4. Update the Runner wrapper, Mac Supervisor prompt, and one LaunchAgent.
5. Restart only the Runner service.
6. Verify status, an empty queue, Ollama readiness, and the original-thread handshake.
7. Keep the previous source path and plist available for rollback.

Runner SQLite state is authoritative. Buzz publication alone does not prove job
receipt or completion; use the original thread's ACK, RUNNING, VERIFYING, and
terminal evidence together with the Runner ledger.

`SEND_UNCERTAIN` is a terminal publishing outcome for an attempt and must not be
automatically retried. The cross-machine publication protocol belongs in the
parent dual-agent system, not in Runner's deterministic state machine.

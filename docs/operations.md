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

1. Record both LaunchAgents, PIDs, Runner status, repository fingerprints, and
   SQLite integrity/schema/job/event counts.
2. Stop Buzz ACP, then Runner, and confirm that no process can write the ledger.
3. Back up Runner source/config/ledger/LaunchAgent and the Buzz launcher,
   settings, prompt, and LaunchAgent without exporting Keychain secrets.
4. Run unit tests, schema checks, plist lint, and configuration preflight from
   an isolated checkout.
5. Deploy Runner source and schemas, then open the production ledger once to
   apply the additive SQLite migration and legacy-row backfill.
6. Confirm SQLite integrity, job/event counts, terminal-state counts, and exact
   legacy wire replay before enabling services.
7. Deploy the Buzz launcher/settings/prompt, start Runner first, then Buzz ACP.
8. Verify Runner queue/capabilities, Buzz owner resolution, dynamic channel
   discovery, Queue/Queue behavior, lazy child creation, heartbeat, and observer.
9. Keep the previous source, configuration, plists, and ledger backup available
   until a fresh cross-machine attempt reaches a consistent terminal state.

The migration only adds policy/wire/approval/capability columns. Existing job
and event rows remain in place; legacy payloads are used to backfill their
original wire JSON/hash and canonical policy-v1 fields. An exact replay must
return the stored attempt without adding another job or event.

## Policy and capability rollout

- Leave every operational capability disabled until its repository, remote,
  branch prefix, fixed tool, or service binding is explicit in external config.
- `prepare-registered-repo` is network-free and requires an immutable external
  config snapshot: exact untracked-status SHA-256/count, allowed pre-repair
  remote URLs, canonical remote, and an external backup root. It moves only
  contained regular untracked files, writes and verifies a size/SHA-256
  manifest, repairs only the fixed remote URL, and rolls back on failure.
- `sync-registered-repo` requires the reserved `git-sync-verify` profile and a
  clean registered checkout on the configured branch and canonical remote.
- Keep `permission_mode=default`. Do not enable `bypassPermissions` until the
  Runner-only side-effect tool surface and network boundary have independent
  end-to-end evidence.
- Generic lockfile dependency acquisition is not implemented by this release;
  verification commands remain no-network and credential-free.
- Runner self-restart is fail-closed until a one-shot helper can verify the new
  process after the current Runner exits. Set `runner.service_label` to the
  installed Runner LaunchAgent label so this boundary is enforced.

Runner SQLite state is authoritative. Buzz publication alone does not prove job
receipt or completion; use the original thread's ACK, RUNNING, VERIFYING, and
terminal evidence together with the Runner ledger.

`SEND_UNCERTAIN` is a terminal publishing outcome for an attempt and must not be
automatically retried. The cross-machine publication protocol belongs in the
parent dual-agent system, not in Runner's deterministic state machine.

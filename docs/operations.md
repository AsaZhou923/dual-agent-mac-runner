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

1. Record all three LaunchAgents, PIDs, Runner status, repository fingerprints,
   Runner/publisher SQLite integrity, schema, job, event, cursor, and publication
   counts.
2. Stop Buzz ACP and the state publisher, then Runner, and confirm that no
   process can write either database.
3. Back up Runner source/config/ledger/LaunchAgent and the Buzz launcher,
   settings, prompt, and LaunchAgent without exporting Keychain secrets.
4. Run unit tests, schema checks, plist lint, and configuration preflight from
   an isolated checkout.
5. Deploy Runner source and schemas, then open the production ledger once to
   apply the additive SQLite migration and legacy-row backfill.
6. Confirm SQLite integrity, job/event counts, terminal-state counts, and exact
   legacy wire replay before enabling services.
7. Deploy the Buzz launcher/settings/prompts and state publisher. Start Runner,
   then the state publisher, then Buzz ACP.
8. Verify Runner queue/capabilities, Buzz owner resolution, dynamic channel
   discovery, Queue/Queue behavior, lazy child creation, the repository audit
   heartbeat, state-publisher cursor/receipt health, and observer.
9. Keep the previous source, configuration, plists, and ledger backup available
   until a fresh cross-machine attempt reaches a consistent terminal state.

For the fresh read-only attempt, require all of the following before declaring
the cutover healthy:

- the original thread begins with exact `ACK <job_id> <attempt>` and proceeds
  through on-time `RUNNING`, `VERIFYING`, and one terminal state;
- the job payload contains a canonical 36-character source channel UUID and
  64-hex source event id, and production has
  `supervisor.require_source_metadata=true`;
- the direct supervisor publishes ACK/RUNNING, the deterministic publisher
  publishes VERIFYING/terminal, and the heartbeat does not duplicate either;
- the real macOS Seatbelt profile completes `/usr/bin/git diff --check HEAD`
  without `xcrun_db` denial while the surrounding Darwin temporary directory
  remains unreadable as a subtree;
- the structured result contains equal `configuration.before_sha256`,
  `after_sha256`, and `loaded_sha256` values with `unchanged=true`;
- the terminal Runner row, Windows ledger, queue/worktree counts, and registered
  checkout fingerprint agree.

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
- `self-update-runner` is privileged, default-disabled, and requires the
  reserved `self-update-runner` profile, a configured owner pubkey, the fixed
  Runner LaunchAgent label, an external one-shot helper path, and a registered
  checkout whose repo id, canonical URL, remote name, and branch exactly match
  the separate capability binding. The current app `.source-commit` must
  equal `base_sha`; the fetched fixed branch must equal `target_sha`; and
  `target_sha` must be a fast-forward descendant of `base_sha` unless the
  request is a deterministic no-op. The candidate is materialized from Git
  archive blobs, rejects unsafe paths/symlinks/special files, preserves config,
  and leaves the job in `VERIFYING` until the restarted Runner reads the helper
  result.
- Keep `permission_mode=default`. Do not enable `bypassPermissions` until the
  Runner-only side-effect tool surface and network boundary have independent
  end-to-end evidence.
- Generic lockfile dependency acquisition is not implemented by this release;
  verification commands remain no-network and credential-free.
- Install the self-update helper outside `runner.app_dir` during bootstrap.
  The helper swaps app directories, restarts only the fixed LaunchAgent,
  verifies new PID, source marker, config hash, SQLite integrity, and writes a
  bounded result. On failure it attempts to restore the previous app and its
  consistent SQLite backup before reporting `FAILED`. Keep `staging_root` on
  the same filesystem as `runner.app_dir`; cross-device self-update is rejected
  before mutation.

### First self-update bootstrap

The deployed Runner cannot authorize a capability it does not yet contain. The
first installation therefore remains a reviewed manual deployment:

1. Deploy one exact reviewed Mac Runner commit and run the complete macOS test
   suite before enabling the capability.
2. Copy `scripts/mac-runner-self-update-helper` to the fixed external
   `helper_path`, owned by the Runner user with mode `0700` or `0755` and no
   group/other write bit.
3. Register the Mac Runner source checkout with its canonical public remote,
   fixed remote name, fixed branch, and `self-update-runner` test profile.
4. Set `runner.app_dir`, `runner.service_label`, owner pubkey, helper path, and a
   same-filesystem staging root in external config. Seed `app_dir/.source-commit`
   with the exact deployed 40-character commit.
5. Enable `self-update-runner`, restart once through the existing reviewed
   deployment path, and require `status.runner.self_update.ready=true` with the
   expected source marker, service label, and external helper.
6. Only after a schema-only dry run remains `job_not_found` and no repository or
   service mutation occurred may Windows set its separate self-update verified
   gate. Every later update is a new privileged `(job_id, attempt)`.

The privileged job updates the Runner app only. It never replaces the external
helper that enforces the swap; updating that trust root requires another
reviewed bootstrap.

Runner SQLite state is authoritative. Buzz publication alone does not prove job
receipt or completion; use the original thread's ACK, RUNNING, VERIFYING, and
terminal evidence together with the Runner ledger.

### Deterministic Buzz state publication

Run `integrations/codex/run-state-publisher.py` as its own LaunchAgent using the
example plist. It reads the Runner database in SQLite read-only mode and queries
only event identity/state plus canonical source references. It publishes
`VERIFYING` and the first terminal state to the original thread with exact
`VERIFYING|DONE|FAILED <job_id> <attempt>` content, while Mac Codex continues to
own ACK and RUNNING.

The publisher stores its cursor and receipts outside the repository. On first
start it seeds the cursor at the current maximum Runner event id, so deployment
never backfills historical attempts. Before every network send it commits a
`PENDING` row. A confirmed Buzz event id changes that row to `SENT`; timeout,
process interruption, nonzero exit, invalid JSON, or a missing event id changes
it to `SEND_UNCERTAIN`. Startup converts abandoned `PENDING` rows to
`SEND_UNCERTAIN`. Later states for that attempt are `SUPPRESSED`, never resent.
Missing or invalid source references are `UNROUTABLE`.

Use these read-only checks after deployment:

```sh
python3 integrations/codex/run-state-publisher.py --check
sqlite3 -readonly ~/.local/state/buzz-acp/state-publisher.db \
  "SELECT status, COUNT(*) FROM publications GROUP BY status;"
sqlite3 -readonly ~/.local/state/buzz-acp/state-publisher.db \
  "SELECT value FROM publisher_meta WHERE key='runner_event_cursor';"
```

Production Buzz ACP may retain the 30-second heartbeat as an audit backstop. It
reports expired Runner leases and publisher `UNROUTABLE`/`SEND_UNCERTAIN`
records, but never publishes or retries lifecycle states. The Runner daemon also
reconciles expired `SUPERVISING`, `PREPARING`, and `RUNNING` leases every service
loop when it can acquire the task lock, even if no new pending job exists.

`SEND_UNCERTAIN` is a terminal publishing outcome for an attempt and must not be
automatically retried. Windows reconciliation policy remains in the parent
dual-agent system; credentialed transport remains in the separate Supervisor
integration process, never in Runner's deterministic state machine.

# dual-agent-mac-runner

Deterministic macOS execution boundary for a supervised dual-agent system:

```text
Windows Codex Lead
  -> Buzz
  -> Mac Codex Supervisor
  -> Mac Runner
  -> Ornith/Ollama Worker
```

Mac Runner is a Python daemon/CLI, not an LLM. It owns validation, the SQLite
ledger, `(job_id, attempt)` idempotency, disposable Git worktrees, path and
profile policy, deadlines, verification, and structured results. Mac Codex is
the required local supervisor; Ornith is a constrained read-only worker.

The current contract accepts strict `mac-job/v1` policy v2 payloads while
retaining a legacy adapter for `write`, `allowed_paths`, and `test_profile`.
Policy v2 separates descriptive requirements from execution authorization with
four permission profiles: `observe`, `standard-worktree`, `operational`, and
`privileged`.

## Safety properties

- Standard-library-only Python 3.14 implementation.
- Exact 40-character Git SHAs and a configured repository allowlist.
- Strict top-level policy-v2 schema and a 48 KiB canonical wire-payload budget.
- Original wire payload/hash plus canonical policy fields retained in SQLite.
- Approved test profiles executed without `shell=True`.
- Temporary HOME plus a no-network Seatbelt profile for tests.
- Worktree writes are limited by scope, changed-file count, diff size, and
  configured sensitive-path patterns.
- Operational actions use fixed capability handlers for reversible
  registered-repository preparation, registered-repository sync, task-branch
  push, PR management, fixed user-tool installation, and configured
  user-service restart.
- Ornith tool paths are normalized only when they resolve inside the fixed task
  worktree; outside paths and symlink escapes remain fail-closed.
- Privileged actions require an exact, recent owner approval bound to one
  deterministic action summary.
- Credentials are removed from worker and test environments.
- Runner state, task artifacts, worktrees, logs, and model files stay outside Git.

## Repository layout

- `runner.py` — daemon and CLI.
- `tests/` — unit and integration tests using temporary repositories.
- `schemas/` — job and result contracts.
- `profiles/` — production-safe generic profiles.
- `examples/` — synthetic configuration and one synthetic profile example.
- `launchd/` — parameterized LaunchAgent template.
- `integrations/codex/` — Mac Codex Supervisor and Buzz ACP integration.
- `agents/ornith/` — trackable Ollama model declaration; never model weights.

## Local setup

1. Copy `examples/config.example.toml` to a location outside the repository.
2. Replace every absolute-path placeholder and configure only approved repos.
3. Keep write tasks disabled until local tests and the integration handshake pass.
4. Set `MAC_RUNNER_CONFIG` when using `scripts/mac-runner`.

```bash
MAC_RUNNER_CONFIG=/path/to/config.toml scripts/mac-runner status
MAC_RUNNER_PYTHON=/path/to/python3 scripts/mac-runner --help
/path/to/python3 -m unittest discover -s tests -v
```

Do not commit a real `config.toml`, SQLite database, task artifact, Buzz setting,
Keychain value, Codex session, Ollama manifest, GGUF file, log, or worktree.

The Buzz ACP launcher template is mention-only and dynamically subscribes to
Relay channels where the Agent is a member. It enforces one Agent, Queue/Queue
event handling, lazy child creation, a read-only heartbeat, relay observation,
and `permission_mode=default`; it deliberately does not inject a static channel
list or a generic MCP shell command.

See `docs/operations.md` for the safe cutover sequence and
`docs/dependencies.md` for the audited dependency baseline.

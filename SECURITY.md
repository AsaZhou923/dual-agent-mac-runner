# Security

Do not commit credentials, private/public identity material copied from a real
installation, `.env` files, real configuration, SQLite databases, WAL/SHM files,
logs, task payloads or artifacts, Codex/Buzz sessions, worktrees, model manifests,
GGUF files, Ollama blobs, caches, or installed LaunchAgent plists.

Buzz private keys and authorization tags must remain in macOS Keychain. The
integration launcher and deterministic state publisher may read them only at
process startup and pass them only to Buzz ACP or the fixed Buzz CLI process.
They must never place secrets in argv, logs, publisher SQLite rows, Runner,
Codex, Ornith, worker, or test environments.

Keep operational capabilities default-deny and bind them to fixed remotes,
branch prefixes, package names/versions, and user-service labels in external
configuration. Never replace a capability handler with free-form shell input.
Privileged jobs require an owner approval whose signer, timestamp, reference,
and deterministic action summary all match the requested action.

Keep the bootstrapped self-update helper outside `runner.app_dir`, owned by the
same user and non-writable by group/other. `self-update-runner` must remain
default-disabled until the fixed source checkout/remote/branch, service label,
same-filesystem staging root, `.source-commit`, helper hash, and rollback path
have been verified. The target job may select only an immutable SHA already
reachable from that fixed branch; it may not supply paths, commands, remotes,
service names, or downgrade policy.
The capability never replaces its external helper; changing that trust root is
a separate reviewed bootstrap operation.

Configure sensitive-path patterns for every repository. Runner rejects a target
commit containing a tracked match before model, test, worktree, Git, network, or
service activity, and rejects new or modified matches again at the write gate.

Report suspected vulnerabilities privately to the repository owner. Include a
minimal reproduction without real task content, identities, or secrets.

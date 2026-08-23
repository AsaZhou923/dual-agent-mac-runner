# Security

Do not commit credentials, private/public identity material copied from a real
installation, `.env` files, real configuration, SQLite databases, WAL/SHM files,
logs, task payloads or artifacts, Codex/Buzz sessions, worktrees, model manifests,
GGUF files, Ollama blobs, caches, or installed LaunchAgent plists.

Buzz private keys and authorization tags must remain in macOS Keychain. The
integration launcher may read them only at process startup and must pass them
only to the Buzz ACP process. Runner worker and test environments must remain
credential-free.

Keep operational capabilities default-deny and bind them to fixed remotes,
branch prefixes, package names/versions, and user-service labels in external
configuration. Never replace a capability handler with free-form shell input.
Privileged jobs require an owner approval whose signer, timestamp, reference,
and deterministic action summary all match the requested action.

Configure sensitive-path patterns for every repository. Runner rejects a target
commit containing a tracked match before model, test, worktree, Git, network, or
service activity, and rejects new or modified matches again at the write gate.

Report suspected vulnerabilities privately to the repository owner. Include a
minimal reproduction without real task content, identities, or secrets.

# Security

Do not commit credentials, private/public identity material copied from a real
installation, `.env` files, real configuration, SQLite databases, WAL/SHM files,
logs, task payloads or artifacts, Codex/Buzz sessions, worktrees, model manifests,
GGUF files, Ollama blobs, caches, or installed LaunchAgent plists.

Buzz private keys and authorization tags must remain in macOS Keychain. The
integration launcher may read them only at process startup and must pass them
only to the Buzz ACP process. Runner worker and test environments must remain
credential-free.

Report suspected vulnerabilities privately to the repository owner. Include a
minimal reproduction without real task content, identities, or secrets.

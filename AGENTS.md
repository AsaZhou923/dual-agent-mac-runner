# Repository instructions

- Keep Mac Runner deterministic; it must not interpret or expand product intent.
- Preserve `(job_id, attempt)` idempotency and explicit state transitions.
- Keep all real configuration and runtime state outside the repository.
- Do not add `shell=True`, implicit network access, or unrestricted command execution.
- Add regression tests for state, path, command, worktree, or credential-boundary changes.
- Run `python3 -m unittest discover -s tests -v` before committing code changes.
- Do not vendor Buzz, Codex, Ollama, Ornith weights, or third-party binaries.

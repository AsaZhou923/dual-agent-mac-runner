# Dependency baseline

The repository does not vendor these dependencies. Verify and pin versions in
the deployment environment.

| Dependency | Audited baseline | Source |
| --- | --- | --- |
| macOS | 26.6.2, arm64 | Apple |
| Python | 3.14.7 | Homebrew |
| ripgrep | 15.2.0, arm64 | Homebrew |
| Codex CLI | 0.149.0 | `@openai/codex` |
| codex-acp | 1.6.2 | `@agentclientprotocol/codex-acp` |
| Buzz ACP | 0.5.18, commit `39f8b46935736334cdd7045a4e4b5d7eb1a33888` | `block/buzz` |
| Ollama | 0.32.15 | Ollama app |
| Ornith | `ornith-ai/Ornith-1.0-9B-GGUF:Q6_K` | Hugging Face/Ollama |

Resolve required executables before accepting work. In particular, the Ornith
`rg` tool currently invokes the command name `rg`, so the controlled Runner PATH
must contain the intended executable. Do not rely on a copy bundled privately
inside another application.

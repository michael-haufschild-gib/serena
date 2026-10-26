# Serena — Project Core

Serena is an MCP server giving coding agents lightweight, language-server-based code navigation and editing,
plus memories. This is a private, macOS-only fork (never merged upstream). Serena never builds, checks or
diagnoses code: there are no diagnostics tools, no shell tool, and language servers are configured not to build
(no `cargo check`, no JDTLS autobuild, pyright type checking off).

## Source map

- `src/serena/` — agent, MCP server, tools, project/config layer
  - `cli.py` — CLI; `start-mcp-server` execs into the shared-server launcher for project sessions
  - `shared_server.py` — stdlib-only launcher: connects a session's stdio to the project's shared server
    (Unix socket in `$TMPDIR/serena-shared-servers/`), starting it under a lock if needed
  - `project_daemon.py` — the shared server: one `Project` + one `TaskExecutor` per project, one `SerenaAgent`
    per session; exits after `ls_idle_timeout` without sessions
  - `agent.py`, `mcp.py`, `project.py`, `project_server.py`, `hooks.py` — wiring
  - `ls_manager.py` — language servers start on first use, stop after `ls_idle_timeout` without use
  - `util/process_lifecycle.py` — clean shutdown on SIGTERM/SIGHUP/SIGINT and on parent exit (kqueue)
  - `tools/` — tool implementations (memory, symbol, file, workflow, query_project, config tools)
  - `config/` — `serena_config.py`, `context_mode.py`, `client_setup.py`
  - `resources/config/contexts/*.yml`, `resources/config/modes/*.yml` — context/mode definitions
  - `prompt_factory.py` + `generated/generated_prompt_factory.py` — prompts (regenerate with `scripts/gen_prompt_factory.py`)
- `src/solidlsp/` — LSP client framework; language servers under `language_servers/` for cpp (clangd), java (JDTLS),
  lean4, python (pyright via npm), rust (rust-analyzer), swift (sourcekit-lsp), toml (taplo), typescript, yaml
  - `util/subprocess_util.py` — every language server runs in its own process group, registered with a `/bin/sh`
    watcher (`ProcessGroupReaper`) that kills the groups when Serena dies, even by SIGKILL
  - `util/cache.py` — `PersistentSymbolCache`: per-file SQLite symbol caches with a small in-memory LRU, shared
    safely by concurrent processes and written immediately
- `src/interprompt/` — prompt template library
- `test/serena/`, `test/solidlsp/<lang>/` — pytest suites; per-language tests gated by pytest markers
- `test/resources/repos/<lang>/` — fixture projects used by language-server tests
- `scripts/` — utilities (prompt regen, tool overview, profiling, agno agent)
- `docs/` — Jupyter Book sources; build via `poe doc-build`

## Project-wide invariants

- macOS only: no Windows/Linux code paths; `ty` checks with `python-platform = "darwin"`.
- Removed config keys, tool names and CLI flags (dashboard, JetBrains, transports, diagnostics, activation command,
  shell tool) are still accepted and ignored, so existing configs and client launch commands keep working.
- Package name: `serena-agent`; wheel includes `serena`, `interprompt`, `solidlsp`.
- Python: `>=3.11, <3.15`. Dependencies are exact-pinned in `pyproject.toml` (uvx installs from git, lockfile ignored — pin exactly).
- Entry points: `serena` → `serena.cli:top_level`; `serena-hooks` → `serena.hooks:hook_commands`.

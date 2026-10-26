# Connecting Your MCP Client

In the following, we provide general instructions on how to connect Serena to your MCP-enabled client,
as well as specific instructions for popular clients.

(clients-general-instructions)=
## General Instructions

In general, Serena can be used with any MCP-enabled client.
To connect Serena to your favourite client, simply

1. determine how to add a custom MCP server to your client (refer to the client's documentation).
2. add a new MCP server entry by specifying a [run command](start-mcp-server) that allows the client to start the
   MCP server in stdio mode as a subprocess.

Find concrete examples for popular clients below.

Depending on your needs, you might want to further customize Serena's behaviour by
* [adding command-line arguments](mcp-args)
* [adjusting configuration](050_configuration).

**Mode of Operation**.
Note that some clients have a per-workspace MCP configuration (e.g. Claude Code),
while others have a global MCP configuration (e.g. Codex and Claude Desktop).

- In the per-workspace case, you typically want to start Serena with your workspace directory as the project directory 
  and never switch to a different project. This is achieved by specifying the
  `--project <path>` argument with a single-project [context](#contexts) (e.g. `claude-code`).
- In the global configuration case, you must first activate the project you want to work on, which you can do by asking
  the LLM to do so (e.g., "Activate the current dir as project using serena"). In such settings, the `activate_project`
  tool is required.

**Tool Selection**.
While you may be able to turn off tools through your client's interface (e.g., in Claude Desktop),
we recommend selecting your base tool set through Serena's configuration, as Serena's prompts automatically
adjust based on which tools are enabled/disabled.  
A key mechanism for this is to use the appropriate [context](#contexts) when starting Serena.

(clients-common-pitfalls)=
### Common Pitfalls

**Discoverability of the `serena` command**.
Your client may not find the `serena` CLI command, even if it is on your system PATH.
In this case, a workaround is to provide the full path to the `serena` executable.

**Serena's tools not being used**.
With some clients, you may experience that Serena's tools are not being used.
This is mainly due to problems in the client itself (like a poorly implemented tool discovery). To counteract this,
Serena comes with a set of commands that can be used in _hooks_. See the sections on hooks for Claude Code and Codex below.

**Environment Variables**.
Some language servers may require additional environment variables to be set (e.g. F# on macOS with Homebrew),
which you may need to explicitly add to the MCP server configuration.
Note that for some clients (e.g. Claude Desktop), the spawned MCP server process may not inherit environment variables that
are only configured in your shell profile (e.g. `.bashrc`, `.zshrc`, etc.); they would need to be set system-wide instead.
An easy fix is to add them explicitly to the MCP server entry.
For example, in Claude Desktop and other clients, you can simply add an `env` key to the `serena`
object, e.g.

```
"env": {
    "DOTNET_ROOT": "/opt/homebrew/Cellar/dotnet/9.0.8/libexec"
}
```

## Claude Code

Serena is a great way to make Claude Code both more efficient and more powerful!
To set up the Serena MCP server for Claude Code, you can simply run this command: 

    serena setup claude-code

Find manual setup instructions as well as workarounds for Claude Code's recent regressions pertaining to (external) tool use below.

:::{attention}
Recent updates to Claude Code (CC) and to the Opus line of models resulted in drastically reduced
adherence to instructions pertaining to Serena's tools.

After extensive analysis, we identified part of the reason to be very long and detailed
tool descriptions for built-in tools and parts of the default system prompt. 
The descriptions of CC's system tools take almost 16k tokens, cannot be adjusted by the user,
and introduce a very strong bias towards internal tools, making it almost impossible to convince Opus 4.7 to use Serena.

As a workaround, we crafted a system prompt that counteracts this bias.
When using Serena, we highly recommend that you start CC as 

```shell
claude --system-prompt="$(serena prompts print-cc-system-prompt-override)"
```

You can also consider adding the content of `serena cc-system-prompt-override` to your `CLAUDE.md` files,
but the effect be insufficient for counteracting Claude Code's bias towards internal tools.
:::

**Global Configuration**. To add the Serena MCP server for all your projects, use the user-level configuration of claude code and the `--project-from-cwd` flag:

```shell
claude mcp add --scope user serena -- serena start-mcp-server --context claude-code --project-from-cwd
```

**Per-Project Configuration.** Alternatively, to add Serena only for the current project in the current directory, 
use the command:

```shell
claude mcp add serena -- serena start-mcp-server --context claude-code --project "$(pwd)"
```

**Verification.**
Confirm that Claude Code is connected to Serena by running the `/mcp` command and by reconnecting, if necessary.
If Serena fails to start fast enough, you should set `MCP_TIMEOUT` to a sufficiently high value
(e.g. by adding `export MCP_TIMEOUT=60000` to your shell profile)

**Hooks.**
Due to recent changes (especially dynamic tool loading) in Claude Code, the agent will often fail to make proper use
of Serena's tools, either by failing to load them in the beginning or by forgetting the instructions in a long session
(a behavior known as agent drift). To counteract this, we provide reminder hooks. We **strongly recommend** setting
up the hooks as below (or a variation thereof) for optimal performance of Serena in Claude Code.

:::{note}
While recommended, hooks are an **alpha feature**. Provide feedback via the [GitHub issue tracker](https://github.com/oraios/serena/issues) if you encounter any issues.
:::

All hooks below are opt-in — include only the ones you want. Add the following to your
Claude Code settings file (`.claude/settings.json` in your project directory, or
`~/.claude/settings.json` globally):

```json
{
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks remind --client=claude-code"
                    }
                ]
            },
            {
                "matcher": "mcp__serena__*",
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks auto-approve --client=claude-code"
                    }
                ]
            }
        ],
        "SessionStart": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks activate --client=claude-code"
                    }
                ]
            }
        ],
        "SessionEnd": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks cleanup --client=claude-code"
                    }
                ]
            }
        ]
    }
}
```

The hooks will:

- **`remind`**: Nudge the agent to use Serena's symbolic tools when it makes too many consecutive
  `grep` or `read_file` calls without using any Serena tools in between.
- **`activate`**: Prompt the agent to activate the project and read Serena's instructions at session start.
- **`cleanup`**: Clean up hook session data when the session ends.
- **`auto-approve`**: Auto-approve Serena tool calls whenever Claude Code is in a permissive
  permission mode (`acceptEdits` or `auto`), so blanket approvals cover Serena's destructive
  tools (e.g. `replace_symbol_body`, `rename_symbol`) instead of prompting on every call.

For more details on Claude Code's hook system, see the
[Claude Code hooks documentation](https://code.claude.com/docs/en/hooks).

## Codex (CLI and App)

You can simply run `serena setup codex`.

Alternatively, you can manually add the following to `~/.codex/config.toml` (create the file if it does not exist):

```toml
[mcp_servers.serena]
startup_timeout_sec = 15
command = "serena"
args = ["start-mcp-server", "--project-from-cwd", "--context=codex"]
```

**Verification.**
Run the `/mcp` command and verify that Serena is connected.
The Codex app does not start a session in the project's directory, so when using the app, we recommend
asking Codex to "Activate the current dir as project using serena" at the start of each session (though Codex might
do this automatically).

**Hooks.**
Codex supports lifecycle hooks; see the
[Codex hooks documentation](https://developers.openai.com/codex/hooks) for details. To enable
Serena's hooks for Codex, add this feature flag to `~/.codex/config.toml`:

```toml
[features]
codex_hooks = true
```

Then create `~/.codex/hooks.json` with the following content:

```json
{
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks remind --client=codex"
                    }
                ]
            }
        ],
        "SessionStart": [
            {
                "matcher": "startup|resume",
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks activate --client=codex"
                    }
                ]
            }
        ],
        "SessionEnd": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "serena-hooks cleanup --client=codex"
                    }
                ]
            }
        ]
    }
}
```

The `SessionEnd` cleanup hook requires Codex 0.145.0 or newer. Older Codex versions only support
`Stop` for cleanup, which currently has a [known compatibility issue](https://github.com/oraios/serena/issues/1533).
If you still configure it, replace `SessionEnd` with `Stop` in the example above. Configure cleanup
under exactly one of these events, never both: `Stop` runs after every turn, while `SessionEnd` runs
when Codex tears down the root thread.

The hooks will:

- **`activate`**: Prompt the agent to activate the current project and read Serena's instructions
  when a Codex session starts or resumes.
- **`remind`**: Nudge the agent to use Serena's symbolic tools when it makes too many consecutive
  code-search or code-file-read calls without using Serena tools in between.
- **`cleanup`**: Clean up hook session data when the session ends.

The `PreToolUse` matcher is intentionally restricted to `Bash`. The Serena reminder hook for Codex
tracks shell-based grep and code-file reads, so running it for every tool call is unnecessary.

## Claude Desktop

Install the official [Claude Desktop application by Anthropic](https://claude.ai/download).

To configure MCP server settings, go to Claude / Settings / Developer / Edit Config,
which will let you open the JSON file `claude_desktop_config.json`
(located at `~/Library/Application Support/Claude/claude_desktop_config.json`).

Add the `serena` MCP server configuration

```json
{
  "mcpServers": {
    "serena": {
      "command": "serena",
      "args": [
        "start-mcp-server",
        "--context=desktop-app"
      ]
    }
  }
}
```

If your language server requires specific environment variables to be set (e.g. F# on macOS with Homebrew),
you can add them via an `env` key (see [above](#clients-common-pitfalls)).

**Verification.**
Once you have created the new MCP server entry, save the config and then restart Claude Desktop.

:::{attention}
Be sure to fully quit the Claude Desktop application via Claude / Quit Claude (Cmd+Q), as merely closing the window
will keep the application running.
:::

After restarting, you should see Serena's tools in your chat interface (notice the small hammer icon).

## Antigravity

Add this configuration:

```json
{
  "mcpServers": {
    "serena": {
      "command": "serena",
      "args": [
        "start-mcp-server",
        "--context=antigravity"
      ]
    }
  }
}
```

You will have to prompt Antigravity's agent to "Activate the current project using serena's activation tool" after starting Antigravity in the project directory (once in the first chat enough, all other chat sessions will continue using the same Serena session).

Antigravity does not currently support including the working directory in the MCP configuration.

## Other Clients

For other clients, follow the [general instructions](#clients-general-instructions) above to set up Serena as an MCP server.

### Terminal-Based Clients

There are many terminal-based coding assistants that support MCP servers, such as

 * [Gemini-CLI](https://github.com/google-gemini/gemini-cli), 
 * [Qwen3-Coder](https://github.com/QwenLM/Qwen3-Coder),
 * [rovodev](https://community.atlassian.com/forums/Rovo-for-Software-Teams-Beta/Introducing-Rovo-Dev-CLI-AI-Powered-Development-in-your-terminal/ba-p/3043623),
 * [OpenHands CLI](https://docs.all-hands.dev/usage/how-to/cli-mode) and
 * [opencode](https://github.com/sst/opencode).

They generally benefit from the symbolic tools provided by Serena. You might want to customize some aspects of Serena
by writing your own context, modes or prompts to adjust it to the client's respective internal capabilities (and your general workflow).
In particular, a custom context which excludes the tools that overlap with the client's internal tools reduces tool duplication.

### Local GUIs and Agent Frameworks

Over the last months, several technologies have emerged that allow you to run a local GUI client
and connect it to an MCP server. The respective applications will typically work with Serena out of the box.
Some of the leading open source GUI applications are

  * [Jan](https://jan.ai/docs/mcp), 
  * [OpenHands](https://github.com/All-Hands-AI/OpenHands/),
  * [OpenWebUI](https://docs.openwebui.com/openapi-servers/mcp) and 
  * [Agno](https://docs.agno.com/introduction/playground).

These applications allow combining Serena with almost any LLM (including locally running ones) 
and offer various other integrations.

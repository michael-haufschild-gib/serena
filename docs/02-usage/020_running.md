# Running Serena

Serena is a command-line tool with a variety of sub-commands.
This section describes
 * how to run Serena in general
 * how to run and configure the most important command, i.e. starting the MCP server
 * other useful commands.

The main way to run Serena is to use the [installed version](install-serena),
which should be available in your system PATH as `serena.`

In general, to get help, append `--help` to the command, i.e.

    serena --help
    serena <command> --help


(start-mcp-server)=
## Running the MCP Server

Given your preferred method of running Serena, you can start the MCP server using the `start-mcp-server` command:

    serena start-mcp-server [options]  

### Standard I/O Mode

The typical usage involves the client (e.g. Claude Code or Codex) running
the MCP server as a subprocess and using the process' stdin/stdout streams to communicate with it.
In order to launch the server, the client thus needs to be provided with the command to run the MCP server.

:::{note}
MCP servers which use stdio as a protocol are somewhat unusual as far as client/server architectures go, as the server
necessarily has to be started by the client in order for communication to take place via the server's standard input/output streams.
In other words, you do not need to start the server yourself. The client application (e.g. Claude Desktop) takes care of this and
therefore needs to be configured with a launch command.
:::

Communication over stdio is the only transport supported by the Serena MCP server, so in the simplest
case, you can simply run the `start-mcp-server` command without any additional options.
 
    serena start-mcp-server

See the section ["Configuring Your MCP Client"](030_clients) for specific information on how to configure your MCP client (e.g. Claude Code, Codex, Claude Desktop, etc.)
to use such a launch command.

Note that Serena is a stateful MCP server, and only one coding project can be active per session.
See section [The Project Workflow](040_workflow) for more information on how to manage projects in Serena.

(shared-server)=
### Sessions Share One Server per Project

When a session is started for a project (`--project` or `--project-from-cwd`), the `start-mcp-server` process
hands the session over to the project's *shared server* and turns into a small relay between the client and that
server. All sessions on the same project (e.g. several chats) are served by the same server, so they share one
set of language servers. Each session keeps its own context, modes and tools. The shared server is started by the
first session and exits once it has had no session for `ls_idle_timeout` seconds (see the
[configuration](050_configuration)); its log is written to `~/.serena/logs/<date>/shared-server_*.txt`.

Tool calls of all sessions on a project are executed one at a time, so a long-running call in one session delays
the calls of the others.

Pass `--no-shared-server` to serve a session in its own process instead; sessions without a project are always
served in their own process.

Language servers are started when a tool first needs them and are stopped after `ls_idle_timeout` seconds without
use. Serena never leaves language servers behind: they are terminated when Serena exits, including when it is
killed, and a session's process exits when its client goes away.

(mcp-args)=
### MCP Server Command-Line Arguments

The Serena MCP server supports a wide range of additional command-line options.
Use the command

    <serena> start-mcp-server --help

to get a list of all available options.

Some useful options include:

  * `--project <path|name>`: specify the project to work on by name or path.
  * `--project-from-cwd`: auto-detect the project from current working directory     
    (walking up the parent directories and activating the nearest one that contains either `.serena/project.yml`
    or `.git`, if any). The nearest boundary wins, so a git worktree nested under another Serena project resolves
    to the worktree itself rather than the ancestor project.
    This option is intended for CLI-based agents like Claude Code, Gemini and Codex, which are typically started from within the project directory
    and which do not change directories during their operation.
  * `--context <context>`: specify the operation [context](contexts) in which Serena shall operate
  * `--mode <mode>`: specify one or more [modes](modes) to enable (can be passed several times)
  * `--no-shared-server`: serve the session in this process instead of the project's [shared server](shared-server)

## Other Commands

Serena provides several other commands in addition to `start-mcp-server`, 
most of which are related to project setup and configuration.

To get a list of available commands, run:

    <serena> --help

To get help on a specific command, run:

    <serena> <command> --help

In general, add `--help` to any command or sub-command to get information about its usage and available options.

Here are some examples of commands you might find useful:

```bash
# get help about a sub-command
serena> tools list --help

# list all available tools
serena> tools list --all

# get detailed description of a specific tool
serena> tools description find_symbol

# creating a new Serena project in the current directory 
serena project create

# creating and immediately indexing a project
serena project create --index

# indexing the project in the current directory (auto-creates if needed)
serena project index

# run a health check on the project in the current directory
serena project health-check

# check if a path is ignored by the project
serena project is_ignored_path path/to/check

# edit Serena's configuration file
serena config edit

# list available contexts
serena context list

# create a new context
serena context create my-custom-context

# edit a custom context
serena context edit my-custom-context

# list available modes
serena mode list

# create a new mode
serena mode create my-custom-mode

# edit a custom mode
serena mode edit my-custom-mode

# list available prompt definitions
serena prompts list

# create an override for internal prompts
serena prompts create-override prompt-name

# edit a prompt override
serena prompts edit-override prompt-name
```

Explore the full set of commands and options using the CLI itself!


## Alternative Ways of Running Serena

Depending on your requirements, you may want to run Serena in different ways.
When applying one of these approaches, replace `serena` in commands mentioned throughout the documentation
with the respective command and options. The same applies to `serena-hooks` commands.

### Using uvx to Run the Latest Source Version
    
`uvx` is part of `uv`. It can be used to run the latest version of Serena directly from the repository, without an explicit local installation.

    uvx -p 3.13 --from git+https://github.com/oraios/serena serena 

This was previously the main way of running Serena.
Since this has the downside that every new commit in the repository will trigger a (potentially slow) re-synchronization, an [installation](010_installation) of Serena should usually be preferred.
If you should experience timeouts when connecting the MCP server, consider switching.  
If, however, the synchronisation is fast enough for you, this is still a good option.

### Running from Cloned Source

1. Clone the repository and change into it.

   ```shell
   git clone https://github.com/oraios/serena
   cd serena
   ```

2. Run Serena via

   ```shell
   uv run serena 
   ```

   when within the serena installation directory.     
   From other directories, run it with the `--directory` option, i.e.

   ```shell
    uv run --directory /abs/path/to/serena serena
    ```

:::{note}
Adding the `--directory` option results in the working directory being set to the Serena directory.
As a consequence, you will need to specify paths when using CLI commands that would otherwise operate on the current directory.
:::

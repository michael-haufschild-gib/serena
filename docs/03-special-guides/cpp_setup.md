# C/C++ Setup Guide

This guide explains how to prepare a C/C++ project so that Serena can provide reliable code intelligence via the clangd language server.

---

## General

Serena uses the clangd language server for C/C++, which requires a properly configured `compile_commands.json` 
for cross-file reference finding, see below for details.

Your project must have a `compile_commands.json` file at the repository root. 
This file is essential for correct parsing and cross-file reference finding.

You can use a specific clangd installation (e.g., a custom build or a version provided by your project),
by specifying the path in your configuration, see [ls-specific-settings](ls-specific-settings) for details.

## compile_commands.json Requirements

For reliable cross-file reference finding with clangd, your `compile_commands.json` must:

1. **Include proper C++ standard flags** (e.g., `-std=c++17`)
2. **Include all necessary include paths** (`-I` flags)

---

### With clangd

By default, Serena automatically downloads and manages clangd. Since clangd does not properly work with relative paths in `compile_commands.json`,
Serena will detect them and transform them into absolute paths automatically (writing a new `compile_commands.json` file), if needed.

#### Customizing the Compilation Database Location

By default, Serena creates the transformed compilation database at `.serena/compile_commands.json`. 
You can customize this location via project settings:

```yaml
# .serena/project.yml
language_servers:
  cpp:
    compile_commands_dir: custom/rel/path (defaults to .serena)
```

---

## Known Limitations

### Files Created After Server Initialization

clangd has a fundamental limitation: 
**files created by external mechanisms after the language server starts are not automatically indexed**.

Cross-file references to newly created files will not work unless the new file is at some point opened by the language server (for example, by a symbol lookup in it), or until `compile_commands.json` is updated and 
the language server is restarted.

---

## Reference

- Clangd official documentation: https://clangd.llvm.org/
- Clangd project setup: https://clangd.llvm.org/installation#project-setup

## Unreal Engine projects

For Unreal Engine 5 projects (reflection macros, UnrealBuildTool), clangd needs a compilation database.
Build the editor target once, then generate the database with UnrealBuildTool:

```
<Engine>/Build/BatchFiles/Mac/Build.sh <Proj>Editor Mac Development -project="<Proj>.uproject" -mode=GenerateClangDatabase -OutputDir="<projectDir>"
```

Serena ignores the engine's build and cache directories (`Binaries`, `DerivedDataCache`, `Intermediate`, `Saved`)
in Unreal Engine projects.

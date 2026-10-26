# Language Support

Serena provides a set of versatile code querying and editing functionalities
based on symbolic understanding of the code across a wide range of programming languages.
Equipped with these capabilities, Serena discovers and edits code just like a seasoned developer
making use of an IDE's capabilities would.
Serena can efficiently find the right context and do the right thing even in very large and
complex projects!

These capabilities are powered by language servers implementing the language server protocol (LSP).
See the [Features](025_features) section for an overview of the capabilities.

(language-servers)=
## Language Servers

Serena incorporates a powerful abstraction layer for the integration of language servers 
that implement the language server protocol (LSP).
It even supports multiple language servers in parallel to support polyglot projects.

The language servers themselves are typically open-source projects (like Serena)
or at least freely available for use.

We provide support for the programming languages listed below.
Some languages require additional installations or setup steps, as noted.

* **C/C++/Objective-C**  
  (language `cpp`; uses clangd, which is downloaded automatically;
  for best results, provide a `compile_commands.json` at the repository root;
  see the [C/C++ Setup Guide](../03-special-guides/cpp_setup) for details)
* **Java**  
  (language `java`; uses Eclipse JDTLS, which is downloaded automatically together with a JRE)
* **JavaScript/TypeScript**  
  (language `typescript`; requires Node.js and npm; typescript-language-server is installed automatically)
* **Lean 4**  
  (language `lean4`; requires the Lean toolchain (`lean`, `lake`) on PATH)
* **Python**  
  (language `python`; uses pyright, which is installed automatically from npm; requires Node.js and npm)
* **Rust**  
  (language `rust`; requires rust-analyzer, e.g. via `rustup component add rust-analyzer`)
* **Swift**  
  (language `swift`; uses sourcekit-lsp from the Xcode/Swift toolchain)
* **TOML**  
  (language `toml`, experimental; uses Taplo, which is downloaded automatically)
* **YAML**  
  (language `yaml`, experimental; requires Node.js and npm; yaml-language-server is installed automatically)

Serena uses language servers for code navigation and editing only: it never builds, checks or
diagnoses code, and it configures the language servers accordingly (e.g. no `cargo check`,
no Java workspace builds, no type checking by pyright).

Support for further languages can easily be added by providing a shallow adapter for a new language server implementation,
see Serena's [memory on that](https://github.com/oraios/serena/blob/main/.serena/memories/adding_new_language_support_guide.md).

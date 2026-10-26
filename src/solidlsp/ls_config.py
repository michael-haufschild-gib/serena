"""
Configuration objects for language servers
"""

import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from solidlsp import SolidLanguageServer

log = logging.getLogger(__name__)


class FilenameMatcher:
    def __init__(self, *file_extensions: str, case_sensitive: bool = True) -> None:
        """
        :param file_extensions: file extensions, e.g., `.py, .yml`
        :param case_sensitive: whether the file extensions are case-sensitive.
        """
        self._file_extensions = list(set(file_extensions)) if case_sensitive else list(set(ext.lower() for ext in file_extensions))
        self._case_sensitive = case_sensitive

    @property
    def file_extensions(self) -> list[str]:
        """The file extensions registered with this matcher. Returned as a copy."""
        return list(self._file_extensions)

    def is_relevant_filename(self, fn: str) -> bool:
        if not self._case_sensitive:
            fn = fn.lower()
        for ext in self._file_extensions:
            if fn.endswith(ext):
                return True
        return False

    def string_contains_relevant_filename(self, string: str) -> bool:
        """:return: whether ``string`` contains an occurrence of any registered extension as
        a *complete* extension — i.e. the extension must either end the string or be followed
        by a non-extension-character (anything other than a letter, digit, or underscore).
        """
        if not self._case_sensitive:
            string = string.lower()
        for ext in self._file_extensions:
            if re.search(rf"{re.escape(ext)}(?:\W|$)", string):
                return True
        return False


class LanguageServerId(str, Enum):
    """
    Enumeration of language servers supported by SolidLSP.
    """

    PYTHON = "python"
    RUST = "rust"
    JAVA = "java"
    TYPESCRIPT = "typescript"
    CPP = "cpp"
    SWIFT = "swift"
    LEAN4 = "lean4"
    YAML = "yaml"
    """YAML language server (experimental).
    Must be explicitly specified as the main language, not auto-detected.
    """
    TOML = "toml"
    """TOML language server using Taplo.
    Supports TOML validation, formatting, and schema support.
    """

    @classmethod
    def iter_all(cls, include_experimental: bool = True, include_non_programming_languages: bool = True) -> Iterable[Self]:
        for lang in cls:
            if include_experimental or not lang.is_experimental():
                if include_non_programming_languages or lang.is_programming_language():
                    yield lang

    def is_experimental(self) -> bool:
        """
        Check if the language server is experimental (potentially not robust),
        secondary (not default for respective language) or deprecated.
        """
        return self in {self.YAML, self.TOML}

    def is_programming_language(self) -> bool:
        """Whether the supported language should be considered a programming language.
        Solidlsp supports data languages like YAML or TOML, this method returns False for them.
        """
        return self not in frozenset((self.TOML, self.YAML))

    def __str__(self) -> str:
        return self.value

    def get_priority(self) -> int:
        """
        :return: priority of the language for breaking ties between languages; higher is more important.
        """
        # experimental languages have the lowest priority
        if self.is_experimental():
            return 0
        return 2

    def supports_implementation_request(self) -> bool:
        """
        Return whether the default language server for this language supports ``textDocument/implementation``.
        """
        return self.get_ls_class().supports_implementation_request()

    @cache
    def get_source_fn_matcher(self) -> FilenameMatcher:
        match self:
            case self.PYTHON:
                return FilenameMatcher(".py", ".pyi")
            case self.JAVA:
                return FilenameMatcher(".java")
            case self.TYPESCRIPT:
                # see https://github.com/oraios/serena/issues/204
                path_patterns = []
                for prefix in ["c", "m", ""]:
                    for postfix in ["x", ""]:
                        for base_pattern in ["ts", "js"]:
                            path_patterns.append(f".{prefix}{base_pattern}{postfix}")
                return FilenameMatcher(*path_patterns)
            case self.RUST:
                return FilenameMatcher(".rs")
            case self.CPP:
                # From llvm-project/clang/lib/Driver/Types.cpp types::lookupTypeForExtension:
                return FilenameMatcher(
                    # C
                    ".c",
                    ".h",
                    # C++
                    ".c++",
                    ".cc",
                    ".cp",
                    ".cpp",
                    ".cxx",
                    ".hh",
                    ".hpp",
                    ".hxx",
                    # C++ include files
                    ".inl",
                    ".ipp",
                    ".tpp",
                    ".txx",
                    # Objective-C
                    ".m",
                    ".mm",
                    # C++20 module interface files
                    ".c++m",
                    ".cppm",
                    ".cxxm",
                    ".ixx",
                    # CUDA
                    ".cu",
                    # HIP
                    ".hip",
                    # OpenCL
                    ".cl",
                    ".clcpp",
                    # Arduino sketch: not in clang's extension table, but it is C++.
                    # Routing it here lets clangd serve symbols; the project must
                    # tell clangd it is C++ (a .clangd with CompileFlags Add: [-xc++],
                    # or a compile DB), since the clang driver can't infer a job from .ino.
                    ".ino",
                    case_sensitive=False,
                )
            case self.SWIFT:
                return FilenameMatcher(".swift")
            case self.YAML:
                return FilenameMatcher(".yaml", ".yml")
            case self.TOML:
                return FilenameMatcher(".toml")
            case self.LEAN4:
                return FilenameMatcher(".lean")
            case _:
                raise ValueError(f"Unhandled language: {self}")

    def get_ls_class(self) -> type["SolidLanguageServer"]:
        match self:
            case self.PYTHON:
                from solidlsp.language_servers.pyright_server import PyrightServer

                return PyrightServer
            case self.JAVA:
                from solidlsp.language_servers.eclipse_jdtls import EclipseJDTLS

                return EclipseJDTLS
            case self.RUST:
                from solidlsp.language_servers.rust_analyzer import RustAnalyzer

                return RustAnalyzer
            case self.TYPESCRIPT:
                from solidlsp.language_servers.typescript_language_server import TypeScriptLanguageServer

                return TypeScriptLanguageServer
            case self.CPP:
                from solidlsp.language_servers.clangd_language_server import ClangdLanguageServer

                return ClangdLanguageServer
            case self.SWIFT:
                from solidlsp.language_servers.sourcekit_lsp import SourceKitLSP

                return SourceKitLSP
            case self.YAML:
                from solidlsp.language_servers.yaml_language_server import YamlLanguageServer

                return YamlLanguageServer
            case self.TOML:
                from solidlsp.language_servers.taplo_server import TaploServer

                return TaploServer
            case self.LEAN4:
                from solidlsp.language_servers.lean4_language_server import Lean4LanguageServer

                return Lean4LanguageServer
            case _:
                raise ValueError(f"Unhandled language: {self}")

    @classmethod
    def from_ls_class(cls, ls_class: type["SolidLanguageServer"]) -> Self:
        """
        Get the Language enum value from a SolidLanguageServer class.

        :param ls_class: The SolidLanguageServer class to find the corresponding Language for
        :return: The Language enum value
        :raises ValueError: If the language server class is not supported
        """
        for enum_instance in cls:
            if enum_instance.get_ls_class() == ls_class:
                return enum_instance
        raise ValueError(f"Unhandled language server class: {ls_class}")


@dataclass(frozen=True)
class LanguageServerConfig:
    """
    Configuration parameters for a language server instance
    """

    ls_id: LanguageServerId
    """
    defines the language server to use
    """
    workspace_folders: list[str] = field(default_factory=lambda: ["."])
    """
    list of workspace folders to be used by the language server and to be fully indexed by SolidLSP.
    Paths can either be absolute or relative to the project root.
    These folders must be descendants of the project root.
    """
    additional_workspace_folders: list[str] = field(default_factory=list)
    """
    list of additional workspace folders to be passed to the language server, but which are not to be indexed by SolidLSP. 
    Paths can either be absolute or relative to the project root.
    These folders can potentially be outside of the project root, e.g. for cross-package reference support.
    """
    trace_lsp_communication: bool = False
    ignored_paths: list[str] = field(default_factory=list)
    """
    list of ordered ignore patterns (same syntax as .gitignore; only forward slashes) to be used by the language server
    for filtering out files and folders from indexing and analysis.
    """
    encoding: str = "utf-8"
    """File encoding to use when reading source files"""

    @classmethod
    def from_dict(cls, env: dict) -> Self:
        import inspect

        return cls(**{k: v for k, v in env.items() if k in inspect.signature(cls).parameters})

    @staticmethod
    def _absolute_workspace_folders(folders: list[str], project_root: str) -> list[str]:
        abs_workspace_folders = []
        for path in folders:
            if os.path.isabs(path):
                abs_path = str(Path(path).resolve())
            else:
                abs_path = os.path.realpath(os.path.join(project_root, path))
            if not os.path.exists(abs_path):
                log.error("Workspace folder does not exist: %s; skipping", abs_path)
                continue
            if abs_path in abs_workspace_folders:
                log.warning("Duplicate workspace folder: %s; skipping", abs_path)
                continue
            abs_workspace_folders.append(abs_path)
        return abs_workspace_folders

    def get_absolute_workspace_folders(self, project_root: str) -> list[str]:
        """
        Get the absolute paths of the workspace folders, resolving relative paths against the project root.

        :param project_root: The root path of the project
        :return: List of absolute workspace folder paths
        """
        return self._absolute_workspace_folders(self.workspace_folders, project_root)

    def get_absolute_additional_workspace_folders(self, project_root: str) -> list[str]:
        """
        Get the absolute paths of the additional workspace folders, resolving relative paths against the project root.

        :param project_root: The root path of the project
        :return: List of absolute additional workspace folder paths
        """
        return self._absolute_workspace_folders(self.additional_workspace_folders, project_root)

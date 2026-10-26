"""
The Serena Model Context Protocol (MCP) Server
"""

import json
import os
import platform
import signal
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from logging import Logger
from typing import TypeVar

import requests
from sensai.util import logging
from sensai.util.helper import mark_used
from sensai.util.string import dict_string

from interprompt.jinja_template import JinjaTemplate
from serena import serena_version
from serena.config.context_mode import SerenaAgentContext, SerenaAgentMode
from serena.config.serena_config import (
    ModeSelectionDefinition,
    ModeSelectionDefinitionWithAddedModes,
    ModeSelectionDefinitionWithBaseModes,
    NamedToolInclusionDefinition,
    SerenaConfig,
    SerenaPaths,
    ToolInclusionDefinition,
)
from serena.ls_manager import LanguageServerManager
from serena.memories.memory_manager import MemoryManager
from serena.project import Project
from serena.prompt_factory import SerenaPromptFactory
from serena.task_executor import TaskExecutor
from serena.tools import (
    ActivateProjectTool,
    GetCurrentConfigTool,
    OnboardingTool,
    ReadMemoryTool,
    ReplaceContentTool,
    Tool,
    ToolMarker,
    ToolRegistry,
)
from serena.util.inspection import iter_subclasses
from solidlsp.ls_config import LanguageServerId

log = logging.getLogger(__name__)
TTool = TypeVar("TTool", bound="Tool")
T = TypeVar("T")
SUCCESS_RESULT = "OK"


class ProjectNotFoundError(Exception):
    pass


class AvailableTools:
    """
    Represents the set of available/exposed tools of a SerenaAgent.
    """

    def __init__(self, tools: list[Tool]):
        """
        :param tools: the list of available tools
        """
        self.tools = tools
        self.tool_names = sorted([tool.get_name_from_cls() for tool in tools])
        """
        the list of available tool names, sorted alphabetically
        """
        self._tool_name_set = set(self.tool_names)
        self.tool_marker_names = set()
        for marker_class in iter_subclasses(ToolMarker):
            for tool in tools:
                if isinstance(tool, marker_class):
                    self.tool_marker_names.add(marker_class.__name__)

    def __len__(self) -> int:
        return len(self.tools)

    def contains_tool_name(self, tool_name: str) -> bool:
        return tool_name in self._tool_name_set

    def contains_tool_class(self, tool_class: type[Tool]) -> bool:
        return self.contains_tool_name(tool_class.get_name_from_cls())


class ToolSet:
    """
    Represents a set of tools by their names.
    """

    LEGACY_TOOL_NAME_MAPPING = {"replace_regex": ReplaceContentTool.get_name_from_cls()}
    """
    maps legacy tool names to their new names for backward compatibility
    """

    def __init__(self, tool_names: set[str]) -> None:
        self._tool_names = tool_names

    def __len__(self) -> int:
        return len(self._tool_names)

    @classmethod
    def default(cls) -> "ToolSet":
        """
        :return: the default tool set, which contains all tools that are enabled by default
        """
        from serena.tools import ToolRegistry

        return cls(set(ToolRegistry().get_tool_names_default_enabled()))

    def apply(self, *tool_inclusion_definitions: "ToolInclusionDefinition") -> "ToolSet":
        """
        Applies one or more tool inclusion definitions to this tool set,
        resulting in a new tool set.

        :param tool_inclusion_definitions: the definitions to apply
        :return: a new tool set with the definitions applied
        """
        from serena.tools import ToolRegistry

        def get_updated_tool_name(tool_name: str) -> str:
            """Retrieves the updated tool name if the provided tool name is deprecated, logging a warning."""
            if tool_name in self.LEGACY_TOOL_NAME_MAPPING:
                new_tool_name = self.LEGACY_TOOL_NAME_MAPPING[tool_name]
                log.warning("Tool name '%s' is deprecated, please use '%s' instead", tool_name, new_tool_name)
                return new_tool_name
            return tool_name

        registry = ToolRegistry()
        tool_names = set(self._tool_names)
        for definition in tool_inclusion_definitions:
            if definition.is_fixed_tool_set():
                tool_names = set()
                for fixed_tool in definition.fixed_tools:
                    fixed_tool = get_updated_tool_name(fixed_tool)
                    if registry.check_valid_tool_name(fixed_tool, " (in fixed tools set)"):
                        tool_names.add(fixed_tool)
                log.info(f"{definition} defined a fixed tool set with {len(tool_names)} tools: {', '.join(tool_names)}")
            else:
                included_tools = []
                excluded_tools = []
                for included_tool in definition.included_optional_tools:
                    included_tool = get_updated_tool_name(included_tool)
                    if registry.check_valid_tool_name(included_tool, " (in included optional tools)") and included_tool not in tool_names:
                        tool_names.add(included_tool)
                        included_tools.append(included_tool)
                for excluded_tool in definition.excluded_tools:
                    excluded_tool = get_updated_tool_name(excluded_tool)
                    registry.check_valid_tool_name(excluded_tool, " (in excluded tools)")
                    if excluded_tool in tool_names:
                        tool_names.remove(excluded_tool)
                        excluded_tools.append(excluded_tool)
                if included_tools:
                    log.info(f"{definition} included {len(included_tools)} tools: {', '.join(included_tools)}")
                if excluded_tools:
                    log.info(f"{definition} excluded {len(excluded_tools)} tools: {', '.join(excluded_tools)}")
        return ToolSet(tool_names)

    def without_editing_tools(self) -> "ToolSet":
        """
        :return: a new tool set that excludes all tools that can edit
        """
        from serena.tools import ToolRegistry

        registry = ToolRegistry()
        tool_names = set(self._tool_names)
        for tool_name in self._tool_names:
            if registry.get_tool_class_by_name(tool_name).can_edit():
                tool_names.remove(tool_name)
        return ToolSet(tool_names)

    def get_tool_names(self) -> set[str]:
        """
        Returns the names of the tools that are currently included in the tool set.
        """
        return self._tool_names

    def includes_name(self, tool_name: str) -> bool:
        return tool_name in self._tool_names

    def to_available_tools(self, all_tools: dict[type[Tool], Tool]) -> AvailableTools:
        return AvailableTools([t for t in all_tools.values() if self.includes_name(t.get_name())])


class ActiveModes:
    _mode_instances: dict[str, SerenaAgentMode] = {}

    def __init__(self) -> None:
        self._configured_base_modes: Sequence[str] | None = None
        self._configured_default_modes: Sequence[str] | None = None
        self._added_modes: set[str] = set()
        self._dynamically_activated_mode_names: set[str] = set()
        """
        the subset of active mode names that are dynamically activated (not necessarily enabled after project change)
        """
        self._active_mode_names: Sequence[str] = []
        """
        the full list of active mode names
        """

    def apply(self, mode_selection: ModeSelectionDefinition) -> None:
        log.debug("Applying mode selection definition %s", mode_selection)

        # apply overrides
        if isinstance(mode_selection, ModeSelectionDefinitionWithBaseModes):
            if mode_selection.base_modes is not None:
                self._configured_base_modes = mode_selection.base_modes
        if mode_selection.default_modes is not None:
            self._configured_default_modes = mode_selection.default_modes
        log.debug("Current mode selection: base_modes=%s, default_modes=%s", self._configured_base_modes, self._configured_default_modes)

        # apply added modes (if any)
        if isinstance(mode_selection, ModeSelectionDefinitionWithAddedModes):
            if mode_selection.added_modes:
                log.debug("Adding modes: %s", mode_selection.added_modes)
                self._added_modes.update(mode_selection.added_modes)
                log.debug("Current added modes: %s", self._added_modes)

        self._dynamically_activated_mode_names = set(self._configured_default_modes or []) | self._added_modes
        self._active_mode_names = sorted(set(self._configured_base_modes or []) | self._dynamically_activated_mode_names)

    def get_mode_names(self) -> Sequence[str]:
        """
        :return: the ordered list of active mode names
        """
        return self._active_mode_names

    @classmethod
    def get_mode_instance(cls, mode_name: str) -> SerenaAgentMode:
        if mode_name not in cls._mode_instances:
            cls._mode_instances[mode_name] = SerenaAgentMode.load(mode_name)
        return cls._mode_instances[mode_name]

    def get_modes(self) -> Sequence[SerenaAgentMode]:
        return [self.get_mode_instance(mode_name) for mode_name in self._active_mode_names]

    def get_dynamically_activated_modes(self) -> Sequence[SerenaAgentMode]:
        return [self.get_mode_instance(mode_name) for mode_name in self._dynamically_activated_mode_names]

    def get_base_modes(self) -> Sequence[SerenaAgentMode]:
        return [self.get_mode_instance(mode_name) for mode_name in self._configured_base_modes or []]


class ProjectPromptProvisionStatus:
    """
    Manages the status of the provision of project-specific prompts
    """

    @dataclass
    class SessionStatus:
        mode_prompts_provided: bool = False
        project_activation_message_provided: bool = False

    def __init__(self, newly_activated_mode_names: set[str] | None = None):
        """
        :param newly_activated_mode_names: list of mode names that have been newly activated (by dynamic project activation)
            and for which prompts must still be provided (either in the system prompt or via the activation message)
        """
        if newly_activated_mode_names is None:
            newly_activated_mode_names = set()
        self._newly_activated_mode_names: set[str] = newly_activated_mode_names
        self._session_status_dict: dict[str, ProjectPromptProvisionStatus.SessionStatus] = defaultdict(lambda: self.SessionStatus())

    def _get_session_status(self, session_id: str) -> SessionStatus:
        return self._session_status_dict[session_id]

    def is_mode_prompt_already_provided(self, mode_name: str, session_id: str) -> bool:
        """
        :param mode_name: the mode name
        :param session_id: the client session ID
        :return: whether the mode name was already provided (in a project-specific activation message) and therefore
            should not be included again (in the Serena instructions manual)
        """
        if not self._get_session_status(session_id).mode_prompts_provided:
            return False
        return mode_name in self._newly_activated_mode_names

    def get_modes_with_prompts_to_be_provided_for_project_activation(self, session_id: str) -> list[SerenaAgentMode]:
        """
        Gets the modes that have been newly activated and for which prompts still need to be provided
        (in dynamic project activation message).

        :param: session_id: the client session ID
        :return: the modes
        """
        result = []

        # Note: We always want to provide the prompts of newly activated modes in the activation message
        #   because some clients (e.g. Claude Desktop) use a single session for all chats.
        #   Therefore, we view project activation as an "entry action", which must always provide
        #   all the information that is relevant to the project
        # Because of this, we cannot use a condition like this:
        #   new_mode_prompts_must_be_provided_for_activation = not self._get_session_status(session_id).mode_prompts_provided
        new_mode_prompts_must_be_provided_for_activation = True
        mark_used(session_id)

        if new_mode_prompts_must_be_provided_for_activation:
            for mode_name in self._newly_activated_mode_names:
                mode = ActiveModes.get_mode_instance(mode_name)
                if mode.has_prompt():
                    result.append(mode)
        return result

    def mark_mode_prompts_as_provided(self, session_id: str) -> None:
        """
        Marks the prompts for all newly activated modes as provided, so that they will not be included in the project activation message.

        :param session_id: the client session ID
        """
        self._get_session_status(session_id).mode_prompts_provided = True

    def mark_project_activation_message_as_provided(self, session_id: str) -> None:
        """
        Marks the project activation message as provided, so that it will not be included again in case of multiple activations of the same project.

        :param session_id: the client session ID
        """
        self._get_session_status(session_id).project_activation_message_provided = True

    def is_project_activation_message_already_provided(self, session_id: str) -> bool:
        """
        :param session_id: the client session ID
        :return: whether the project activation message was already provided and therefore should not be included again
        """
        return self._get_session_status(session_id).project_activation_message_provided


class SerenaAgent:
    def __init__(
        self,
        project: str | None = None,
        *,
        project_activation_callback: Callable[[], None] | None = None,
        project_activation_error: str | None = None,
        serena_config: SerenaConfig | None = None,
        context: SerenaAgentContext | None = None,
        modes: ModeSelectionDefinition | None = None,
    ):
        """
        :param project: the project to load immediately or None to not load any project; may be a path to the project or a name of
            an already registered project;
        :param project_activation_callback: a callback function to be called when a project is activated.
        :param project_activation_error: an initial error to report back to the client/LLM pertaining to project determination/activation
            in Serena's initial prompts. This is only applicable if `project` is None.
        :param serena_config: the Serena configuration or None to read the configuration from the default location.
        :param context: the context in which the agent is operating, None for default context.
            The context may adjust prompts, tool availability, and tool descriptions.
        :param modes: mode selection definition to apply for this session
        """
        self._active_project: Project | None = None  # NOTE: field name used in __del__
        self._project_activation_callback = project_activation_callback
        self._project_activation_error: str | None = project_activation_error
        self._project_prompt_status = ProjectPromptProvisionStatus()
        self._session_mode_selection_definition = modes
        self.version = serena_version()

        # obtain serena configuration using the decoupled factory function
        self.serena_config = serena_config or SerenaConfig.from_config_file()

        # register the project to be activated (if any) if it has a project configuration but is not yet registered
        if project is not None:
            self.serena_config.get_registered_project(project, autoregister=True)

        # adjust log level
        serena_log_level = self.serena_config.log_level
        if Logger.root.level != serena_log_level:
            log.info(f"Changing the root logger level to {serena_log_level}")
            Logger.root.setLevel(serena_log_level)

        # set the agent context
        if context is None:
            context = SerenaAgentContext.load_default()
        self._context = context

        # instantiate all tool classes
        self._all_tools: dict[type[Tool], Tool] = {tool_class: tool_class(self) for tool_class in ToolRegistry().get_all_tool_classes()}

        # log fundamental information
        log.info(
            f"Starting Serena server (version={self.version}, process id={os.getpid()}, parent process id={os.getppid()}); "
            f"Python version={platform.python_version()}, platform={platform.platform()}"
        )
        log.info("Configuration file: %s", self.serena_config.config_file_path)
        log.info("Available projects: {}".format(", ".join(self.serena_config.project_names)))
        log.info(f"Loaded tools ({len(self._all_tools)}): {', '.join([tool.get_name_from_cls() for tool in self._all_tools.values()])}")

        # create the tool names mapping for prompts
        self._prompt_tool_names_mapping = self._create_prompt_tool_names_mapping()

        # create executor for starting the language server and running tools in another thread
        # This executor is used to achieve linear task execution
        self._task_executor = TaskExecutor("SerenaAgentTaskExecutor")

        # Initialize the prompt factory
        self.prompt_factory = SerenaPromptFactory()

        # initialise active modes (baseline modes prior to project activation, allowing newly activated modes to be tracked)
        self._active_modes: ActiveModes
        self._update_active_modes(log_message=False)

        # activate the given project (if any), also updating the active modes
        # Note: We cannot update the active tools yet, because the base toolset has not been computed yet
        #       (and its computation depends on the active project)
        if project is not None:
            try:
                self.activate_project_from_path_or_name(project, update_active_modes=False, update_active_tools=False)
            except Exception as e:
                log.error(f"Error activating project '{project}' at startup: {e}", exc_info=e)
                self._project_activation_error = str(e)
        self._update_active_modes()

        # determine the base toolset defining the set of exposed tools (which e.g. the MCP shall see),
        self._base_toolset = self._create_base_toolset(self.serena_config, self._context, self._active_modes, self._active_project)
        self._exposed_tools = self._base_toolset.to_available_tools(self._all_tools)
        log.info(f"Number of exposed tools: {len(self._exposed_tools)}. Exposed tools: {self._exposed_tools.tool_names}")

        # update the active tools (considering the active project, if any)
        self._active_tools: AvailableTools
        self._update_active_tools()

        self._send_usage_info()

    def _send_usage_info(self) -> None:
        if os.getenv("CI") == "true" or os.getenv("GITHUB_ACTIONS") == "true" or os.getenv("SERENA_USAGE_REPORTING") == "false":
            return
        params: dict[str, str | int] = {
            "os": platform.system(),
            "version": self.version,
            "context": self._context.name,
        }
        try:
            requests.get("https://oraios-software.de/serena_usage.php", params=params, timeout=1)
        except Exception as e:
            log.debug(f"Failed to send usage info: {e}")

    @classmethod
    def _create_base_toolset(
        cls,
        serena_config: SerenaConfig,
        context: SerenaAgentContext,
        modes: ActiveModes,
        project: Project | None,
    ) -> ToolSet:
        """
        Determines the base toolset defining the set of exposed tools (which e.g. the MCP shall see).
        It depends on ...
           * Serena config
           * the context (which is fixed for the session)
           * the base modes
           * the optional tools enabled by initial dynamic modes
           * single-project mode reductions (if applicable)
        """
        # consider Serena configuration and the active context
        tool_inclusion_definitions: list[ToolInclusionDefinition] = [serena_config, context]

        # determine whether we are operating in a single-project context
        # (i.e. the project that is activated at startup is the only project that will be worked with throughout the session)
        is_single_project = context.single_project and project is not None

        # consider modes
        # * base modes: These cannot be changed, so they are fully applied
        for base_mode in modes.get_base_modes():
            tool_inclusion_definitions.append(base_mode)
        # * dynamically activated modes:
        #    - When not in a single-project context, these modes can later be turned off,
        #      so we consider only their inclusions (but not their exclusions, because these must not be hard).
        #    - In a single-project context, we can consider them fully.
        for mode in modes.get_dynamically_activated_modes():
            if is_single_project:
                tool_inclusion_definitions.append(mode)
            else:
                # Since modes can be dynamically turned on and off, we don't include their definitions directly,
                # For the initially active dynamic modes, we make sure that the tools they enable are included.
                tool_inclusion_definitions.append(
                    NamedToolInclusionDefinition(
                        name=f"InitialDynamicModeInclusions[{mode.name}]", included_optional_tools=mode.included_optional_tools
                    )
                )

        # When in a single-project context, the agent is assumed to work on a single project, and we thus
        # want to apply that project's tool exclusions/inclusions from the get-go, limiting the set
        # of tools that will be exposed to the client.
        # Furthermore, we disable tools that are only relevant for project activation.
        # So if the project exists, we apply all the aforementioned exclusions.
        if is_single_project:
            assert project is not None
            log.info(
                "Applying tool inclusion/exclusion definitions for single-project context based on project '%s'",
                project.project_name,
            )
            tool_inclusion_definitions.append(
                NamedToolInclusionDefinition(
                    name="SingleProjectExclusions",
                    excluded_tools=[ActivateProjectTool.get_name_from_cls(), GetCurrentConfigTool.get_name_from_cls()],
                )
            )
            tool_inclusion_definitions.append(project.project_config)

        # compute the resulting tool set
        base_toolset = ToolSet.default().apply(*tool_inclusion_definitions)
        log.info(f"Number of exposed tools: {len(base_toolset)}")
        return base_toolset

    def get_language_server_manager(self) -> LanguageServerManager | None:
        if self._active_project is not None:
            return self._active_project.language_server_manager
        return None

    def get_language_server_manager_or_raise(self) -> LanguageServerManager:
        active_project = self.get_active_project_or_raise()
        return active_project.get_language_server_manager_or_raise()

    def get_log_inspection_instructions(self) -> str:
        log_path = SerenaPaths().last_returned_log_file_path
        if log_path is not None:
            return f"Find the current log file here: {log_path}"
        else:
            return "Unfortunately, logs are not available. We recommend enabling logging to a file."

    def get_context(self) -> SerenaAgentContext:
        return self._context

    def get_tool_description_override(self, tool_name: str) -> str | None:
        return self._context.tool_description_overrides.get(tool_name, None)

    def get_exposed_tool_instances(self) -> list["Tool"]:
        """
        :return: the tool instances which are exposed (e.g. to the MCP client).
            Note that the set of exposed tools is fixed for the session, as
            clients don't react to changes in the set of tools, so this is the superset
            of tools that can be offered during the session.
            If a client should attempt to use a tool that is dynamically disabled
            (e.g. because a project is activated that disables it), it will receive an error.
        """
        return list(self._exposed_tools.tools)

    def get_active_project(self) -> Project | None:
        """
        :return: the active project or None if no project is active
        """
        return self._active_project

    def get_active_project_or_raise(self) -> Project:
        """
        :return: the active project or raises an exception if no project is active
        """
        project = self.get_active_project()
        if project is None:
            raise ValueError("No active project. Please activate a project first.")
        return project

    def get_active_modes(self) -> ActiveModes:
        """
        :return: the active modes
        """
        return self._active_modes

    @staticmethod
    def _create_prompt_tool_names_mapping() -> dict[str, str]:
        """
        Creates a mapping from tool names to the tool names to use in prompts, which takes legacy tool names
        (where the name was changed) into consideration.

        The mapping is intended to be used for the generation of prompts, such that prompts can
        refer to tool names as `{{ tool_names["find_symbol"] }}`.

        :return: the mapping from tool names to new tool names
        """
        result = dict(ToolSet.LEGACY_TOOL_NAME_MAPPING)
        for tool_class in ToolRegistry().get_all_tool_classes():
            result[tool_class.get_name_from_cls()] = tool_class.get_name_from_cls()
        return result

    @staticmethod
    def _format_prompt_tag(text: str, tag: str, tag_name_attr: str | None = None) -> str:
        open_tag = f"<{tag}" + (f' name="{tag_name_attr}"' if tag_name_attr is not None else "") + ">"
        close_tag = f"</{tag}>"
        return f"{open_tag}\n{text.strip()}\n{close_tag}"

    def _render_prompt(self, prompt_template: str, tag: str | None = None, tag_name_attr: str | None = None) -> str:
        """
        Renders the given prompt template, providing the necessary variables and functions

        :param prompt_template: the template text (jinja2) to render
        :param tag: if not None, wraps the rendered prompt text in a tag
        :param tag_name_attr: for the case where tag is not None, specifies the value of the "name" attribute of the tag
        :return: the rendered prompt
        """

        def embed_memory(memory_name: str) -> str:
            try:
                memory_manager = self._get_memory_manager()
                return self._format_prompt_tag(memory_manager.load_memory(memory_name), tag="memory", tag_name_attr=memory_name)
            except Exception as e:
                log.error("Tried to embed memory '%s' but failed to load it: %s", memory_name, e)
                return ""

        template = JinjaTemplate(prompt_template)
        text = template.render(
            available_tools=self._exposed_tools.tool_names,
            available_markers=self._exposed_tools.tool_marker_names,
            tool_names=self._prompt_tool_names_mapping,
            embed_memory=embed_memory,
        )

        if tag is not None:
            text = self._format_prompt_tag(text, tag=tag, tag_name_attr=tag_name_attr)

        return text

    def create_connection_prompt(self) -> str:
        """
        Returns the bootstrap prompt to be sent at MCP connection time.

        :return: the prompt
        """
        return self.prompt_factory.create_connection_prompt()

    def _create_global_memory_manager(self) -> MemoryManager:
        """
        :return: a memory manager for global memories only (no project memories)
        """
        return MemoryManager(serena_data_folder=None, read_only_memory_patterns=self.serena_config.read_only_memory_patterns)

    def _get_memory_manager(self) -> MemoryManager:
        """
        :return: the memory manager for the active project (if any) or a global memory manager if no project is active
        """
        if self._active_project is not None:
            return self._active_project.memory_manager
        else:
            return self._create_global_memory_manager()

    def create_system_prompt(self, session_id: str = "global") -> str:
        """
        Returns the 'Serena Instructions Manual', i.e. Serena's system prompt.

        :param session_id: the client session ID for the case where this is run from a tool; "global" for the connection time case
        :return: the prompt
        """
        available_tools = self._active_tools
        available_markers = available_tools.tool_marker_names
        global_memories = self._create_global_memory_manager().list_global_memories()
        global_memories_str = dict_string(global_memories.to_dict()) if len(global_memories) > 0 else ""
        log.info("Generating system prompt with available_tools=(see active tools), available_markers=%s", available_markers)

        # determine modes for which prompts must (still) be provided, excluding modes that were already provided in a
        # previously provided project activation message (if any)
        relevant_modes = []
        for mode in self.get_active_modes().get_modes():
            if mode.has_prompt():
                if not self._project_prompt_status.is_mode_prompt_already_provided(mode.name, session_id):
                    relevant_modes.append(mode)
        self._project_prompt_status.mark_mode_prompts_as_provided(session_id)

        system_prompt = self.prompt_factory.create_system_prompt(
            context_system_prompt=self._render_prompt(self._context.prompt, tag="context"),
            mode_system_prompts=[self._render_prompt(mode.prompt, tag="mode", tag_name_attr=mode.name) for mode in relevant_modes],
            available_tools=available_tools.tool_names,
            available_markers=available_markers,
            global_memories_list=global_memories_str,
            tool_names=self._prompt_tool_names_mapping,
        )

        # provide the project activation message if it hasn't yet been provided
        if self._active_project is not None and not self._project_prompt_status.is_project_activation_message_already_provided(session_id):
            system_prompt += "\n\n" + self._format_prompt_tag(self.get_project_activation_message(session_id), tag="active-project")
        elif self._project_activation_error:
            system_prompt += f"\n\nNo project is active ({self._project_activation_error})."

        return self._format_prompt_tag(system_prompt, tag="serena")

    def get_project_activation_message(self, session_id: str) -> str:
        """
        :return: a message providing information about the project upon activation (e.g. programming language, memories, initial prompt)
        :raise: AssertionError if no project is active
        """
        proj = self._active_project
        assert proj is not None, "A project must be active before calling this."

        # Note: The activation message is always returned in full, even if it was already provided in the current session,
        #   because some clients (e.g. Claude Desktop) will use the same session across multiple chats.
        #   So while we don't want the activation message to be additionally included in the system prompt
        #   (initial_instructions), an explicit project activation should always return it.
        # (The check below deliberately left in place for documentation purposes, preventing a regression)
        if self._project_prompt_status.is_project_activation_message_already_provided(session_id):
            pass  # no special handling

        # provide basic project information (name, location, languages, encoding)
        if proj.is_newly_created:
            msg = f"Created and activated a new project with name '{proj.project_name}' at {proj.project_root}.\n"
        else:
            msg = f"The project with name '{proj.project_name}' at {proj.project_root} is activated.\n"
        language_servers_str = ", ".join([ls.value for ls in proj.project_config.language_servers])
        msg += f"Active language servers: {language_servers_str}.\n"
        msg += f"File encoding: {proj.project_config.encoding}.\n"

        # add list of memories (if memories are enabled)
        include_memories = self._active_tools.contains_tool_class(ReadMemoryTool)
        if include_memories:
            project_memories = proj.memory_manager.list_project_memories()
            if project_memories:
                msg += (
                    f"{json.dumps(project_memories.to_dict())}\n"
                    + f"Use the `{ReadMemoryTool.get_name_from_cls()}` tool to read these memories later if they are relevant to the task.\n"
                )
            elif self._active_tools.contains_tool_class(OnboardingTool):
                msg += f"Onboarding has not been performed yet. Ask the user whether to perform onboarding via the `{OnboardingTool.get_name_from_cls()}` tool.\n"

        # add prompts for modes that were dynamically activated by the project
        modes_with_prompts = self._project_prompt_status.get_modes_with_prompts_to_be_provided_for_project_activation(session_id)
        if modes_with_prompts:
            for mode in modes_with_prompts:
                msg += self._render_prompt(mode.prompt, tag="mode", tag_name_attr=mode.name) + "\n"
        self._project_prompt_status.mark_mode_prompts_as_provided(session_id)

        # add project-specific prompt
        if proj.project_config.initial_prompt:
            msg += "\n" + self._render_prompt(proj.project_config.initial_prompt, tag="project-instructions")

        self._project_prompt_status.mark_project_activation_message_as_provided(session_id)

        return msg

    def _update_active_modes(self, log_message: bool = True) -> None:
        """
        Updates the active modes based on the Serena configuration, the active project configuration (if any),
        and mode overrides (if any).
        """
        self._active_modes = ActiveModes()
        self._active_modes.apply(self.serena_config)
        if self._active_project:
            self._active_modes.apply(self._active_project.project_config)
        if self._session_mode_selection_definition:
            self._active_modes.apply(self._session_mode_selection_definition)
        if log_message:
            active_mode_names = self._active_modes.get_mode_names()
            log.info(f"Active modes ({len(active_mode_names)}): {', '.join(active_mode_names)}")

    def _update_active_tools(self) -> None:
        """
        Updates the active tools based on the active modes and the active project.
        The base tool set already takes the Serena configuration and the context into account.
        """
        # apply modes
        tool_set = self._base_toolset.apply(*self._active_modes.get_modes())

        # apply active project configuration (if any)
        if self._active_project is not None:
            tool_set = tool_set.apply(self._active_project.project_config)
            if self._active_project.project_config.read_only:
                tool_set = tool_set.without_editing_tools()

        self._active_tools = tool_set.to_available_tools(self._all_tools)
        log.info(f"Active tools ({len(self._active_tools)}): {', '.join(self._active_tools.tool_names)}")

        # check if a tool was activated that is not in the exposed tool set and issue a warning if so
        active_tools_not_exposed = set(self._active_tools.tool_names) - set(self._exposed_tools.tool_names)
        if active_tools_not_exposed:
            log.warning(
                "The following active tools are not in the exposed tool set and thus won't be available to clients:\n"
                f"{active_tools_not_exposed}\n"
                "Consider adjusting your configuration to include these tools if you want to use them."
            )

    def issue_task(
        self, task: Callable[[], T], name: str | None = None, logged: bool = True, timeout: float | None = None
    ) -> TaskExecutor.Task[T]:
        """
        Issue a task to the executor for asynchronous execution.
        It is ensured that tasks are executed in the order they are issued, one after another.

        :param task: the task to execute
        :param name: the name of the task for logging purposes; if None, use the task function's name
        :param logged: whether to log management of the task; if False, only errors will be logged
        :param timeout: the maximum time to wait for task completion in seconds, or None to wait indefinitely
        :return: the task object, through which the task's future result can be accessed
        """
        return self._task_executor.issue_task(task, name=name, logged=logged, timeout=timeout)

    def execute_task(self, task: Callable[[], T], name: str | None = None, logged: bool = True, timeout: float | None = None) -> T:
        """
        Executes the given task synchronously via the agent's task executor.
        This is useful for tasks that need to be executed immediately and whose results are needed right away.

        :param task: the task to execute
        :param name: the name of the task for logging purposes; if None, use the task function's name
        :param logged: whether to log management of the task; if False, only errors will be logged
        :param timeout: the maximum time to wait for task completion in seconds, or None to wait indefinitely
        :return: the result of the task execution
        """
        return self._task_executor.execute_task(task, name=name, logged=logged, timeout=timeout)

    def _activate_project(self, project: Project, update_active_modes: bool = True, update_active_tools: bool = True) -> bool:
        """
        :return: True if the project was newly activated, False if it was already active
        """
        # check if the project is already active
        if self._active_project is not None and self._active_project.project_root == project.project_root:
            return False

        log.info(f"Activating {project.project_name} at {project.project_root}")

        self._project_activation_error = None

        # shut down the previously active project to release its language server processes
        if self._active_project is not None:
            log.info(f"Shutting down previously active project '{self._active_project.project_name}' before switching")
            self._active_project.shutdown()

        self._active_project = project
        project.set_agent(self)

        if update_active_modes:
            active_mode_names_before = set(self._active_modes.get_mode_names())
            self._update_active_modes()
            newly_activated_mode_names = set(self._active_modes.get_mode_names()) - active_mode_names_before
        else:
            newly_activated_mode_names = None

        self._project_prompt_status = ProjectPromptProvisionStatus(newly_activated_mode_names=newly_activated_mode_names)

        if update_active_tools:
            self._update_active_tools()

        # create the project's language server manager in the background (it may have to wait for the project
        # configuration to be generated); the language servers themselves are only started on first use
        self.issue_task(self.reset_language_server_manager, name="CreateLanguageServerManager")

        if self._project_activation_callback is not None:
            self._project_activation_callback()

        return True

    def activate_project_from_path_or_name(
        self, project_root_or_name: str, update_active_modes: bool = True, update_active_tools: bool = True
    ) -> bool:
        """
        Activate a project from a path or a name.
        If the project was already registered, it will just be activated.
        If the argument is a path at which no Serena project previously existed, the project will be created beforehand.
        Raises ProjectNotFoundError if the project could neither be found nor created.

        :return: True if the project was newly activated, False if it was already active
        """
        project_instance: Project | None = self.serena_config.get_project(project_root_or_name)
        if project_instance is not None:
            log.info(f"Found registered project '{project_instance.project_name}' at path {project_instance.project_root}")
        elif os.path.isdir(project_root_or_name):
            project_instance = self.serena_config.add_project_from_path(project_root_or_name, asynchronous_autogen=True)
            log.info(f"Added new project {project_instance.project_name} for path {project_instance.project_root}")

        if project_instance is None:
            raise ProjectNotFoundError(
                f"Project '{project_root_or_name}' not found: Not a valid project name or directory. "
                f"Existing project names: {self.serena_config.project_names}"
            )

        return self._activate_project(project_instance, update_active_modes=update_active_modes, update_active_tools=update_active_tools)

    def get_active_tool_names(self) -> list[str]:
        """
        :return: the list of names of the active tools for the current project, sorted alphabetically
        """
        return self._active_tools.tool_names

    def tool_is_active(self, tool_name: str) -> bool:
        """
        :param tool_class: the name of the tool to check
        :return: True if the tool is active, False otherwise
        """
        return self._active_tools.contains_tool_name(tool_name)

    def tool_is_exposed(self, tool_name: str) -> bool:
        """
        :param tool_name: the name of the tool to check
        :return: True if the tool is in the exposed tool set, False otherwise
        """
        return self._exposed_tools.contains_tool_name(tool_name)

    def get_current_config_overview(self) -> str:
        """
        :return: a string overview of the current configuration, including the active and available configuration options
        """
        result_str = "Current configuration:\n"
        result_str += f"Serena version: {self.version}\n"
        result_str += f"Loglevel: {self.serena_config.log_level}, trace_lsp_communication={self.serena_config.trace_lsp_communication}\n"
        if self._active_project is not None:
            result_str += f"Active project: {self._active_project.project_name}\n"
        else:
            result_str += "No active project\n"
        if self._active_project:
            result_str += f"Language server status: {self._active_project.get_language_server_manager_status()}\n"
        result_str += "Available projects:\n" + "\n".join(list(self.serena_config.project_names)) + "\n"
        result_str += f"Active context: {self._context.name}\n"

        # Active modes
        active_mode_names = self.get_active_modes().get_mode_names()
        result_str += "Active modes: {}\n".format(", ".join(active_mode_names)) + "\n"

        # Available but not active modes
        all_available_modes = SerenaAgentMode.list_registered_mode_names()
        inactive_modes = [mode for mode in all_available_modes if mode not in active_mode_names]
        if inactive_modes:
            result_str += "Available but not active modes: {}\n".format(", ".join(inactive_modes)) + "\n"

        # Active tools
        result_str += "Active tools (after all exclusions from the project, context, and modes):\n"
        active_tool_names = self.get_active_tool_names()
        # print the tool names in chunks
        chunk_size = 4
        for i in range(0, len(active_tool_names), chunk_size):
            chunk = active_tool_names[i : i + chunk_size]
            result_str += "  " + ", ".join(chunk) + "\n"

        # Available but not active tools
        all_tool_names = sorted([tool.get_name_from_cls() for tool in self._all_tools.values()])
        inactive_tool_names = [tool for tool in all_tool_names if tool not in active_tool_names]
        if inactive_tool_names:
            result_str += "Available but not active tools:\n"
            for i in range(0, len(inactive_tool_names), chunk_size):
                chunk = inactive_tool_names[i : i + chunk_size]
                result_str += "  " + ", ".join(chunk) + "\n"

        return result_str

    def reset_language_server_manager(self) -> None:
        """
        Starts/resets the language server manager for the current project
        """
        self.get_active_project_or_raise().create_language_server_manager()

    def add_language_server(self, ls_id: LanguageServerId) -> None:
        """
        Adds a new language server to the active project, spawning the respective language server and updating the project configuration.
        The addition is scheduled via the agent's task executor and executed synchronously, i.e. the method returns
        when the addition is complete.

        :param ls_id: the language server to add
        """
        self.execute_task(lambda: self.get_active_project_or_raise().add_language_server(ls_id), name=f"AddLanguage:{ls_id.value}")

    def remove_language_server(self, ls_id: LanguageServerId) -> None:
        """
        Removes a language server from the active project, shutting down the respective server and updating the project configuration.
        The removal is scheduled via the agent's task executor and executed asynchronously.

        :param ls_id: the language to remove
        """
        self.issue_task(lambda: self.get_active_project_or_raise().remove_language_server(ls_id), name=f"RemoveLanguage:{ls_id.value}")

    def get_tool(self, tool_class: type[TTool]) -> TTool:
        return self._all_tools[tool_class]

    def print_tool_overview(self) -> None:
        ToolRegistry().print_tool_overview(self._active_tools.tools)

    def __del__(self) -> None:
        is_object_initialised = "_active_project" in self.__dict__
        if is_object_initialised:
            self.on_shutdown()

    def on_shutdown(self, timeout: float = 2.0) -> None:
        """
        Shutdown handler of the agent, freeing resources and stopping background tasks.
        """
        log.info("SerenaAgent is shutting down ...")
        if self._active_project is not None:
            log.info(f"Shutting down active project '{self._active_project.project_name}' ...")
            self._active_project.shutdown(timeout=timeout)
            self._active_project = None

    def shutdown(self) -> None:
        """
        Triggers a hard shutdown of the agent, freeing resources and signalling the process to terminate
        """
        # perform clean-up right away, because kill does not result in normal deletion of the object
        self.on_shutdown()

        # signal process termination
        os.kill(os.getpid(), signal.SIGTERM)

    def get_tool_by_name(self, tool_name: str) -> Tool:
        tool_class = ToolRegistry().get_tool_class_by_name(tool_name)
        return self.get_tool(tool_class)

    def get_active_language_server_ids(self) -> list[LanguageServerId]:
        ls_manager = self.get_language_server_manager()
        if ls_manager is None:
            return []
        return ls_manager.get_active_language_server_ids()

    @contextmanager
    def active_project_context(self, project: Project) -> Iterator[None]:
        """
        Context manager for temporarily setting/overriding the active project

        :param project: the project to be active
        """
        original_project = self._active_project
        self._active_project = project
        try:
            yield
        finally:
            self._active_project = original_project

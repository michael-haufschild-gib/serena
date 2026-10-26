from serena.config.client_setup import client_setup_handlers
from serena.config.context_mode import SerenaAgentContext


def test_client_setup_handlers_use_resolvable_contexts():
    for handler in client_setup_handlers:
        context_options = [option for option in handler.get_mcp_server_options() if option.startswith("--context=")]
        assert len(context_options) == 1
        context_name = context_options[0].removeprefix("--context=")
        assert SerenaAgentContext.from_name(context_name).name == context_name

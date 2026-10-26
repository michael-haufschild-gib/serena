import logging
import sys

log = logging.getLogger(__name__)


def report_fatal_exception(e: Exception) -> None:
    """
    Reports a fatal exception, logging it (including the traceback) and printing it to stderr, so that it remains
    visible to MCP clients that capture the server's stderr even if logging is not (yet) configured.

    :param e: the exception to report
    """
    log.error(f"Fatal exception: {e}", exc_info=e)
    print(f"Fatal exception: {e}", file=sys.stderr)

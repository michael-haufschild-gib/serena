import logging
import os
import shlex
import signal
import subprocess
import threading
from typing import IO, TYPE_CHECKING, Generic, TypeVar

from sensai.util.string import ToStringMixin

if TYPE_CHECKING:
    from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo

log = logging.getLogger(__name__)
TStream = TypeVar("TStream", bound=str | bytes)


def subprocess_run(
    cmd: list[str] | str, timeout: int | None = None, check: bool = False, capture_output: bool = True, text: bool = True, **kwargs
) -> subprocess.CompletedProcess:
    """
    Runs a command in a subprocess, applying safe default settings.

    The stdin of the subprocess is set to DEVNULL to avoid interference with the parent process' stdin;
    this cannot be overridden by passing a different value for stdin in kwargs.

    :param cmd: the command to run, specified as a list of arguments or a string
    :param timeout: the timeout in seconds for the command to complete; if None, no timeout is applied
    :param check: if True, raises CalledProcessError if the command exits with a non-zero status
    :param capture_output: if True, captures stdout and stderr; otherwise, they are not captured
    :param text: if True, captures output as text (str); otherwise, captures as bytes
    :return: a CompletedProcess instance containing information about the completed process
    """
    kwargs = dict(kwargs)
    kwargs.update(
        {
            "timeout": timeout,
            "capture_output": capture_output,
            "text": text,
            "stdin": subprocess.DEVNULL,  # important to avoid interference with parent process' stdin
        }
    )
    return subprocess.run(cmd, check=check, **kwargs)


def convert_shell_cmd(cmd: str | list[str]) -> str:
    """
    Converts a command to a string suitable for subprocess calls with ``shell=True``, quoting the
    arguments if the command is given as a list.

    :param cmd: the command to convert, specified as a list of arguments or as a string
    :return: the shell command string
    """
    return shlex.join(cmd) if isinstance(cmd, list) else cmd


class ManagedSubprocess(Generic[TStream], ToStringMixin):
    """
    Represents a subprocess.Popen instance running in its own process group, which can be terminated
    together with all its descendants, gracefully at first and forcefully if necessary.
    """

    def __init__(self, popen: subprocess.Popen[TStream], name: str, reaper: "ProcessGroupReaper") -> None:
        """
        :param popen: the subprocess.Popen instance, which must have been launched with ``start_new_session=True``
        :param name: the name of the process (used for logging purposes); should start with a capital letter
        :param reaper: the reaper the process group is registered with, which is told when the group is gone
        """
        self._popen = popen
        self._name = name
        self._reaper = reaper

        # a process launched with start_new_session=True is its own group leader, so its PGID is its PID
        self._process_group_id = popen.pid

    def _tostring_includes(self) -> list[str]:
        return ["_name"]

    @property
    def stdin(self) -> "IO[TStream] | None":
        return self._popen.stdin

    @property
    def stdout(self) -> "IO[TStream] | None":
        return self._popen.stdout

    @property
    def stderr(self) -> "IO[TStream] | None":
        return self._popen.stderr

    @property
    def returncode(self) -> int | None:
        """
        :return: the exit code of the process as of the most recent status check (see :meth:`poll`),
            or None if the process was not yet known to have terminated
        """
        return self._popen.returncode

    def poll(self) -> int | None:
        """
        Checks whether the process has terminated, updating :attr:`returncode` accordingly.

        :return: the exit code of the process or None if it is still running
        """
        return self._popen.poll()

    def terminate(self, timeout: float) -> None:
        """
        Terminates the process and all other processes in its group, forcefully killing them if they do
        not exit in time.

        :param timeout: the time, in seconds, to wait for the process to terminate gracefully before killing it
        """
        terminate_process_group_with_kill_fallback(
            self._popen,
            self._process_group_id,
            terminate_timeout=timeout,
            process_name=self._name,
        )
        self._reaper.unregister(self._process_group_id)


class ProcessGroupReaper:
    """
    Guarantees that the process groups registered with it do not outlive this process, even if this
    process is killed without any chance to clean up (e.g. SIGKILL).

    It runs a minimal watcher shell in its own session, which reads group registrations from a pipe.
    The write end of that pipe is held only by this process, so the kernel closes it when this process
    dies, however that happens; the watcher then reads end-of-file and kills every group still registered.
    Groups that were terminated regularly are unregistered first, so a later reuse of their IDs is safe.
    """

    _WATCHER_SCRIPT = """
groups=""
while IFS= read -r line; do
  case "$line" in
    +*) groups="$groups ${line#+}" ;;
    -*) gone="${line#-}"; rest=""
        for g in $groups; do [ "$g" = "$gone" ] || rest="$rest $g"; done
        groups="$rest" ;;
  esac
done
[ -z "$groups" ] && exit 0
for g in $groups; do kill -TERM -"$g" 2>/dev/null; done
sleep 2
for g in $groups; do kill -KILL -"$g" 2>/dev/null; done
"""
    """the watcher, a POSIX shell script; lines `+<pgid>` register a group, lines `-<pgid>` unregister it"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._watcher: subprocess.Popen[bytes] | None = None

    def _send(self, line: str) -> None:
        with self._lock:
            if self._watcher is None or self._watcher.poll() is not None:
                self._watcher = subprocess.Popen(
                    ["/bin/sh", "-c", self._WATCHER_SCRIPT],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,  # immune to signals sent to our own process group
                )
            assert self._watcher.stdin is not None
            try:
                self._watcher.stdin.write(f"{line}\n".encode())
                self._watcher.stdin.flush()
            except OSError as e:
                log.warning(f"Could not notify the process group watcher ({line}): {e}")

    def register(self, process_group_id: int) -> None:
        """
        :param process_group_id: the group to kill should this process die without terminating it first
        """
        self._send(f"+{process_group_id}")

    def unregister(self, process_group_id: int) -> None:
        """
        :param process_group_id: a group previously registered via :meth:`register` that has been terminated
        """
        self._send(f"-{process_group_id}")


class ManagedSubprocessLauncher:
    """
    Launcher for managed subprocesses (see :class:`ManagedSubprocess`), which are started for stdio-based communication.
    It is home to the concern of launching a subprocess with well-defined lifecycle properties: every
    subprocess runs in its own process group, which cannot outlive this process (see :class:`ProcessGroupReaper`).

    The class is a singleton, as it owns the process-wide reaper.
    """

    _instance: "ManagedSubprocessLauncher | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._reaper = ProcessGroupReaper()

    @classmethod
    def get_instance(cls) -> "ManagedSubprocessLauncher":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def launch(self, process_launch_info: "ProcessLaunchInfo", name: str) -> ManagedSubprocess[bytes]:
        """
        Launches a subprocess from ``process_launch_info`` in its own session (and hence its own process group).

        :param process_launch_info: the command, environment and working directory to launch with
        :param name: the name of the process (used for logging purposes); should start with a capital letter
        """
        # build the child environment from ours, overridden by the launch info's entries
        child_proc_env = os.environ.copy()
        child_proc_env.update(process_launch_info.env)

        popen = subprocess.Popen(
            convert_shell_cmd(process_launch_info.cmd),
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_proc_env,
            cwd=process_launch_info.cwd,
            shell=True,
            start_new_session=True,
        )
        self._reaper.register(popen.pid)
        return ManagedSubprocess(popen, name, self._reaper)


def _signal_process_group(pgid: int, terminate: bool = True) -> None:
    """
    Sends a signal to every process in the given process group by group ID, without enumerating the
    system process table (which can be denied even for processes we started and own).

    :param pgid: the process group ID to signal
    :param terminate: if True, signal terminate (SIGTERM), otherwise signal kill (SIGKILL)
    """
    sig = signal.SIGTERM if terminate else signal.SIGKILL
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        # the group is already gone; nothing left to signal
        pass
    except PermissionError as e:
        log.warning(f"Permission denied signaling process group {pgid} with {sig.name}: {e}")
    except Exception as e:
        log.warning(f"Unexpected error signaling process group {pgid} with {sig.name}: {e}")


def terminate_process_group_with_kill_fallback(
    process: subprocess.Popen,
    process_group_id: int,
    terminate_timeout: float,
    process_name: str = "Process",
) -> None:
    """
    Terminates the process group led by the given process by signaling it to terminate, and kills the
    group forcefully if the leader does not exit within the given timeout.

    The termination is logged.

    :param process: the group leader, i.e. a process launched with ``start_new_session=True``
    :param process_group_id: the ID of the group led by ``process`` (equal to its PID). Never pass the group of
        a process that shares our own group: signaling it would also signal us.
    :param terminate_timeout: the time to wait for the process to terminate gracefully before killing it
    :param process_name: the name of the process (used for logging purposes); should start with capital letter
    """
    log.debug(f"Terminating process {process.pid}, current status: {process.poll()}")

    _signal_process_group(process_group_id, terminate=True)
    try:
        log.debug(f"Waiting for process {process.pid} to terminate...")
        exit_code = process.wait(timeout=terminate_timeout)
        log.info(f"{process_name} terminated successfully with exit code {exit_code}.")
    except subprocess.TimeoutExpired:
        log.warning(f"{process_name} (pid={process.pid}) termination timed out, killing process forcefully...")
        _signal_process_group(process_group_id, terminate=False)
        try:
            exit_code = process.wait(timeout=2.0)
            log.info(f"{process_name} killed successfully with exit code {exit_code}.")
        except subprocess.TimeoutExpired:
            log.error(f"{process_name} (pid={process.pid}) could not be killed within timeout.")
    except Exception as e:
        log.error(f"Error during process shutdown: {e}")

    # children that outlived the leader (e.g. ignoring SIGTERM) remain in the group; make sure they are gone too
    _signal_process_group(process_group_id, terminate=False)

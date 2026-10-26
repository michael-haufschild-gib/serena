import concurrent.futures
import threading
import time
from collections.abc import Callable
from threading import Thread
from typing import Generic, TypeVar

from sensai.util import logging
from sensai.util.logging import LogTime
from sensai.util.string import ToStringMixin

log = logging.getLogger(__name__)
T = TypeVar("T")


class TaskExecutor:
    """
    Executes tasks one after another, in the order they were issued.
    """

    GRACE_PERIOD_AFTER_TIMEOUT = 30.0
    """
    seconds for which the next task is held back after a task timed out, waiting for the timed-out task's thread to
    actually finish; this keeps two tasks from running at the same time, unless a task hangs for longer than this
    """

    def __init__(self, name: str):
        """
        :param name: the name of the task executor, used for logging purposes
        """
        self._task_executor_lock = threading.Lock()
        self._task_executor_queue: list[TaskExecutor.Task] = []
        self._task_executor_thread = Thread(target=self._process_task_queue, name=name, daemon=True)
        self._task_executor_thread.start()
        self._task_executor_task_index = 1
        self._task_executor_current_task: TaskExecutor.Task | None = None

    class Task(ToStringMixin, Generic[T]):
        def __init__(self, function: Callable[[], T], name: str, logged: bool = True, timeout: float | None = None):
            """
            :param function: the function representing the task to execute
            :param name: the name of the task
            :param logged: whether to log management of the task; if False, only errors will be logged
            :param timeout: the maximum time to wait for task completion in seconds, or None to wait indefinitely
            """
            self.name = name
            self.future: concurrent.futures.Future = concurrent.futures.Future()
            self.logged = logged
            self.timeout = timeout
            self._function = function
            self._started = threading.Event()
            self._thread: Thread | None = None

        def _tostring_includes(self) -> list[str]:
            return ["name"]

        def start(self) -> None:
            """
            Executes the task in a separate thread, setting the result or exception on the future.
            """

            def run_task() -> None:
                self._started.set()
                try:
                    if self.future.done():
                        if self.logged:
                            log.info(f"Task {self.name} was already completed/cancelled; skipping execution")
                        return
                    with LogTime(self.name, logger=log, enabled=self.logged):
                        result = self._function()
                        if not self.future.done():
                            self.future.set_result(result)
                except Exception as e:
                    if not self.future.done():
                        log.error(f"Error during execution of {self.name}: {e}", exc_info=e)
                        self.future.set_exception(e)

            self._thread = Thread(target=run_task, name=self.name)
            self._thread.start()

        def is_done(self) -> bool:
            """
            :return: whether the task has completed (either successfully, with failure, or via cancellation)
            """
            return self.future.done()

        def result(self, timeout: float | None = None, cancel_on_timeout: bool = True) -> T:
            """
            Blocks until the task is done or the timeout is reached, and returns the result.
            If an exception occurred during task execution, it is raised here.
            If the timeout is reached, a TimeoutError is raised.
            If the task is cancelled, a CancelledError is raised.

            :param timeout: the maximum execution time in seconds, counted from the start of the task's execution
                (not including the time it waits for earlier tasks); if None, wait indefinitely
            :param cancel_on_timeout: whether to cancel the task if the timeout is reached.
                If the task has not yet started, cancellation prevents its execution entirely;
                if it is already running, the underlying thread continues to run, but its result
                is discarded and any waiter will receive a CancelledError.
            :return: the result of the task
            """
            try:
                if timeout is not None:
                    while not self._started.wait(0.1) and not self.future.done():
                        pass
                return self.future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                if cancel_on_timeout:
                    self.cancel()
                raise

        def cancel(self) -> None:
            """
            Cancels the task. If it has not yet started, it will not be executed.
            If it has already started, its future will be marked as cancelled and will raise a CancelledError
            when its result is requested.
            """
            self.future.cancel()

        def wait_until_finished(self, grace_period: float) -> bool:
            """
            Waits until the task's execution has finished, i.e. until its thread has terminated, but no longer than
            the task's timeout plus the given grace period.
            A task that timed out (or was cancelled) while running is thus still waited for, so that the next task
            does not run concurrently with it.

            :param grace_period: the time, in seconds, to wait beyond the task's timeout
            :return: True if the execution has finished, False if the task is still running
            """
            if self._thread is None:
                return True
            self._thread.join(timeout=None if self.timeout is None else self.timeout + grace_period)
            return not self._thread.is_alive()

    def _process_task_queue(self) -> None:
        while True:
            # obtain task from the queue
            task: TaskExecutor.Task | None = None
            with self._task_executor_lock:
                if len(self._task_executor_queue) > 0:
                    task = self._task_executor_queue.pop(0)
            if task is None:
                time.sleep(0.1)
                continue

            # start task execution asynchronously
            with self._task_executor_lock:
                self._task_executor_current_task = task
            if task.logged:
                log.info("Starting execution of %s", task.name)
            task.start()

            # wait for task completion
            if not task.wait_until_finished(self.GRACE_PERIOD_AFTER_TIMEOUT):
                log.error(
                    "Task %s is still running %s seconds after its timeout of %s seconds; continuing with the next task",
                    task.name,
                    self.GRACE_PERIOD_AFTER_TIMEOUT,
                    task.timeout,
                )
            with self._task_executor_lock:
                self._task_executor_current_task = None

    def issue_task(self, task: Callable[[], T], name: str | None = None, logged: bool = True, timeout: float | None = None) -> Task[T]:
        """
        Issue a task to the executor for asynchronous execution.
        It is ensured that tasks are executed in the order they are issued, one after another.

        :param task: the task to execute
        :param name: the name of the task for logging purposes; if None, use the task function's name
        :param logged: whether to log management of the task; if False, only errors will be logged
        :param timeout: the maximum time to wait for task completion in seconds, or None to wait indefinitely
        :return: the task object, through which the task's future result can be accessed
        """
        with self._task_executor_lock:
            if logged:
                task_prefix_name = f"Task-{self._task_executor_task_index}"
                self._task_executor_task_index += 1
            else:
                task_prefix_name = "BackgroundTask"
            task_name = f"{task_prefix_name}:{name or getattr(task, '__name__', 'task')}"
            if logged:
                log.info(f"Scheduling {task_name}")
            task_obj = self.Task(function=task, name=task_name, logged=logged, timeout=timeout)
            self._task_executor_queue.append(task_obj)
            return task_obj

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
        task_obj = self.issue_task(task, name=name, logged=logged, timeout=timeout)
        return task_obj.result(timeout=timeout)

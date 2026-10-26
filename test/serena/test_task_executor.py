import time

import pytest

from serena.task_executor import TaskExecutor


@pytest.fixture
def executor():
    """
    Fixture for a basic SerenaAgent without a project
    """
    return TaskExecutor("TestExecutor")


class Task:
    def __init__(self, delay: float, exception: bool = False):
        self.delay = delay
        self.exception = exception
        self.did_run = False

    def run(self):
        self.did_run = True
        time.sleep(self.delay)
        if self.exception:
            raise ValueError("Task failed")
        return True


def test_task_executor_sequence(executor):
    """
    Tests that a sequence of tasks is executed correctly
    """
    future1 = executor.issue_task(Task(1).run, name="task1")
    future2 = executor.issue_task(Task(1).run, name="task2")
    assert future1.result() is True
    assert future2.result() is True


def test_task_executor_exception(executor):
    """
    Tests that tasks that raise exceptions are handled correctly, i.e. that
      * the exception is propagated,
      * subsequent tasks are still executed.
    """
    future1 = executor.issue_task(Task(1, exception=True).run, name="task1")
    future2 = executor.issue_task(Task(1).run, name="task2")
    have_exception = False
    try:
        assert future1.result()
    except Exception as e:
        assert isinstance(e, ValueError)
        have_exception = True
    assert have_exception
    assert future2.result() is True


def test_task_executor_cancel_current(executor):
    """
    Tests that a task that is cancelled while running raises CancelledError when result() is called, and that
    subsequent tasks are still executed (once the cancelled task's execution has ended).
    """
    future1 = executor.issue_task(Task(2).run, name="task1")
    future2 = executor.issue_task(Task(0.1).run, name="task2")
    time.sleep(0.5)
    future1.cancel()
    assert future2.result() is True
    with pytest.raises(Exception) as exc_info:
        future1.result()
    assert exc_info.value.__class__.__name__ == "CancelledError"


def test_task_executor_cancel_future(executor):
    """
    Tests that when a future task is cancelled, it is never run at all
    """
    task1 = Task(10)
    task2 = Task(1)
    future1 = executor.issue_task(task1.run, name="task1")
    future2 = executor.issue_task(task2.run, name="task2")
    time.sleep(1)
    future2.cancel()
    future1.cancel()
    try:
        future2.result()
    except:
        pass
    assert task1.did_run
    assert not task2.did_run


def test_timeout_counts_from_the_start_of_execution(executor):
    """
    A task that waits behind a long-running task must get its full timeout once it runs (with a shared executor,
    the long-running task may belong to another session).
    """
    executor.issue_task(Task(1.0).run, name="long")
    quick = executor.issue_task(Task(0.1).run, name="quick", timeout=0.5)

    assert quick.result(timeout=0.5) is True


def test_a_timed_out_task_finishes_before_the_next_task_starts(executor):
    events: list[tuple[str, float]] = []

    def slow() -> None:
        time.sleep(0.6)
        events.append(("slow finished", time.monotonic()))

    def next_task() -> None:
        events.append(("next started", time.monotonic()))

    timed_out = executor.issue_task(slow, name="slow", timeout=0.1)
    following = executor.issue_task(next_task, name="next")
    with pytest.raises(TimeoutError):
        timed_out.result(timeout=0.1)
    following.result()

    assert [name for name, _ in events] == ["slow finished", "next started"]

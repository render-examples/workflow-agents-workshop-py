"""
Workflow modules. Each module defines its own `Workflows` app and decorates
its tasks in place. `loader.py` auto-discovers the modules; `workflow.py`
merges their apps with `Workflows.from_workflows` and starts the runner.

Every task takes a `TaskContext` as its first parameter. `@app.task` returns
a `TaskDefinition`, which is not callable — reach a subtask through
`await ctx.run(definition, *args)`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from render import TaskContext

if TYPE_CHECKING:
    from render.workflows import TaskDefinition

P = ParamSpec("P")
R = TypeVar("R")


class LocalTaskContext(TaskContext):
    """
    A TaskContext for running tasks in this process, without a Render
    workflow environment.

    `run` calls the target task's function directly instead of dispatching it
    to its own instance, so the whole workflow runs inline. The gateway's
    in-process mode and the tests use it.
    """

    async def run(
        self, task: TaskDefinition[P, R], *args: P.args, **kwargs: P.kwargs
    ) -> R:
        result: Any = task.func(self, *args, **kwargs)
        if hasattr(result, "__await__"):
            return await result
        return result


local_task_context = LocalTaskContext()

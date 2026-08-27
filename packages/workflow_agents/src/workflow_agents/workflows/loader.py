"""
Auto-discover workflows from the workflows/ directory.

Convention: each workflows/{name}.py defines a `Workflows` app (its tasks
decorated in place) and a `workflow_task` entry point whose task name matches
the module name. The module name becomes the route name and the Render slug
is derived as {service_name}/{module_name}.

The gateway uses `mapping` to dispatch and `local_tasks` to run entry points
in-process (through a LocalTaskContext, so subtasks run inline too). The
runner (workflow.py) merges `apps` into the single app it starts.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from render.workflows import TaskDefinition

from . import local_task_context

SKIP = {"loader", "__init__"}


class DiscoveredWorkflows:
    __slots__ = ("mapping", "local_tasks", "apps")

    def __init__(self) -> None:
        self.mapping: dict[str, str] = {}
        self.local_tasks: dict[str, Callable[[Any], Awaitable[Any]]] = {}
        self.apps: list[Any] = []


def load_workflows(
    package_path: str | None = None,
    workflow_slug: str = "workflow-agents",
) -> DiscoveredWorkflows:
    if package_path is None:
        package_path = str(Path(__file__).parent)

    result = DiscoveredWorkflows()

    for _importer, modname, _ispkg in pkgutil.iter_modules([package_path]):
        if modname in SKIP:
            continue
        mod = importlib.import_module(
            f".{modname}", package="workflow_agents.workflows"
        )
        definition = _find_task_export(mod)
        if definition:
            result.mapping[modname] = f"{workflow_slug}/{modname}"
            result.local_tasks[modname] = _local_runner(definition)
        app = getattr(mod, "app", None)
        if app is not None:
            result.apps.append(app)

    return result


def _local_runner(definition: TaskDefinition) -> Callable[[Any], Awaitable[Any]]:
    """Run an entry point and its subtasks in this process."""

    async def run(input: Any) -> Any:
        return await local_task_context.run(definition, input)

    return run


def _find_task_export(mod: Any) -> TaskDefinition | None:
    """Find the module's entry point. @app.task returns a TaskDefinition."""
    for attr_name in ("workflow_task", "task", "run"):
        val = getattr(mod, attr_name, None)
        if isinstance(val, TaskDefinition):
            return val
    return None

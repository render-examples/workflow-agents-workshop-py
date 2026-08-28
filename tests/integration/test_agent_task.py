"""Test that an agent wrapped in a Render task runs in-process."""

import os

import pytest

os.environ.setdefault("AGENT_MODEL", "mock")

from render import TaskContext, Workflows
from workflow_agents.workflows import local_task_context
from workshop_agent import security_reviewer
from workshop_agent.types import RunContext
from workshop_db import store_tracer


@pytest.mark.asyncio
async def test_agent_runs_in_process():
    result = await security_reviewer.run(
        {"patches": [{"file": "a.ts", "diff": "+x"}]},
    )
    assert isinstance(result.text, str)
    assert len(result.text) > 0
    assert isinstance(result.usage.input_tokens, int)


@pytest.mark.asyncio
async def test_agent_accepts_optional_run_id():
    result = await security_reviewer.run(
        {"patches": [{"file": "a.ts", "diff": "+x"}]},
        RunContext(run_id="test-run-id"),
    )
    assert isinstance(result.text, str)


@pytest.mark.asyncio
async def test_task_runs_through_a_local_context():
    """A task definition is not callable — local_task_context runs its func."""
    app = Workflows()

    @app.task(name="security")
    async def security_task(
        ctx: TaskContext, patches: list[dict[str, str]], run_id: str | None = None
    ) -> str:
        result = await security_reviewer.run(
            {"patches": patches}, RunContext(tracer=store_tracer(), run_id=run_id)
        )
        return result.text

    assert not callable(security_task)

    text = await local_task_context.run(
        security_task, [{"file": "a.ts", "diff": "+x"}], "test-run-id"
    )
    assert isinstance(text, str)
    assert len(text) > 0

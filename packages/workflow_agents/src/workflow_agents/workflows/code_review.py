"""
Code-review workflow — a root task that fans out per-agent subtasks.

Every task is defined right here: this module's `app` is merged into the
runner by workflow.py, so the @app.task functions below register
automatically. Under the workflow runtime the reviewers and judge run as
their own subtask runs — own instance, own retries, own trace node —
because `ctx.run` dispatches them. In tests and the gateway's in-process
mode the context is a LocalTaskContext, which calls them inline.

The agents come from workshop_agent — identical to the ones naive_agent
and worker_agents run.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from render import Retry, TaskContext, Workflows
from workshop_agent import (
    filter_diff,
    has_frontend_files,
    judge,
    performance_reviewer,
    prepare_diff,
    security_reviewer,
    to_review_summary,
    ux_reviewer,
)
from workshop_agent.prepare_diff import PullRequest
from workshop_agent.types import AgentResult, RunContext, TokenUsage
from workshop_db import persist_review, set_review_result, store_tracer
from workshop_db.types import ReviewResultUpdate

app = Workflows(
    default_retry=Retry(max_retries=2, wait_duration_ms=1000, backoff_scaling=2),
    default_timeout=120,
)


def _agent_context(run_id: str | None = None) -> RunContext:
    return RunContext(tracer=store_tracer(), run_id=run_id)


def _to_dict(result: AgentResult) -> dict[str, Any]:
    return {
        "text": result.text,
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        },
    }


@app.task(name="security")
async def security_task(
    ctx: TaskContext, patches: list[dict[str, str]], run_id: str | None = None
) -> dict[str, Any]:
    return _to_dict(await security_reviewer.run({"patches": patches}, _agent_context(run_id)))


@app.task(name="performance")
async def performance_task(
    ctx: TaskContext, patches: list[dict[str, str]], run_id: str | None = None
) -> dict[str, Any]:
    return _to_dict(await performance_reviewer.run({"patches": patches}, _agent_context(run_id)))


@app.task(name="ux")
async def ux_task(
    ctx: TaskContext, patches: list[dict[str, str]], run_id: str | None = None
) -> dict[str, Any]:
    return _to_dict(await ux_reviewer.run({"patches": patches}, _agent_context(run_id)))


@app.task(name="judge")
async def judge_task(
    ctx: TaskContext, findings: list[dict[str, str]], run_id: str | None = None
) -> dict[str, Any]:
    return _to_dict(await judge.run({"findings": findings}, _agent_context(run_id)))


REVIEWER_TASKS = {
    "security": security_task,
    "performance": performance_task,
    "ux": ux_task,
}


# The root waits on every subtask, so it overrides the app-level default.
@app.task(name="code_review", timeout_seconds=600)
async def workflow_task(ctx: TaskContext, input: dict[str, Any]) -> dict[str, Any]:
    """The code-review workflow entry point (the root task)."""
    url = input["url"]
    labels = input.get("labels", [])
    run_id = input.get("_runId")

    try:
        all_patches = await prepare_diff(PullRequest(url=url, labels=labels))
        filtered = filter_diff(all_patches)
        patches = [{"file": p.file, "diff": p.diff} for p in filtered.patches]

        reviewer_names = ["security", "performance"]
        if has_frontend_files(patches):
            reviewer_names.append("ux")

        raw_results = await asyncio.gather(
            *[ctx.run(REVIEWER_TASKS[name], patches, run_id) for name in reviewer_names]
        )
        reviewer_results = []
        for name, raw in zip(reviewer_names, raw_results, strict=True):
            reviewer_results.append({
                "agent": name,
                "note": raw["text"],
                "usage": raw["usage"],
            })

        findings = [{"agent": r["agent"], "note": r["note"]} for r in reviewer_results]
        decision_raw = await ctx.run(judge_task, findings, run_id)

        judge_result = AgentResult(
            text=decision_raw["text"],
            usage=TokenUsage(**decision_raw["usage"]),
        )
        summary = to_review_summary(reviewer_results, judge_result)
        if isinstance(run_id, str) and os.environ.get("RENDER_SDK_MODE") == "run":
            await persist_review(run_id, summary)

        return {
            "verdict": summary.verdict,
            "reason": summary.reason,
            "reviews": [{"agent": r.agent, "note": r.note} for r in summary.reviews],
            "usage": {
                "inputTokens": summary.usage.input_tokens,
                "outputTokens": summary.usage.output_tokens,
            },
        }
    except Exception as err:
        if isinstance(run_id, str) and os.environ.get("RENDER_SDK_MODE") == "run":
            await set_review_result(
                run_id,
                ReviewResultUpdate(status="error", reason=str(err)),
            )
        raise

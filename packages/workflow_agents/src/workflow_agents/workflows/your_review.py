"""
YOUR REVIEW — a sandbox workflow with a working define_agent() example.

This module is *yours* to experiment with. The loader auto-discovers it as
the `your_review` workflow, and workflow.py merges this module's `app` into
the runner — no registration step. Run it, break it, extend it, compare
traces against the finished code_review workflow next door.

The starter below defines a custom agent using define_agent() and runs it
as a task. This is the full anatomy of an agent: a name, a model tier, a
system prompt, and a set of tools from the shared registry. Modify it
however you like — the ideas at the bottom of the file are starting points.

See workshop/participants/04-author-a-task.md for a walkthrough.
"""

from __future__ import annotations

from typing import Any

from render import Retry, TaskContext, Workflows
from workshop_agent import (
    define_agent,
    extensions,
    filter_diff,
    overview,
    prepare_diff,
)
from workshop_agent.model_tiers import resolve_model_spec
from workshop_agent.prepare_diff import PullRequest
from workshop_agent.types import AgentDefinition, RunContext
from workshop_db import store_tracer

# ---------------------------------------------------------------------------
# 1. Define a custom agent
#
# This is the full anatomy — the same shape as security_reviewer et al. in
# shared/agent/src/workshop_agent/agents.py, just defined inline here so you
# can see every field and tweak them:
#
#   name          — shows up in traces and logs
#   model         — resolve_model_spec("small" | "medium" | "large")
#   tools         — tool names from the shared registry (see shared/agent/tools/)
#   system_prompt — what the agent focuses on
#
# Try changing these:
#   • Swap the system prompt to focus on documentation, naming, or testing
#   • Change "medium" to "small" (faster, cheaper) or "large" (more capable)
#   • Add another tool: "contrast_ratio", "current_time"
#   • Remove tools entirely to see a tool-free review
# ---------------------------------------------------------------------------

my_reviewer = define_agent(AgentDefinition(
    name="my-reviewer",
    model=resolve_model_spec("medium"),
    tools=["diff_stats"],
    system_prompt="""# Code clarity reviewer

You review a pull request's per-file patches for clarity and maintainability.

Focus on:
- Confusing control flow or deeply nested logic
- Missing or misleading names (variables, functions, types)
- Dead code or unreachable branches
- Overly broad exception handling that hides bugs

Use `diff_stats` on large hunks to quantify the scope of a change before
commenting on complexity.

## Output format

Return a short list of findings. Each finding has:
- **severity**: `info` | `warn` | `block`
- **location**: `path/to/file:line`
- **note**: 1–3 sentences. State the problem and a suggested fix.

If you find nothing, say so explicitly.""",
))


# ---------------------------------------------------------------------------
# 2. Wrap it as a task with retry config
#
# @app.task registers it automatically when workflow.py merges apps.
# Retry gives you durable execution: if the LLM call fails, Render retries
# in a fresh instance — no try/except or dead-letter queue.
#
# Every task takes a TaskContext first, then its own inputs. @app.task returns
# a definition, not a callable — run it with await ctx.run(definition, ...).
# ---------------------------------------------------------------------------

app = Workflows(
    default_retry=Retry(max_retries=2, wait_duration_ms=1000, backoff_scaling=2),
    default_timeout=120,
)


@app.task(name="my_reviewer", timeout_seconds=120)
async def my_reviewer_task(
    ctx: TaskContext, patches: list[dict[str, str]], run_id: str | None = None,
) -> dict[str, Any]:
    agent_context = RunContext(tracer=store_tracer(), run_id=run_id)
    result = await my_reviewer.run({"patches": patches}, agent_context)
    return {
        "text": result.text,
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        },
    }


# ---------------------------------------------------------------------------
# 3. The workflow entry point — fetch diff, run the agent, return findings
# ---------------------------------------------------------------------------

@app.task(name="your_review", timeout_seconds=300)
async def workflow_task(ctx: TaskContext, input: dict[str, Any]) -> dict[str, Any]:
    """The your-review workflow entry point."""
    url = input["url"]
    run_id = input.get("_runId")

    all_patches = await prepare_diff(PullRequest(url=url, labels=[]))
    filtered = filter_diff(all_patches)
    patches_dicts = [{"file": p.file, "diff": p.diff} for p in filtered.patches]

    result = await ctx.run(my_reviewer_task, patches_dicts, run_id)

    return {
        "url": url,
        "overview": overview(patches_dicts),
        "extensions": extensions(patches_dicts),
        "dropped": filtered.dropped,
        "review": result["text"],
        "usage": result["usage"],
    }


# -- Ideas to explore ----------------------------------------------------------
#
# You now have a working agent + task. Here are ways to extend it:
#
# ▸ Change the system prompt focus
#   Swap "clarity and maintainability" for documentation coverage, test
#   quality, or API design. The agent structure stays the same.
#
# ▸ Add a second agent and fan out with asyncio.gather
#   Define another agent (e.g. a naming reviewer), wrap it as a task, and
#   run both in parallel:
#
#     import asyncio
#     results = await asyncio.gather(
#         ctx.run(my_reviewer_task, patches_dicts, run_id),
#         ctx.run(naming_reviewer_task, patches_dicts, run_id),
#     )
#
# ▸ Add a judge step
#   Import the judge from code_review.py or define your own. Feed the
#   reviewer findings into it and return a verdict:
#
#     from .code_review import judge_task
#     findings = [{"agent": "my-reviewer", "note": result["text"]}]
#     decision = await ctx.run(judge_task, findings, run_id)
#
# ▸ Try different tools
#   The registry has: scan_for_secrets, diff_stats, contrast_ratio,
#   current_time. Add them to the agent's tools list, or drop tools
#   entirely for a tool-free review.
#
# ▸ Force a failure to see retry behavior
#   Add this at the top of workflow_task:
#
#     import random
#     if random.random() < 0.5:
#         raise RuntimeError("flaky!")
#
#   Watch Render retry in a fresh instance. Remove it when done.
#
# ▸ Use a built-in reviewer instead of (or alongside) your custom one
#   The per-agent tasks from code_review.py are already registered:
#
#     from .code_review import security_task
#     review = await ctx.run(security_task, patches_dicts, run_id)
#
# ---------------------------------------------------------------------------

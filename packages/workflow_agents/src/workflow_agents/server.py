"""
Pattern 3 — gateway (web service).

A FastAPI server that turns inbound PR submissions into Render Workflow runs,
and serves the shared telemetry viewer.

In local dev (RENDER_USE_LOCAL_DEV=true), workflows run in-process.
In production the Render SDK dispatches real Workflow task runs.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from workshop_db import create_review, get_review, migrate, persist_review, set_review_result
from workshop_db.types import ReviewMeta, ReviewResultUpdate
from workshop_ui import create_ui_router

from .workflows.loader import load_workflows

_background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]


async def create_app() -> FastAPI:
    is_local_dev = os.environ.get("RENDER_USE_LOCAL_DEV") == "true"
    local_dev_url = (os.environ.get("RENDER_LOCAL_DEV_URL") or "").strip()
    use_in_process = is_local_dev and not local_dev_url
    workflow_slug = (os.environ.get("RENDER_WORKFLOW_SLUG") or "").strip()
    is_production = os.environ.get("NODE_ENV") == "production"
    if is_production and not local_dev_url and not workflow_slug:
        raise RuntimeError(
            "RENDER_WORKFLOW_SLUG is required when dispatching workflow runs"
        )

    discovered = load_workflows(workflow_slug=workflow_slug or "workflow-agents")

    async def run_workflow(name: str, input: Any) -> Any:
        if use_in_process:
            fn = discovered.local_tasks.get(name)
            if not fn:
                raise ValueError(f'no local task for workflow "{name}"')
            return await fn(input)
        slug = discovered.mapping.get(name)
        if not slug:
            raise ValueError(f'unknown workflow "{name}"')
        from render import RenderAsync

        client = RenderAsync(token=os.environ.get("RENDER_API_KEY"))
        result = await client.workflows.run_task(slug, [input])
        return result.results[0] if isinstance(result.results, list) else result.results

    async def run_review_workflow(
        pr_url: str,
        labels: list[str] | None = None,
        workflow_name: str = "code_review",
    ) -> str:
        review_id = await create_review(
            pr_url,
            meta=ReviewMeta(source="workflow-agents", workflow=workflow_name),
        )

        async def _run() -> None:
            try:
                result = await run_workflow(
                    workflow_name,
                    {
                        "url": pr_url,
                        "labels": labels or [],
                        "_runId": review_id,
                    },
                )
                if not isinstance(result, dict):
                    result = {}
                review = await get_review(review_id)
                if review and review.status != "running":
                    return
                if isinstance(result.get("reviews"), list) and isinstance(
                    result.get("verdict"), str
                ):
                    from workshop_agent.helpers import ReviewFinding, ReviewSummary
                    from workshop_agent.types import TokenUsage

                    usage_raw = result.get("usage", {})
                    await persist_review(
                        review_id,
                        ReviewSummary(
                            verdict=result["verdict"],
                            reason=result.get("reason", ""),
                            reviews=[
                                ReviewFinding(agent=r["agent"], note=r["note"])
                                for r in result["reviews"]
                            ],
                            usage=TokenUsage(
                                input_tokens=usage_raw.get("inputTokens", 0),
                                output_tokens=usage_raw.get("outputTokens", 0),
                            ),
                        ),
                    )
                else:
                    await set_review_result(
                        review_id,
                        ReviewResultUpdate(
                            status="done",
                            verdict=result.get("verdict"),
                            reason=result.get("reason") or json.dumps(result, indent=2),
                            input_tokens=result.get("usage", {}).get("inputTokens", 0),
                            output_tokens=result.get("usage", {}).get("outputTokens", 0),
                        ),
                    )
            except Exception as err:
                print(f"[workflow-agents] review {review_id} failed: {err}")
                try:
                    await set_review_result(
                        review_id, ReviewResultUpdate(status="error", reason=str(err))
                    )
                except Exception as persist_err:
                    print(
                        f"[workflow-agents] failed to persist error for review "
                        f"{review_id}: {persist_err}"
                    )

        task = asyncio.create_task(_run())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return review_id

    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/workflows")
    async def get_workflows() -> list[str]:
        return list(discovered.mapping.keys())

    @app.post("/api/reviews")
    async def post_review(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        pr_url = body.get("prUrl")
        if not pr_url:
            return JSONResponse({"error": "prUrl is required"}, status_code=400)

        workflow_name = body.get("workflow", "code_review")
        if workflow_name not in discovered.mapping:
            return JSONResponse(
                {"error": f'workflow "{workflow_name}" not available'},
                status_code=503,
            )

        review_id = await run_review_workflow(pr_url, [], workflow_name)
        return JSONResponse({"id": review_id}, status_code=202)

    app.include_router(create_ui_router("localhost Workshop: Workflow Agents"))

    dispatch_mode = (
        "in-process"
        if use_in_process
        else f"local-dev-server ({local_dev_url})"
        if local_dev_url
        else "render"
    )
    print(
        f"[workflow-agents] workflows: {', '.join(discovered.mapping.keys())} "
        f"(dispatch: {dispatch_mode})"
    )

    return app


if __name__ == "__main__":
    import uvicorn

    async def main() -> None:
        await migrate()
        application = await create_app()
        port = int(os.environ.get("PORT", "3000"))
        config = uvicorn.Config(application, host="0.0.0.0", port=port)
        server = uvicorn.Server(config)
        await server.serve()

    asyncio.run(main())

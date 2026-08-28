"""
Render Workflows runner.

Each module in workflows/ defines its own `Workflows` app with its tasks
decorated in place. The loader discovers them, and this module merges them
into the single app the Render runtime starts — so adding a workflow module
needs no registration step here.

Usage:
  Local dev:   render workflows dev -- uv run python -m workflow_agents.workflow
  Production:  uv run python -m workflow_agents.workflow  (start command on Render)
"""

from __future__ import annotations

from render import Workflows

from .workflows.loader import load_workflows

app = Workflows.from_workflows(*load_workflows().apps)

if __name__ == "__main__":
    app.start()

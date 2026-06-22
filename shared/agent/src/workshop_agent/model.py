"""
Model adapters. Provider-agnostic ModelClient with one `complete` call.
Each adapter talks to its provider's API directly over httpx (no vendor SDK).

When no API key is configured (or AGENT_MODEL=mock), we return a deterministic
MockClient so the entire pipeline runs offline.

If the default provider's key is missing but another provider's key is present,
we switch providers automatically so that setting only OPENAI_API_KEY (or only
ANTHROPIC_API_KEY) still produces a real review.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from .types import (
    CompleteArgs,
    CompleteResult,
    ContentBlock,
    Message,
    MessageRole,
    ModelClient,
    ModelSpec,
    Provider,
    TextBlock,
    TokenUsage,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
)

DEFAULT_MAX_OUTPUT_TOKENS = 4096


def _client_for_provider(model: ModelSpec) -> ModelClient:
    if model.provider == Provider.ANTHROPIC:
        return AnthropicClient(model)
    if model.provider == Provider.OPENAI:
        return OpenAIClient(model)
    raise ValueError(f'unknown model provider "{model.provider}"')


def resolve_client(model: ModelSpec) -> ModelClient:
    if os.environ.get("AGENT_MODEL") == "mock" or model.provider == Provider.MOCK:
        return MockClient()

    key_env = model.api_key_env or (
        "OPENAI_API_KEY" if model.provider == Provider.OPENAI else "ANTHROPIC_API_KEY"
    )
    if os.environ.get(key_env):
        return _client_for_provider(model)

    alt_provider = Provider.ANTHROPIC if model.provider == Provider.OPENAI else Provider.OPENAI
    alt_key_env = "OPENAI_API_KEY" if alt_provider == Provider.OPENAI else "ANTHROPIC_API_KEY"
    if os.environ.get(alt_key_env):
        import warnings
        warnings.warn(
            f"[agent] no {key_env} set but {alt_key_env} found — using {alt_provider.value} provider"
        )
        return _client_for_provider(ModelSpec(
            provider=alt_provider, model=model.model, base_url=model.base_url,
        ))

    import warnings
    warnings.warn(f"[agent] no {key_env} set — falling back to mock model client")
    return MockClient()


# -- Mock --------------------------------------------------------------------

class MockClient:
    async def complete(self, args: CompleteArgs) -> CompleteResult:
        is_judge = bool(re.search(r"judge", args.system, re.IGNORECASE))
        if is_judge:
            text = json.dumps({
                "verdict": "approve",
                "reason": "Mock judge: no blocking findings (set an API key for a real review).",
                "findings": [],
            })
        else:
            text = "\n".join([
                "- **severity**: info",
                "- **location**: (mock)",
                "- **note**: Mock review. Set ANTHROPIC_API_KEY or OPENAI_API_KEY for a real review.",
            ])
        return CompleteResult(
            content=[TextBlock(text=text)],
            usage=TokenUsage(input_tokens=0, output_tokens=0),
            stop_reason="end_turn",
        )


# -- Anthropic ---------------------------------------------------------------

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicClient:
    def __init__(self, model: ModelSpec) -> None:
        self._model = model

    async def complete(self, args: CompleteArgs) -> CompleteResult:
        key_env = self._model.api_key_env or "ANTHROPIC_API_KEY"
        api_key = os.environ.get(key_env)
        if not api_key:
            raise RuntimeError("Anthropic API key missing")

        body: dict[str, Any] = {
            "model": args.model.model,
            "system": args.system,
            "max_tokens": (args.sampling.max_output_tokens if args.sampling else None)
            or DEFAULT_MAX_OUTPUT_TOKENS,
            "messages": [_to_anthropic_message(m) for m in args.messages],
        }
        if args.sampling and args.sampling.temperature is not None:
            body["temperature"] = args.sampling.temperature
        if args.tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in args.tools
            ]

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._model.base_url or ANTHROPIC_URL,
                headers={
                    "content-type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                },
                json=body,
                timeout=120.0,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"Anthropic API {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        return CompleteResult(
            content=[_from_anthropic_block(b) for b in data["content"]],
            usage=TokenUsage(
                input_tokens=data.get("usage", {}).get("input_tokens", 0),
                output_tokens=data.get("usage", {}).get("output_tokens", 0),
            ),
            stop_reason=data.get("stop_reason", "end_turn"),
        )


def _to_anthropic_message(msg: Message) -> dict[str, Any]:
    # Anthropic's API places tool results in user-role messages (no dedicated "tool" role).
    role = "assistant" if msg.role == MessageRole.ASSISTANT else "user"
    return {"role": role, "content": [_to_anthropic_block(b) for b in msg.content]}


def _to_anthropic_block(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        result: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
        }
        if block.is_error:
            result["is_error"] = True
        return result
    return {"type": "text", "text": str(block)}


def _from_anthropic_block(block: dict[str, Any]) -> ContentBlock:
    if block.get("type") == "text":
        return TextBlock(text=block["text"])
    if block.get("type") == "tool_use":
        return ToolUseBlock(id=block["id"], name=block["name"], input=block.get("input"))
    return TextBlock(text=json.dumps(block))


# -- OpenAI ------------------------------------------------------------------

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIClient:
    def __init__(self, model: ModelSpec) -> None:
        self._model = model

    async def complete(self, args: CompleteArgs) -> CompleteResult:
        key_env = self._model.api_key_env or "OPENAI_API_KEY"
        api_key = os.environ.get(key_env)
        if not api_key:
            raise RuntimeError("OpenAI API key missing")

        messages: list[dict[str, Any]] = [{"role": "system", "content": args.system}]
        for msg in args.messages:
            messages.extend(_to_openai_messages(msg))

        body: dict[str, Any] = {
            "model": args.model.model,
            "messages": messages,
            "max_tokens": (args.sampling.max_output_tokens if args.sampling else None)
            or DEFAULT_MAX_OUTPUT_TOKENS,
        }
        if args.sampling and args.sampling.temperature is not None:
            body["temperature"] = args.sampling.temperature
        if args.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in args.tools
            ]

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._model.base_url or OPENAI_URL,
                headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {api_key}",
                },
                json=body,
                timeout=120.0,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"OpenAI API {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        choice = data.get("choices", [{}])[0]
        if not choice:
            raise RuntimeError("OpenAI returned no choices")

        return CompleteResult(
            content=_from_openai_choice(choice),
            usage=TokenUsage(
                input_tokens=data.get("usage", {}).get("prompt_tokens", 0),
                output_tokens=data.get("usage", {}).get("completion_tokens", 0),
            ),
            stop_reason=choice.get("finish_reason", "stop"),
        )


def _to_openai_messages(msg: Message) -> list[dict[str, Any]]:
    if msg.role == MessageRole.ASSISTANT:
        text_parts = [b.text for b in msg.content if isinstance(b, TextBlock)]
        tool_calls = [
            {
                "id": b.id,
                "type": "function",
                "function": {"name": b.name, "arguments": json.dumps(b.input)},
            }
            for b in msg.content
            if isinstance(b, ToolUseBlock)
        ]
        result: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
        if tool_calls:
            result["tool_calls"] = tool_calls
        return [result]

    if msg.role == MessageRole.TOOL:
        return [
            {"role": "tool", "tool_call_id": b.tool_use_id, "content": b.content}
            for b in msg.content
            if isinstance(b, ToolResultBlock)
        ]

    text = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
    return [{"role": "user", "content": text}]


def _from_openai_choice(choice: dict[str, Any]) -> list[ContentBlock]:
    blocks: list[ContentBlock] = []
    message = choice.get("message", {})
    if message.get("content"):
        blocks.append(TextBlock(text=message["content"]))
    for tc in message.get("tool_calls", []):
        try:
            inp = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            inp = tc.get("function", {}).get("arguments", "")
        blocks.append(ToolUseBlock(id=tc["id"], name=tc["function"]["name"], input=inp))
    if not blocks:
        blocks.append(TextBlock(text=""))
    return blocks

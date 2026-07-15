"""Shared callable-policy adapter for the homogeneous Research Agent runtime."""
from __future__ import annotations

import asyncio
import inspect
from typing import Any


async def call_policy(
    policy: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """Call sync or async OpenAI-compatible policies without sharing call state."""
    try:
        signature = inspect.signature(policy)
    except (TypeError, ValueError):
        accepts_tools = True
    else:
        accepts_tools = "tools" in signature.parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )

    kwargs = {"tools": tools} if accepts_tools else {}
    call = getattr(policy, "__call__", policy)
    if inspect.iscoroutinefunction(call):
        response = await policy(messages, **kwargs)
    else:
        response = await asyncio.to_thread(policy, messages, **kwargs)
        if inspect.isawaitable(response):
            response = await response

    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        dumped = response.model_dump()
        if isinstance(dumped, dict):
            return dumped
    raise TypeError("policy must return an OpenAI-compatible dict")

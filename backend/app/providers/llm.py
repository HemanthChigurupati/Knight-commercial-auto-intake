"""LLM providers behind one interface.

Every call asks the model to fill a named JSON schema through forced tool use, so the
pipeline always gets structured output (never free text it has to parse).

  BedrockLLM   -- Claude on Amazon Bedrock (Converse API)        [aws mode]
  AnthropicLLM -- Claude through the Anthropic API               [local mode]
  ReplayLLM    -- returns responses recorded from real runs      [demo mode]
  RecordingLLM -- wraps a real provider and saves each response for ReplayLLM
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import Settings


@dataclass
class Block:
    """A piece of user content: text, or an image file."""
    text: str | None = None
    image_path: str | None = None

    def media_type(self) -> str:
        suffix = Path(self.image_path or "").suffix.lower()
        return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif"}.get(suffix, "image/png")


@dataclass
class LLMResult:
    data: dict
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    replayed: bool = False


class LLMUnavailable(RuntimeError):
    """No usable model for this call (e.g. demo mode with no recorded response)."""


class LLMProvider(Protocol):
    name: str

    def complete_json(self, role: str, system: str, blocks: list[Block],
                      schema_name: str, schema: dict, max_tokens: int = 2000) -> LLMResult: ...


def cache_key(role: str, system: str, blocks: list[Block], schema_name: str) -> str:
    h = hashlib.sha256()
    for part in (role, system, schema_name):
        h.update(part.encode())
        h.update(b"\x00")
    for b in blocks:
        if b.text is not None:
            h.update(b"T" + b.text.encode())
        if b.image_path:
            h.update(b"I" + hashlib.sha256(Path(b.image_path).read_bytes()).digest())
    return h.hexdigest()[:32]


class BedrockLLM:
    name = "bedrock"

    def __init__(self, settings: Settings):
        import boto3  # imported lazily so demo mode needs no AWS SDK
        session = boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)
        self.client = session.client("bedrock-runtime")
        self.settings = settings

    def complete_json(self, role, system, blocks, schema_name, schema, max_tokens=2000):
        content = []
        for b in blocks:
            if b.text is not None:
                content.append({"text": b.text})
            if b.image_path:
                fmt = b.media_type().split("/")[1]
                content.append({"image": {"format": fmt,
                                          "source": {"bytes": Path(b.image_path).read_bytes()}}})
        model = self.settings.model_for(role)
        t0 = time.time()
        resp = self.client.converse(
            modelId=model,
            system=[{"text": system}],
            messages=[{"role": "user", "content": content}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
            toolConfig={
                "tools": [{"toolSpec": {"name": schema_name,
                                        "description": f"Record the {schema_name} result.",
                                        "inputSchema": {"json": schema}}}],
                "toolChoice": {"tool": {"name": schema_name}},
            },
        )
        data = next(c["toolUse"]["input"] for c in resp["output"]["message"]["content"]
                    if "toolUse" in c)
        usage = resp.get("usage", {})
        return LLMResult(data=data, model=model, input_tokens=usage.get("inputTokens", 0),
                         output_tokens=usage.get("outputTokens", 0),
                         latency_ms=int((time.time() - t0) * 1000))


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, settings: Settings):
        import anthropic  # lazy import
        self.client = anthropic.Anthropic()
        self.settings = settings

    def complete_json(self, role, system, blocks, schema_name, schema, max_tokens=2000):
        content = []
        for b in blocks:
            if b.text is not None:
                content.append({"type": "text", "text": b.text})
            if b.image_path:
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": b.media_type(),
                    "data": base64.b64encode(Path(b.image_path).read_bytes()).decode()}})
        model = self.settings.model_for(role)
        t0 = time.time()
        resp = self.client.messages.create(
            model=model, max_tokens=max_tokens, temperature=0, system=system,
            messages=[{"role": "user", "content": content}],
            tools=[{"name": schema_name, "description": f"Record the {schema_name} result.",
                    "input_schema": schema}],
            tool_choice={"type": "tool", "name": schema_name},
        )
        data = next(c.input for c in resp.content if c.type == "tool_use")
        return LLMResult(data=data, model=model, input_tokens=resp.usage.input_tokens,
                         output_tokens=resp.usage.output_tokens,
                         latency_ms=int((time.time() - t0) * 1000))


class ReplayLLM:
    name = "replay"

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir

    def complete_json(self, role, system, blocks, schema_name, schema, max_tokens=2000):
        path = self.cache_dir / f"{cache_key(role, system, blocks, schema_name)}.json"
        if not path.exists():
            raise LLMUnavailable(f"no recorded response for this {schema_name} call")
        rec = json.loads(path.read_text())
        return LLMResult(data=rec["data"], model=rec["model"], replayed=True)


class RecordingLLM:
    """Pass-through that saves every response so demo mode can replay the run exactly."""

    def __init__(self, inner: LLMProvider, cache_dir: Path):
        self.inner = inner
        self.cache_dir = cache_dir
        self.name = f"{inner.name}+record"
        cache_dir.mkdir(parents=True, exist_ok=True)

    def complete_json(self, role, system, blocks, schema_name, schema, max_tokens=2000):
        res = self.inner.complete_json(role, system, blocks, schema_name, schema, max_tokens)
        key = cache_key(role, system, blocks, schema_name)
        (self.cache_dir / f"{key}.json").write_text(json.dumps(
            {"schema": schema_name, "role": role, "model": res.model, "data": res.data}, indent=2))
        return res


def build_llm(settings: Settings) -> LLMProvider:
    if settings.mode == "demo":
        return ReplayLLM(settings.llm_cache_dir)
    inner: LLMProvider = BedrockLLM(settings) if settings.mode == "aws" else AnthropicLLM(settings)
    return RecordingLLM(inner, settings.llm_cache_dir) if settings.record_llm else inner

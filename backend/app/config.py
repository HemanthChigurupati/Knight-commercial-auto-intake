"""Runtime settings.

One switch -- APP_MODE -- decides which provider sits behind every interface:

  demo  : no credentials needed. Recorded LLM responses are replayed from
          fixtures/llm_cache, OCR runs locally with Tesseract, files stay on disk.
  local : real Claude calls through the Anthropic API, local OCR/storage.
  aws   : Bedrock (Claude), Textract, S3 and SQS in the configured region.

Every value can be overridden with an environment variable (see .env.example).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    mode: str = field(default_factory=lambda: _env("APP_MODE", "demo"))

    # Claude models, addressed by role so a model can be swapped in one place.
    # Defaults are the US cross-region inference profiles (processing stays in the US).
    model_classify: str = field(default_factory=lambda: _env(
        "MODEL_CLASSIFY", "us.anthropic.claude-haiku-4-5-20251001-v1:0"))
    model_extract: str = field(default_factory=lambda: _env(
        "MODEL_EXTRACT", "us.anthropic.claude-sonnet-4-6"))
    model_agent: str = field(default_factory=lambda: _env(
        "MODEL_AGENT", "us.anthropic.claude-sonnet-4-6"))
    model_analysis: str = field(default_factory=lambda: _env(
        "MODEL_ANALYSIS", "us.anthropic.claude-sonnet-4-6"))

    # Anthropic API model names, used only in local mode.
    anthropic_model_classify: str = field(default_factory=lambda: _env(
        "ANTHROPIC_MODEL_CLASSIFY", "claude-haiku-4-5"))
    anthropic_model_default: str = field(default_factory=lambda: _env(
        "ANTHROPIC_MODEL_DEFAULT", "claude-sonnet-4-6"))

    aws_region: str = field(default_factory=lambda: _env("AWS_REGION", "us-east-1"))
    aws_profile: str | None = field(default_factory=lambda: os.environ.get("AWS_PROFILE"))
    s3_bucket: str = field(default_factory=lambda: _env("S3_BUCKET", "knight-demo-submissions"))
    sqs_queue_url: str = field(default_factory=lambda: _env("SQS_QUEUE_URL", ""))

    data_dir: Path = field(default_factory=lambda: Path(_env(
        "DATA_DIR", str(REPO_ROOT / "data"))))
    llm_cache_dir: Path = field(default_factory=lambda: Path(_env(
        "LLM_CACHE_DIR", str(REPO_ROOT / "fixtures" / "llm_cache"))))
    ocr_cache_dir: Path = field(default_factory=lambda: Path(_env(
        "OCR_CACHE_DIR", str(REPO_ROOT / "fixtures" / "ocr_cache"))))
    # When true, every real LLM/OCR call is written to llm_cache_dir so demo mode can replay it.
    record_llm: bool = field(default_factory=lambda: _env("RECORD_LLM", "true").lower() == "true")

    # Pages with fewer extractable characters than this are treated as image-only.
    min_text_chars: int = field(default_factory=lambda: int(_env("MIN_TEXT_CHARS", "40")))
    render_dpi: int = field(default_factory=lambda: int(_env("RENDER_DPI", "150")))

    def model_for(self, role: str) -> str:
        if self.mode == "local":
            return self.anthropic_model_classify if role == "classify" else self.anthropic_model_default
        return {
            "classify": self.model_classify,
            "extract": self.model_extract,
            "agent": self.model_agent,
            "analysis": self.model_analysis,
        }[role]


def get_settings() -> Settings:
    return Settings()

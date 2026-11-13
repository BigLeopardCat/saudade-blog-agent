"""OpenEvals adapter for frozen evidence; semantic policy stays with the application."""

from importlib.metadata import version
from typing import Literal

from langsmith import tracing_context
from openai import LengthFinishReasonError
from openevals.llm import create_llm_as_judge
from pydantic import BaseModel, ConfigDict, Field

from utils.provider_billing import from_usage


class Verdict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    unsupported: list[str] = Field(max_length=5)
    answered: bool
    verdict: Literal["ok", "suspect"]
    reason: str = Field(min_length=1)


REASONING_EFFORTS = ("none", "low", "medium", "high")


def judge_one(material_text, llm, *, instructions, diagnostics=None):
    from config import settings
    from models.llm import response_format

    if llm.max_retries != 0:
        raise ValueError("semantic review requires max_retries=0")
    schema = Verdict.model_json_schema()
    dialect = response_format(settings.llm_provider, schema, name="verdict",
                              model=llm.model_name)["type"]
    evaluator = create_llm_as_judge(
        prompt=lambda outputs, **_: [{"role": "system", "content": instructions},
                                     {"role": "user", "content": outputs}],
        judge=llm, output_schema=schema,
        structured_output_kwargs={"method": ("json_mode" if dialect == "json_object"
                                              else "json_schema"), "include_raw": True},
    )
    # Review is authorized only for the configured model endpoint, not a tracing service.
    try:
        with tracing_context(enabled=False):
            result = evaluator(outputs=material_text)
    except LengthFinishReasonError as exc:
        if diagnostics is not None:
            usage = exc.completion.usage.model_dump() if exc.completion.usage else {}
            diagnostics.update(finish_reason="length", provider_usage=usage,
                               billing=from_usage(usage), model=exc.completion.model)
        raise
    raw = result["raw"]
    metadata = raw.response_metadata
    if diagnostics is not None:
        diagnostics.update(usage=raw.usage_metadata, finish_reason=metadata.get("finish_reason"),
                           model=metadata.get("model_name"), raw_output=raw.content,
                           billing=from_usage(metadata.get("token_usage")))
    if metadata.get("finish_reason") == "length":
        raise ValueError("judge response was truncated")
    if result["parsing_error"] is not None or result["parsed"] is None:
        raise ValueError("judge returned invalid structured output") from result["parsing_error"]
    verdict = Verdict.model_validate(result["parsed"]).model_dump()
    verdict["verdict"] = "suspect" if verdict["unsupported"] or not verdict["answered"] else "ok"
    return verdict


def make_llm(*, reasoning_effort="none", **kwargs):
    from models.llm import get_llm

    if reasoning_effort not in REASONING_EFFORTS:
        raise ValueError("Unsupported judge reasoning effort")
    if "max_tokens" in kwargs and (type(kwargs["max_tokens"]) is not int
                                    or kwargs["max_tokens"] <= 0):
        raise ValueError("Judge max_tokens must be a positive integer")
    defaults = {"temperature": 0.0, "max_tokens": 700 if reasoning_effort == "none" else 4096,
                "enable_thinking": reasoning_effort != "none", "max_retries": 0,
                "streaming": False}
    if reasoning_effort != "none":
        defaults["reasoning_effort"] = reasoning_effort
    return get_llm(**{**defaults, **kwargs})


def reviewer_options(llm):
    """Public inference controls only; exclude credentials and client headers."""
    body = getattr(llm, "extra_body", None) or {}
    return {"reasoning_effort": getattr(llm, "reasoning_effort", None) or "none",
            "thinking": (body.get("thinking") or {}).get("type", body.get("enable_thinking")),
            "max_tokens": getattr(llm, "max_tokens", None),
            "temperature": getattr(llm, "temperature", None)}


def backend_identity():
    return {"library": "openevals", "version": version("openevals"),
            "schema": Verdict.model_json_schema()}

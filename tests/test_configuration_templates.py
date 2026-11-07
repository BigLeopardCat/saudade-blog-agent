"""Published environment templates must load through the real settings boundary."""

from pathlib import Path

import pytest
from dotenv import dotenv_values

from config.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("template", [".env.example", "deploy/agent-runtime.env.example"])
def test_published_template_loads_without_retired_settings(template, tmp_path):
    source = ROOT / template
    private = tmp_path / ".env"
    private.write_text(source.read_text(), encoding="utf-8")
    configured = Settings(_env_file=private)
    assert configured.llm_provider == dotenv_values(source)["LLM_PROVIDER"]


@pytest.mark.parametrize("template", [".env.example", "deploy/agent-runtime.env.example"])
def test_process_environment_overrides_template(template, monkeypatch):
    with monkeypatch.context() as isolated:
        isolated.setenv("AGENT_INPUT_TOKENS", "64000")
        configured = Settings(_env_file=ROOT / template)
        assert configured.agent_input_tokens == 64000

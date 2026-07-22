from pathlib import Path

import pytest

from miniagent.provider.config import (
    Configured,
    NotConfigured,
    ProviderConfigLoader,
    ProviderConfiguration,
    normalize_chat_completions_url,
)
from miniagent.provider.errors import ProviderConfigurationError
from miniagent.provider.errors import ProviderNotConfiguredError
from miniagent.provider.openai import OpenAICompatibleModelAdapter


@pytest.mark.parametrize(("base", "expected"), [
    ("https://example.com", "https://example.com/v1/chat/completions"),
    ("https://example.com/v1/", "https://example.com/v1/chat/completions"),
    ("https://example.com/openai", "https://example.com/openai/v1/chat/completions"),
    ("https://example.com/openai/v1", "https://example.com/openai/v1/chat/completions"),
])
def test_url_normalization(base, expected):
    assert normalize_chat_completions_url(base) == expected


@pytest.mark.parametrize("base", ["example.com", "ftp://example.com", "https://example.com?v=1", "https://example.com/v1/chat/completions"])
def test_invalid_url(base):
    with pytest.raises(ProviderConfigurationError):
        ProviderConfiguration("model", base, "secret")


def test_loader_environment_wins_and_repr_hides_key(tmp_path: Path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENAI_MODEL=file\nOPENAI_BASE_URL=https://file.test\nOPENAI_API_KEY=file-key\n", encoding="utf-8")
    result = ProviderConfigLoader().load({
        "OPENAI_MODEL": "env",
        "OPENAI_BASE_URL": "https://env.test/v1",
        "OPENAI_API_KEY": "env-key",
    }, dotenv)
    assert isinstance(result, Configured)
    assert result.configuration.model == "env"
    assert "env-key" not in repr(result.configuration)


def test_missing_configuration_only_reports_names():
    result = ProviderConfigLoader().load({"OPENAI_MODEL": "model"})
    assert isinstance(result, NotConfigured)
    assert result.missing == ("OPENAI_BASE_URL", "OPENAI_API_KEY")
    with pytest.raises(ProviderNotConfiguredError):
        OpenAICompatibleModelAdapter(result)

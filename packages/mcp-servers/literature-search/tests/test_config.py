"""Settings parsing, defaults, and the guarantee that keys stay out of text."""

from __future__ import annotations

import pytest
from mcp_literature_search.config import (
    DEFAULT_MAX_RESULTS,
    DEFAULT_TIMEOUT_SECONDS,
    OPENALEX_DEFAULT_BASE_URL,
    SEMANTIC_SCHOLAR_DEFAULT_BASE_URL,
    Settings,
)
from pydantic import ValidationError

FAKE_KEY = "sk-openalex-test-key-do-not-log"


def test_defaults_are_the_live_endpoints() -> None:
    settings = Settings()
    assert settings.openalex_base_url == OPENALEX_DEFAULT_BASE_URL == "https://api.openalex.org"
    assert (
        settings.semanticscholar_base_url
        == SEMANTIC_SCHOLAR_DEFAULT_BASE_URL
        == "https://api.semanticscholar.org/graph/v1"
    )
    assert settings.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    assert settings.max_results == DEFAULT_MAX_RESULTS
    assert settings.openalex_api_key is None
    assert settings.semanticscholar_api_key is None


def test_from_env_reads_every_variable() -> None:
    settings = Settings.from_env(
        {
            "OPENALEX_BASE_URL": "https://openalex.example",
            "OPENALEX_API_KEY": FAKE_KEY,
            "OPENALEX_MAILTO": "me@example.org",
            "SEMANTIC_SCHOLAR_BASE_URL": "https://s2.example",
            "SEMANTIC_SCHOLAR_API_KEY": "s2-secret",
            "LITERATURE_TIMEOUT_SECONDS": "2.5",
            "LITERATURE_MAX_RESULTS": "7",
            "LITERATURE_MAX_REFERENCES": "3",
        }
    )
    assert settings.openalex_base_url == "https://openalex.example"
    assert settings.openalex_api_key is not None
    assert settings.openalex_api_key.get_secret_value() == FAKE_KEY
    assert settings.openalex_mailto == "me@example.org"
    assert settings.semanticscholar_base_url == "https://s2.example"
    assert settings.semanticscholar_api_key is not None
    assert settings.semanticscholar_api_key.get_secret_value() == "s2-secret"
    assert settings.timeout_seconds == 2.5
    assert settings.max_results == 7
    assert settings.max_references == 3


def test_from_env_treats_blank_as_absent() -> None:
    """An empty variable must not become an empty base URL.

    `OPENALEX_BASE_URL=` is what a half-filled .env actually looks like, and an
    empty base URL turns every request into a relative one that httpx rejects with
    a message that does not mention the variable anyone mistyped.
    """
    settings = Settings.from_env({"OPENALEX_BASE_URL": "   ", "OPENALEX_API_KEY": ""})
    assert settings.openalex_base_url == OPENALEX_DEFAULT_BASE_URL
    assert settings.openalex_api_key is None
    assert settings.openalex_mailto is None


def test_from_env_rejects_a_non_numeric_variable_by_name() -> None:
    with pytest.raises(ValueError, match="LITERATURE_TIMEOUT_SECONDS"):
        Settings.from_env({"LITERATURE_TIMEOUT_SECONDS": "soon"})


def test_max_results_is_bounded() -> None:
    """The ceiling exists because tool output is prompt input."""
    with pytest.raises(ValidationError):
        Settings(max_results=0)
    with pytest.raises(ValidationError):
        Settings(max_results=101)


def test_repr_and_str_do_not_contain_the_keys() -> None:
    """The D1 rule, re-checked here: a SecretStr must survive being formatted.

    These settings are logged at startup by `server.main()`. Without SecretStr the
    key would reach stderr, and from there a process supervisor or a CI log.
    """
    settings = Settings.from_env(
        {"OPENALEX_API_KEY": FAKE_KEY, "SEMANTIC_SCHOLAR_API_KEY": "s2-secret"}
    )
    for text in (repr(settings), str(settings), repr(settings.openalex_api_key)):
        assert FAKE_KEY not in text
        assert "s2-secret" not in text
    assert "**********" in repr(settings)


def test_settings_are_frozen() -> None:
    settings = Settings()
    with pytest.raises(ValidationError):
        settings.max_results = 3  # type: ignore[misc]


def test_semanticscholar_availability_follows_the_key() -> None:
    assert Settings().semanticscholar_available is False
    keyed = Settings.from_env({"SEMANTIC_SCHOLAR_API_KEY": "s2-secret"})
    assert keyed.semanticscholar_available is True

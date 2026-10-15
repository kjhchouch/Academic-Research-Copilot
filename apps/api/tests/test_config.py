"""Settings loading and secret handling.

Everything here constructs `Settings(_env_file=None)` so the tests are hermetic:
they must not pass or fail depending on what happens to be in the developer's
real `.env`, and they must never print that file's contents on failure.
"""

from __future__ import annotations

import pytest
from api.config import Settings

SECRET_FIELDS = [
    "llm_api_key",
    "langfuse_secret_key",
    "typesafe_api_key",
    "semantic_scholar_api_key",
]


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name, value in env.items():
        monkeypatch.setenv(name.upper(), value)
    return Settings(_env_file=None)


# --------------------------------------------------------------------------- #
# Defaults — these are the values a fresh clone boots with
# --------------------------------------------------------------------------- #


def test_defaults_boot_with_zero_external_services(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch)
    assert (s.checkpoint_backend, s.cache_backend, s.blob_backend) == ("memory", "memory", "local")


def test_defaults_point_at_deepseek(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch)
    assert s.llm_base_url == "https://api.deepseek.com"
    assert s.llm_chat_model == "deepseek-flash"
    assert s.llm_reasoning_model == "deepseek-v4-pro"


def test_default_max_tokens_leaves_room_for_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepSeek bills reasoning_content against max_tokens. A small budget gets
    spent on reasoning and the tool call never gets emitted, which reads exactly
    like "this model cannot call tools". 2048 is the floor, not a tuning knob.
    """
    assert _settings(monkeypatch).llm_max_tokens >= 2048


def test_no_api_key_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch)
    assert s.llm_api_key is None
    assert not s.llm_api_key, "call sites gate on truthiness, so None must be falsy"


# --------------------------------------------------------------------------- #
# Env parsing
# --------------------------------------------------------------------------- #


def test_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(
        monkeypatch,
        checkpoint_backend="sqlite",
        llm_base_url="https://openrouter.ai/api/v1",
        llm_chat_model="some-other-model",
        llm_max_tokens="4096",
    )
    assert s.checkpoint_backend == "sqlite"
    assert s.llm_base_url == "https://openrouter.ai/api/v1"
    assert s.llm_chat_model == "some-other-model"
    assert s.llm_max_tokens == 4096, "int coercion from a string env var"


def test_cors_origins_parses_a_json_list(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch, cors_origins='["http://localhost:3000","https://x.dev"]')
    assert s.cors_origins == ["http://localhost:3000", "https://x.dev"]


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_blank_env_value_stays_falsy(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    """`.env` ships with `LANGFUSE_PUBLIC_KEY=` style placeholders. Those arrive
    as empty strings, and D3/D4 will gate integrations on truthiness — a blank
    credential must not silently enable Langfuse or Jev.
    """
    s = _settings(monkeypatch, **{field: ""})
    assert not getattr(s, field)


# --------------------------------------------------------------------------- #
# Secret masking — the reason these fields are SecretStr and not str
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_secret_never_appears_in_repr_or_dump(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    """Settings objects get logged, embedded in tracebacks, and handed to
    Langfuse. Any of those would publish a plaintext key if the field were `str`.
    """
    sentinel = "SENTINEL-DO-NOT-LEAK-12345"
    s = _settings(monkeypatch, **{field: sentinel})

    assert sentinel not in repr(s)
    assert sentinel not in str(s)
    assert sentinel not in str(s.model_dump())
    assert sentinel not in str(s.model_dump_json())
    assert sentinel not in s.model_dump_json()
    assert str(getattr(s, field)) == "**********"


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_secret_is_still_retrievable_when_explicit(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """Masking must not break the happy path — the value has to reach the SDK."""
    sentinel = "SENTINEL-DO-NOT-LEAK-12345"
    s = _settings(monkeypatch, **{field: sentinel})
    assert getattr(s, field).get_secret_value() == sentinel


def test_real_env_file_key_is_not_in_a_plain_dump() -> None:
    """End-to-end guard against the actual `.env` on this machine.

    Reads the developer's real settings (the only test that does) and asserts the
    live key cannot be recovered from any of the serializations that end up in
    logs. Skips when no key is configured yet.
    """
    from api.config import get_settings

    s = get_settings()
    if not s.llm_api_key:
        pytest.skip("no LLM_API_KEY configured")

    secret = s.llm_api_key.get_secret_value()
    assert secret not in repr(s)
    assert secret not in s.model_dump_json()

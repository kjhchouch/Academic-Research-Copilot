"""Environment-driven settings for the literature-search server.

Deliberately not pydantic-settings. This process has seven variables and is
spawned by another process, so an explicit `from_env` beats a settings framework
whose discovery rules -- which .env file, which prefix, which wins -- would be
one more thing to get wrong across two processes that must agree on nothing.

The Semantic Scholar key is a `SecretStr` for the reason D1 established: it must
not reach a repr, a log line, or a traceback. It is sent as an `x-api-key`
header, never as a query parameter, so it cannot end up in an upstream access log
or in the `UpstreamError` text that gets handed back to the model.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr

OPENALEX_DEFAULT_BASE_URL = "https://api.openalex.org"
SEMANTIC_SCHOLAR_DEFAULT_BASE_URL = "https://api.semanticscholar.org/graph/v1"

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESULTS = 20


class Settings(BaseModel):
    """What the two upstream clients need to know, and nothing else."""

    model_config = ConfigDict(frozen=True)

    openalex_base_url: str = OPENALEX_DEFAULT_BASE_URL
    openalex_api_key: SecretStr | None = None
    # OpenAlex's polite pool: requests carrying a reachable contact address get a
    # faster, dedicated rate limit instead of the shared one. Optional, and the
    # reason it is a separate variable from the API key is that the two solve
    # different problems -- mailto buys priority, a key buys a private budget.
    openalex_mailto: str | None = None
    semanticscholar_base_url: str = SEMANTIC_SCHOLAR_DEFAULT_BASE_URL
    semanticscholar_api_key: SecretStr | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    # A hard ceiling on `limit`, clamped rather than rejected: the caller is a
    # model, and a model that asks for 500 papers and gets a validation error
    # will usually just ask again. Clamping and reporting `truncated` in the
    # result teaches it the real bound in one round trip.
    #
    # The bound exists because tool output goes into a prompt. At ~170 words per
    # abstract, 20 results is already a few thousand tokens, and the whole point
    # of the fast path is to stay cheap.
    max_results: int = Field(default=DEFAULT_MAX_RESULTS, ge=1, le=100)
    # How many entries of a bibliography `get_paper` resolves to titles. Each
    # OpenAlex reference costs a slot in one batched request, so this is a cost
    # knob, not a correctness one; the count of *all* references is always
    # returned as `referenced_works_count`.
    max_references: int = Field(default=20, ge=0, le=50)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Self:
        """Read settings from the environment.

        `environ` is injectable so tests do not have to monkeypatch `os.environ`
        -- same reasoning as the `transport` seam on the upstream clients.
        """
        env = os.environ if environ is None else environ

        def _secret(name: str) -> SecretStr | None:
            raw = env.get(name, "").strip()
            return SecretStr(raw) if raw else None

        def _number(name: str, default: float | int) -> float | int:
            raw = env.get(name, "").strip()
            if not raw:
                return default
            try:
                return float(raw) if isinstance(default, float) else int(raw)
            except ValueError:
                # A typo in a number should not silently become the default: this
                # process is spawned by another one and its stderr may be the only
                # evidence anyone sees. Fail at startup with the variable named.
                raise ValueError(f"{name} must be a number, got {raw!r}") from None

        return cls(
            openalex_base_url=env.get("OPENALEX_BASE_URL", "").strip() or OPENALEX_DEFAULT_BASE_URL,
            openalex_api_key=_secret("OPENALEX_API_KEY"),
            openalex_mailto=env.get("OPENALEX_MAILTO", "").strip() or None,
            semanticscholar_base_url=env.get("SEMANTIC_SCHOLAR_BASE_URL", "").strip()
            or SEMANTIC_SCHOLAR_DEFAULT_BASE_URL,
            semanticscholar_api_key=_secret("SEMANTIC_SCHOLAR_API_KEY"),
            timeout_seconds=float(_number("LITERATURE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
            max_results=int(_number("LITERATURE_MAX_RESULTS", DEFAULT_MAX_RESULTS)),
            max_references=int(_number("LITERATURE_MAX_REFERENCES", 20)),
        )

    @property
    def semanticscholar_available(self) -> bool:
        """Whether the Semantic Scholar path can be used at all.

        Unkeyed requests to its Graph API are rate limited to the point of being
        unusable -- it returns 429 on the first call from a shared egress IP -- so
        "no key" is functionally "no backend", and `get_paper` says so instead of
        making a request that will fail.
        """
        return self.semanticscholar_api_key is not None

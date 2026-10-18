"""Turn token counts into money.

Why this exists instead of a Langfuse model definition
-----------------------------------------------------
Langfuse can price a generation, but only from a table it matches the model name
against. Two problems, in increasing order of seriousness:

1. Its managed table has ~190 entries and **not one DeepSeek model** (checked
   2026-10-17 against our own project). So `deepseek-flash` prices as 0 --
   which is indistinguishable in the UI from "this run was free".
2. Even a hand-written definition cannot express DeepSeek's actual price list.
   Rates depend on the **hour**: peak is Mon-Fri 01:00-04:00 and 06:00-10:00 UTC
   *excluding Chinese public holidays*, and off-peak is exactly half. A model
   definition has `startDate` and pricing tiers, but no hour-of-day dimension and
   no holiday calendar. Whatever single rate you enter is wrong by up to 2x, in a
   direction that changes with the clock.

So cost is computed here, from the same `usage_metadata` LangChain already hands
us -- the very fields Langfuse received and could not price.

Design rule: **an unknown model raises, it never prices as zero.** A cost of 0
must always mean "this genuinely cost nothing", never "we forgot to look it up".
That confusion is the entire reason Langfuse's dashboard has been useless to us.

Two contract details that are easy to get wrong, both verified against
`langchain_core.messages.ai.UsageMetadata` rather than assumed:

- `input_tokens` is the *sum of all input token types*, and
  `input_token_details.cache_read` is a **subset** of it, not an addition. So
  cache-hit tokens are subtracted before the miss rate is applied. Adding them
  instead would double-bill every cached prompt.
- `output_tokens` likewise already includes `output_token_details.reasoning`.
  DeepSeek's thinking mode emits a lot of reasoning tokens; billing them again
  would roughly double the output cost. `test_reasoning_tokens_are_not_billed_twice`
  pins this.

Prices are USD per 1M tokens, exactly as published, kept as literals in this
file rather than in a TOML next to it: they change a few times a year, they are
reviewed in the same diff as the arithmetic that uses them, and a data file
would add path resolution, packaging and a parse-failure mode to buy nothing.
`effective` and `source` exist so staleness is visible at a glance.

**Not modelled: Chinese public holidays.** Those days are off-peak in full. The
`holidays` parameter takes a set of dates; the default is empty, so a holiday
falling on a weekday is priced as peak and cost is over-estimated by up to 2x for
that day. Direction known, magnitude bounded -- better than inventing a holiday
table that silently rots. It is also compared against the **UTC** date, while
DeepSeek means the Beijing one, so even with a set supplied the first and last
eight hours of a holiday can be classified wrong.

**Not modelled: exchange rate.** DeepSeek bills in USD against a balance that may
be displayed in CNY. Converting would mean hard-coding a rate that moves daily,
so everything here stays USD.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

# DeepSeek publishes peak hours as two UTC windows on weekdays. Stored as the
# set of hours that are *inside* a window: 01:00-04:00 covers hours 1, 2, 3
# (04:00 itself is the exclusive end), 06:00-10:00 covers 6, 7, 8, 9.
PEAK_HOURS_UTC = frozenset({1, 2, 3, 6, 7, 8, 9})
PEAK_WEEKDAYS = frozenset({0, 1, 2, 3, 4})  # Mon-Fri

_PER_MILLION = Decimal(1_000_000)


class UnknownModelError(LookupError):
    """The model has no entry in PRICES.

    Deliberately not a silent zero. See the module docstring.
    """


@dataclass(frozen=True, slots=True)
class Tier:
    """USD per 1M tokens, for one peak/off-peak half of the day."""

    input_cache_miss: Decimal
    input_cache_hit: Decimal
    output: Decimal


@dataclass(frozen=True, slots=True)
class ModelPrice:
    peak: Tier
    off_peak: Tier
    #: Date this price list took effect. DeepSeek raised prices on 2026-08-16
    #: (cache hits by up to ~11x); anything copied from a pre-August blog post
    #: is wrong, which is why the date is part of the data.
    effective: date
    source: str


PRICES: dict[str, ModelPrice] = {
    # DeepSeek-V4.1-Flash. `deepseek-flash` is the current name; the legacy
    # `deepseek-v4-flash` is still accepted by the API but routes here.
    "deepseek-flash": ModelPrice(
        peak=Tier(
            input_cache_miss=Decimal("0.30"),
            input_cache_hit=Decimal("0.006"),
            output=Decimal("1.20"),
        ),
        off_peak=Tier(
            input_cache_miss=Decimal("0.15"),
            input_cache_hit=Decimal("0.003"),
            output=Decimal("0.60"),
        ),
        effective=date(2026, 8, 16),
        source="https://api-docs.deepseek.com/quick_start/pricing/",
    ),
    # DeepSeek-V4-Pro-0813. Deep Path reasoning nodes.
    "deepseek-v4-pro": ModelPrice(
        peak=Tier(
            input_cache_miss=Decimal("1.32"),
            input_cache_hit=Decimal("0.044"),
            output=Decimal("3.96"),
        ),
        off_peak=Tier(
            input_cache_miss=Decimal("0.66"),
            input_cache_hit=Decimal("0.022"),
            output=Decimal("1.98"),
        ),
        effective=date(2026, 8, 16),
        source="https://api-docs.deepseek.com/quick_start/pricing/",
    ),
}


def price_of(model: str) -> ModelPrice:
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownModelError(
            f"no price entry for model {model!r}; known: {sorted(PRICES)}"
        ) from None


def is_peak(at: datetime, *, holidays: Collection[date] = ()) -> bool:
    """Whether `at` falls in a DeepSeek peak window.

    An aware datetime is converted to UTC before its hour and weekday are read;
    a naive one is *interpreted* as UTC. The two branches cannot be collapsed
    into `at.astimezone(UTC)`, which would read a naive datetime as local time
    -- on this machine (UTC+8) that silently moves the window by eight hours.

    So: pass `datetime.now(UTC)`. Passing local time is wrong in both forms, and
    wrong by a factor of two in the reported cost rather than by an error.
    """
    utc = at.astimezone(UTC) if at.tzinfo is not None else at.replace(tzinfo=UTC)
    if utc.date() in holidays:
        return False
    return utc.weekday() in PEAK_WEEKDAYS and utc.hour in PEAK_HOURS_UTC


@dataclass(frozen=True, slots=True)
class Cost:
    #: Total, in USD. Not rounded -- a single turn is often under a tenth of a
    #: cent, and rounding to cents would report 0.00 for real work.
    usd: Decimal
    model: str
    peak: bool
    #: Billable at the cache-miss rate: input_tokens minus cache_read.
    input_cache_miss_tokens: int
    #: Billable at the cache-hit rate. A subset of the provider's input_tokens.
    input_cache_hit_tokens: int
    #: Includes reasoning tokens; they are not billed separately.
    output_tokens: int

    @property
    def input_tokens(self) -> int:
        return self.input_cache_miss_tokens + self.input_cache_hit_tokens


def cost_of(
    model: str,
    usage: Mapping[str, Any],
    *,
    at: datetime,
    holidays: Collection[date] = (),
) -> Cost:
    """Price one model call from its LangChain `usage_metadata`.

    Tolerates a missing or partial `usage` mapping: a provider that reports no
    usage yields a cost of 0 with 0 tokens, which is true rather than guessed.
    """
    price = price_of(model)
    peak = is_peak(at, holidays=holidays)
    tier = price.peak if peak else price.off_peak

    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    details = usage.get("input_token_details") or {}
    cache_hit = int(details.get("cache_read") or 0)
    # A provider reporting more cached tokens than input tokens is contradicting
    # the documented contract. This is external data, so clamp rather than raise:
    # the alternative is a negative cost on someone's dashboard.
    cache_hit = min(cache_hit, max(input_tokens, 0))
    cache_miss = max(input_tokens - cache_hit, 0)

    usd = (
        Decimal(cache_miss) * tier.input_cache_miss
        + Decimal(cache_hit) * tier.input_cache_hit
        + Decimal(output_tokens) * tier.output
    ) / _PER_MILLION

    return Cost(
        usd=usd,
        model=model,
        peak=peak,
        input_cache_miss_tokens=cache_miss,
        input_cache_hit_tokens=cache_hit,
        output_tokens=output_tokens,
    )


def format_usd(amount: Decimal) -> str:
    """Render a cost for a log line.

    Microdollar precision looks excessive until you notice that one turn of this
    agent costs about $0.0008. Anything coarser prints "$0.00" for real work,
    which is the exact confusion this module exists to prevent.
    """
    if amount == 0:
        return "$0"
    return f"${amount:.6f}"

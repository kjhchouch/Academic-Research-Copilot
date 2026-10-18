"""Cost arithmetic.

The two tests that matter most are the ones pinning the token-accounting
contract (`cache_read` is a subset of `input_tokens`; `reasoning` is a subset of
`output_tokens`). Both were read out of `langchain_core`'s `UsageMetadata`
docstring rather than assumed, and both would otherwise fail as an inflated
invoice rather than as an error.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from paper_agent.pricing import (
    PRICES,
    Cost,
    UnknownModelError,
    cost_of,
    format_usd,
    is_peak,
    price_of,
)

# 2026-10-19 is a Monday; 10-17/10-18 are the weekend before it.
MONDAY = date(2026, 10, 19)
SATURDAY = date(2026, 10, 17)


def utc(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=UTC)


# A shape close to what DeepSeek actually returns for one tool-calling turn.
USAGE = {
    "input_tokens": 1000,
    "output_tokens": 200,
    "total_tokens": 1200,
    "input_token_details": {"cache_read": 400},
    "output_token_details": {"reasoning": 150},
}


def test_off_peak_is_exactly_half_of_peak() -> None:
    """Published rule: "Off-peak rates are half of the peak rates."

    Both tiers are stored rather than derived, so a price update only has to
    touch one place per model -- and this is what notices if the ratio ever
    stops holding.
    """
    for model, price in PRICES.items():
        for field in ("input_cache_miss", "input_cache_hit", "output"):
            peak = getattr(price.peak, field)
            off_peak = getattr(price.off_peak, field)
            assert off_peak * 2 == peak, f"{model}.{field}"


def test_every_price_entry_is_internally_consistent() -> None:
    """Structural checks that catch a transposed or copy-pasted number.

    Not a second transcription of the table -- that would only fail when someone
    edits both copies. A cache hit must be cheaper than a miss, or the whole
    point of caching is inverted.
    """
    for model, price in PRICES.items():
        assert price.effective >= date(2026, 8, 16), f"{model}: pre-increase prices?"
        assert price.source.startswith("https://"), model
        for tier in (price.peak, price.off_peak):
            assert tier.input_cache_hit < tier.input_cache_miss, model
            assert tier.output > tier.input_cache_miss, model
            for value in (tier.input_cache_miss, tier.input_cache_hit, tier.output):
                assert value > 0, model


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (utc(MONDAY, 0, 59), False),
        (utc(MONDAY, 1, 0), True),  # window opens
        (utc(MONDAY, 3, 59), True),
        (utc(MONDAY, 4, 0), False),  # 04:00 is the exclusive end
        (utc(MONDAY, 5, 59), False),  # the gap between the two windows
        (utc(MONDAY, 6, 0), True),
        (utc(MONDAY, 9, 59), True),
        (utc(MONDAY, 10, 0), False),
        (utc(MONDAY, 23, 0), False),
        (utc(SATURDAY, 2, 0), False),  # weekends are off-peak in full
        (utc(SATURDAY, 7, 0), False),
        (utc(SATURDAY + timedelta(days=1), 7, 0), False),  # Sunday
        (utc(MONDAY + timedelta(days=4), 7, 0), True),  # Friday
        (utc(MONDAY + timedelta(days=5), 7, 0), False),  # the following Saturday
    ],
)
def test_peak_window_boundaries(moment: datetime, expected: bool) -> None:
    assert is_peak(moment) is expected


def test_a_chinese_public_holiday_is_off_peak_even_on_a_weekday() -> None:
    moment = utc(MONDAY, 2, 0)
    assert is_peak(moment) is True
    assert is_peak(moment, holidays={MONDAY}) is False
    # Only the listed date is affected.
    assert is_peak(moment, holidays={MONDAY + timedelta(days=1)}) is True


def test_naive_datetime_is_read_as_utc() -> None:
    """The documented interpretation of a tz-naive `at`.

    Worth pinning because the wrong guess is silent and expensive: this machine
    is UTC+8, so treating local time as UTC would move the peak window by eight
    hours and halve most reported costs.
    """
    naive = datetime(2026, 10, 19, 2, 0)
    assert is_peak(naive) is True
    assert is_peak(naive) == is_peak(naive.replace(tzinfo=UTC))


def test_a_non_utc_aware_datetime_is_shifted_before_comparison() -> None:
    beijing = timezone(timedelta(hours=8))
    moment = utc(MONDAY, 2, 0).astimezone(beijing)
    assert moment.hour == 10  # 02:00 UTC is 10:00 in Beijing
    assert is_peak(moment) is True  # ...and still peak, because it is 02:00 UTC

    # The conversion has to reach the date too, not just the hour: Monday 07:00
    # in Beijing is still Sunday in UTC, and Sunday is off-peak all day.
    monday_morning = datetime(2026, 10, 19, 7, 0, tzinfo=beijing)
    assert monday_morning.utcoffset() == timedelta(hours=8)
    assert is_peak(monday_morning) is False


def test_cache_hit_tokens_are_subtracted_not_added() -> None:
    """`input_tokens` already contains `cache_read`; billing both double-charges.

    600 miss @ $0.15 + 400 hit @ $0.003 + 200 out @ $0.60, all per 1M, off-peak.
    """
    cost = cost_of("deepseek-flash", USAGE, at=utc(MONDAY, 12))
    assert cost.input_cache_miss_tokens == 600
    assert cost.input_cache_hit_tokens == 400
    assert cost.input_tokens == 1000
    assert cost.output_tokens == 200
    assert cost.usd == Decimal("0.0002112")

    # The bug this prevents: treating cache_read as additional input would bill
    # 1400 miss + 400 hit, roughly 2.3x the real cost on this shape.
    wrong = Decimal(1400) * Decimal("0.15") + Decimal(400) * Decimal("0.003")
    wrong += Decimal(200) * Decimal("0.60")
    assert cost.usd < wrong / Decimal(1_000_000)


def test_reasoning_tokens_are_not_billed_twice() -> None:
    """DeepSeek's thinking mode reports reasoning inside output_tokens."""
    at = utc(MONDAY, 12)
    with_reasoning = cost_of("deepseek-flash", USAGE, at=at)
    without = dict(USAGE)
    without.pop("output_token_details")
    assert with_reasoning.usd == cost_of("deepseek-flash", without, at=at).usd
    assert with_reasoning.output_tokens == 200


def test_peak_costs_exactly_twice_off_peak() -> None:
    peak = cost_of("deepseek-flash", USAGE, at=utc(MONDAY, 2))
    off_peak = cost_of("deepseek-flash", USAGE, at=utc(MONDAY, 12))
    assert peak.peak is True and off_peak.peak is False
    assert peak.usd == off_peak.usd * 2


def test_the_reasoning_model_is_priced_separately() -> None:
    """Deep Path uses a different model, and the markup is not uniform.

    Off-peak `deepseek-v4-pro` is 4.4x `deepseek-flash` on a cache miss, 7.3x on
    a cache hit and 3.3x on output -- so the ratio for a whole turn depends on
    the token mix. Asserting the absolute numbers is what catches a lookup
    silently falling back to a default, which is the failure mode this module
    exists to prevent.
    """
    at = utc(MONDAY, 12)
    flash = cost_of("deepseek-flash", USAGE, at=at)
    pro = cost_of("deepseek-v4-pro", USAGE, at=at)
    assert flash.usd == Decimal("0.0002112")
    assert pro.usd == Decimal("0.0008008")
    assert pro.usd > flash.usd


def test_unknown_model_raises_instead_of_pricing_as_zero() -> None:
    """The rule that distinguishes this from Langfuse's model table."""
    with pytest.raises(UnknownModelError) as excinfo:
        cost_of("gpt-5", USAGE, at=utc(MONDAY, 12))
    message = str(excinfo.value)
    assert "gpt-5" in message
    assert "deepseek-flash" in message  # tells the caller what to add


def test_price_of_reports_known_models_in_the_error() -> None:
    with pytest.raises(UnknownModelError, match="deepseek-v4-pro"):
        price_of("")


def test_absent_usage_prices_as_zero() -> None:
    """A provider that reports no usage yields a true zero, not a guess."""
    for usage in ({}, {"input_tokens": None, "output_tokens": None}):
        cost = cost_of("deepseek-flash", usage, at=utc(MONDAY, 12))
        assert cost.usd == 0
        assert cost.input_tokens == 0
        assert cost.output_tokens == 0


def test_a_provider_contradicting_the_contract_is_clamped_not_negative() -> None:
    """cache_read > input_tokens is impossible; the data is external, so clamp.

    The alternative is a negative cost on a dashboard, which is worse than an
    obviously-wrong positive one.
    """
    cost = cost_of(
        "deepseek-flash",
        {"input_tokens": 1000, "output_tokens": 0, "input_token_details": {"cache_read": 5000}},
        at=utc(MONDAY, 12),
    )
    assert cost.input_cache_hit_tokens == 1000
    assert cost.input_cache_miss_tokens == 0
    assert cost.usd >= 0


def test_negative_input_tokens_cannot_produce_a_negative_cost() -> None:
    cost = cost_of("deepseek-flash", {"input_tokens": -500}, at=utc(MONDAY, 12))
    assert cost.input_cache_miss_tokens == 0
    assert cost.usd == 0


def test_a_real_turn_costs_a_fraction_of_a_cent() -> None:
    """Anchor against the numbers our first live Langfuse run actually produced.

    7 generations, 3621 input / 414 output. The point is the order of magnitude:
    at this rate the ~$0.45 balance covers thousands of turns, and it is the
    Week 4 eval suite -- not development -- that will spend the money.
    """
    cost = cost_of(
        "deepseek-flash",
        {"input_tokens": 3621, "output_tokens": 414},
        at=utc(MONDAY, 12),
    )
    assert cost.usd == Decimal("0.00079155")
    assert cost.usd < Decimal("0.01")


def test_cost_is_immutable_and_carries_its_inputs() -> None:
    cost = cost_of("deepseek-flash", USAGE, at=utc(MONDAY, 2))
    assert isinstance(cost, Cost)
    assert cost.model == "deepseek-flash"
    with pytest.raises(AttributeError):
        cost.usd = Decimal(0)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal(0), "$0"),
        (Decimal("0.0002112"), "$0.000211"),
        (Decimal("0.00079155"), "$0.000792"),
        (Decimal("0.01"), "$0.010000"),
        (Decimal("1.23456"), "$1.234560"),
    ],
)
def test_format_usd_does_not_round_real_work_to_zero(amount: Decimal, expected: str) -> None:
    """A single turn costs ~$0.0008. Two decimal places would print $0.00."""
    assert format_usd(amount) == expected

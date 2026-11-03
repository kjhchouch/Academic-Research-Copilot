"""Run the routing or grounding eval against one backend and print the report.

    uv run python -m evals --backend small_llm
    uv run python -m evals --backend jev --json > docs/routing-eval-jev.json
    uv run python -m evals --task grounding --backend small_llm

`--backend` is required and has no default, and that is the whole point of this
file's shape. The production router is Jev; the only backend that can run today
is the `small_llm` fallback on deepseek-flash, because Jev is blocked on
`TYPESAFE_API_KEY`. A tool that silently defaulted to whichever backend happened
to be configured is how a fallback's accuracy ends up in a README described as
the production router's. Forcing the caller to name the backend, and echoing it
back in the report with a warning when it is the fallback, keeps the number
attached to its provenance. The grounding eval inherits both rules unchanged:
its production backend is likewise Jev (entailment checking is one of Jev's
three jobs in the tech-stack table), likewise awaiting the same key.

Backends are imported lazily inside their builders so `--help` and the pure
report formatting work with no credentials and without pulling langchain-openai
or typesafe_sdk into the process until a live run actually asks for them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from evals.grounding import DATA as GROUNDING_DATA
from evals.grounding import Report as GroundingReport
from evals.grounding import load_rows as load_grounding_rows
from evals.grounding import run_eval as run_grounding_eval
from evals.routing import DATA, Report, load_rows, run_eval

# Which backend is the degraded one. Kept as a set rather than a string so the
# warning logic does not have to change when a third backend (the embedding tier)
# lands -- the point is "anything that is not the intended production router".
_PRODUCTION_BACKEND = "jev"


def _require_env(name: str, backend: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"{name} is not set, so the {backend!r} backend cannot run.\n"
            f"Set it in the environment (or your .env and export it) and retry."
        )
    return value


def _build_chat_model() -> Any:
    """routing 与 grounding 共用的降级档模型——同一个 key、同一个便宜档。"""
    from paper_agent.llm import make_chat_model

    return make_chat_model(
        base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
        api_key=_require_env("LLM_API_KEY", "small_llm"),
        model=os.environ.get("LLM_CHAT_MODEL", "deepseek-flash"),
        max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "2048")),
        temperature=float(os.environ.get("LLM_TEMPERATURE", "0.2")),
        # Routing / entailment 都是单轮 JSON 分类，没有工具调用和后续轮次，
        # thinking mode 买不到任何东西——与聊天路径同一个默认（disabled）。
    )


def _build_small_llm() -> Any:
    from paper_agent.routing import SmallLLMBackend

    return SmallLLMBackend(_build_chat_model())


def _build_jev() -> Any:
    from paper_agent.routing import make_jev_backend

    backend = make_jev_backend(
        api_key=os.environ.get("TYPESAFE_API_KEY"),
        model=os.environ.get("JEV_MODEL", "jev"),
    )
    if backend is None:
        raise SystemExit(
            "TYPESAFE_API_KEY is not set, so the jev backend cannot run.\n"
            "This is the intended production router; until the key exists, the "
            "only runnable backend is --backend small_llm (the fallback)."
        )
    return backend


def _build_grounding_small_llm() -> Any:
    from paper_agent.grounding import SmallLLMEntailment

    return SmallLLMEntailment(_build_chat_model())


def _build_grounding_jev() -> Any:
    raise SystemExit(
        "the Jev entailment backend is not built yet: TYPESAFE_API_KEY's online "
        "acceptance (D4) is still pending, and the entailment tier will land with "
        "it. Until then the only runnable grounding backend is small_llm."
    )


_BUILDERS = {"jev": _build_jev, "small_llm": _build_small_llm}
_GROUNDING_BUILDERS = {"jev": _build_grounding_jev, "small_llm": _build_grounding_small_llm}


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


async def _run(backend: str, rows: list[Any]) -> Report:
    """Build the backend, grade it, and always release what it holds.

    One event loop for both the run and the close: the Jev backend's httpx client
    is bound to the loop it was created on, and closing it from a second
    `asyncio.run` would tear down a client in a loop that no longer owns it.
    """
    router = _BUILDERS[backend]()
    try:
        return await run_eval(router, rows)
    finally:
        aclose = getattr(router, "aclose", None)
        if aclose is not None:
            await aclose()


async def _run_grounding(backend: str, rows: list[Any]) -> GroundingReport:
    entailment = _GROUNDING_BUILDERS[backend]()
    try:
        return await run_grounding_eval(entailment, rows)
    finally:
        aclose = getattr(entailment, "aclose", None)
        if aclose is not None:
            await aclose()


def _print_report(report: Report, backend: str, data_path: Path) -> None:
    line = "-" * 74
    print(line)
    print(f"Routing eval — backend: {backend}")
    print(f"dataset: {data_path}")
    print(
        f"rows: {report.n_rows} "
        f"(intent-scored: {report.n_scored}, router errors: {report.n_errors})"
    )
    print(line)

    header = f"{'intent':<18}{'support':>8}{'pred':>7}{'prec':>8}{'recall':>8}{'f1':>8}{'fpr':>8}"
    print(header)
    for intent, s in report.per_intent.items():
        print(
            f"{intent:<18}{s.support:>8}{s.predicted:>7}"
            f"{s.precision:>8.3f}{s.recall:>8.3f}{s.f1:>8.3f}{s.fpr:>8.3f}"
        )
    print(line)

    print(f"accuracy   {report.accuracy:.4f}")
    print(f"macro-F1   {report.macro_f1:.4f}")
    print(f"macro-FPR  {report.macro_fpr:.4f}")
    print(
        f"boundary   leaked {report.boundary.leaked}/{report.boundary.n} "
        f"({_fmt_rate(report.boundary.leak_rate)})  "
        "<- out-of-scope requests that escaped refusal"
    )
    print(
        f"clarify    correct {report.clarify.correct}/{report.clarify.n} "
        f"({_fmt_rate(report.clarify.rate)})"
    )
    print(
        f"path_deep  correct {report.path_deep.correct}/{report.path_deep.n} "
        f"({_fmt_rate(report.path_deep.rate)})"
    )
    print(f"by_source  {report.by_source}")
    print(line)

    if backend != _PRODUCTION_BACKEND:
        print(
            f"NOTE: {backend!r} is a degraded fallback, not the production Jev "
            "router.\n      Report this as the fallback's accuracy; the Jev "
            "number needs TYPESAFE_API_KEY."
        )


def _print_grounding_report(report: GroundingReport, backend: str, data_path: Path) -> None:
    line = "-" * 74
    print(line)
    print(f"Grounding eval — backend: {backend}")
    print(f"dataset: {data_path}")
    print(
        f"rows: {report.n_rows} in {report.n_answer_groups} answer groups "
        f"(backend errors: {report.n_errors})"
    )
    print(line)

    header = f"{'verdict':<14}{'support':>8}{'pred':>7}{'prec':>8}{'recall':>8}{'f1':>8}"
    print(header)
    for label, s in report.per_class.items():
        print(
            f"{label:<14}{s.support:>8}{s.predicted:>7}"
            f"{s.precision:>8.3f}{s.recall:>8.3f}{s.f1:>8.3f}"
        )
    print(line)

    print(f"accuracy   {report.accuracy:.4f}")
    print(f"macro-F1   {report.macro_f1:.4f}")
    print(
        f"leakage    {report.leakage.leaked}/{report.leakage.n} "
        f"({_fmt_rate(report.leakage.rate)})  "
        "<- predicted 'supported' but ground truth disagrees (the dangerous direction)"
    )
    print(
        f"citation precision {report.citation_precision:.4f}   "
        f"citation recall {report.citation_recall:.4f}   "
        "(README 口径：按 answer 组宏平均，从后端判定算——真实产品的端到端数字"
        "需要对真实答案做 live 评测)"
    )
    print(f"by_backend {report.by_backend}")
    print(line)

    if backend != _PRODUCTION_BACKEND:
        print(
            f"NOTE: {backend!r} is a degraded fallback, not the production Jev "
            "entailment tier.\n      Report this as the fallback's number; the Jev "
            "number needs TYPESAFE_API_KEY."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description="Run the routing or grounding eval against one backend and print the report.",
        epilog=(
            "Live runs cost real balance (small_llm) or need a TypeSafe key (jev). "
            "The offline metric tests in packages/evals/tests need neither."
        ),
    )
    parser.add_argument(
        "--task",
        default="routing",
        choices=("routing", "grounding"),
        help="which eval to run (default: routing)",
    )
    parser.add_argument(
        "--backend",
        required=True,
        choices=sorted(_BUILDERS),
        help="which backend to grade; no default, so a fallback number "
        "cannot be produced by accident",
    )
    parser.add_argument(
        "--data",
        default=None,
        help="path to the task's jsonl dataset (default: the packaged one)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the report as JSON (archivable) instead of a table",
    )
    args = parser.parse_args(argv)

    if args.task == "routing":
        data_path = Path(args.data) if args.data else DATA
        report: Any = asyncio.run(_run(args.backend, load_rows(data_path)))
        if args.json:
            print(
                json.dumps(
                    {"task": "routing", "backend": args.backend, **report.as_dict()},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        _print_report(report, args.backend, data_path)
        return 0

    data_path = Path(args.data) if args.data else GROUNDING_DATA
    report = asyncio.run(_run_grounding(args.backend, load_grounding_rows(data_path)))
    if args.json:
        print(
            json.dumps(
                {"task": "grounding", "backend": args.backend, **report.as_dict()},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    _print_grounding_report(report, args.backend, data_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

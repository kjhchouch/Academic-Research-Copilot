# Third-party skill provenance

This directory vendors a subset of **[Nature Skills](https://github.com/Yuan1z0825/nature-skills)**
(Apache-2.0). See `NOTICE` for copyright holders and `LICENSE` for the full grant.

| | |
|---|---|
| Upstream commit | `19be8d45e0d5233aa44be6d4572e655b9914ff54` |
| Commit date | 2026-10-14T16:01:29+08:00 |
| Retrieved | 2026-10-14 (nature-writing added 2026-10-22, same commit) |
| Vendored | 7 skills, 200 files, 875 KB (896,001 bytes, sum of file sizes) |
| Modified | none — verbatim copies |

Upstream ships 23 skills. We vendor 7. This file records **why each of the
other 16 was left out**, so the decision can be re-examined instead of
silently inherited.

## Why vendor third-party skills at all

Skills are **knowledge**, not architecture. Curated Nature-style writing
patterns, statistical-reporting conventions, and reference-verification
heuristics are domain content that would be dishonest to pretend we authored.
What must remain unmistakably ours is the engineering **around** them:

- the router that decides which skill fires (`packages/agent/src/paper_agent/routing/`)
- the grounding pipeline that verifies skill output against retrieved evidence
- the eval suite that measures whether any of it worked

A skill pack cannot substitute for those three, and this project is not
allowed to treat it as one.

## Vendored

| Skill | Maturity | Code | Maps to |
|---|---|---|---|
| `nature-shared` | (infra) | 2 files | Dependency of the six below. Upstream README: *"当技能引用 `../nature-shared` 时需要保留"* |
| `nature-polishing` | Stable | 0 | **润色** capability |
| `nature-statistics` | Draft | 0 | **处理数据** layer (a): statistical reporting |
| `nature-social-science` | Draft | 0 | **提供思路** capability |
| `nature-ref-verifier` | Stable | 0 | Citation Precision KPI / grounding |
| `nature-response` | Beta | 3 files | Reviewer-response letters (scope addition approved 2026-10-14) |
| `nature-writing` | Draft | 0 | **起草/重构章节** capability — section-level only (scope addition approved 2026-10-22; see the boundary condition below) |

### nature-writing boundary condition (辅助不代写)

`nature-writing` is the one vendored skill whose upstream description covers
drafting a *"full manuscript argument"*, which is wider than this product's
辅助不代写 boundary permits. Vendoring the **content** is fine; exposing it
un-narrowed is not. Before the skill loader makes `nature-writing` reachable,
our own routing layer MUST:

- narrow exposure to **section-level** drafting (abstract / introduction /
  results / discussion / conclusion), never whole-paper generation;
- require **author-provided claims, results, figures or notes** as input — the
  skill drafts from the author's material, it does not invent findings;
- have **L0 reject whole-paper / 整篇代写 / 降重 requests** outright.

The boundary is ours to enforce, not the skill's. As of 2026-10-22 no skill
loader exists, so `nature-writing` is vendored-but-unreachable; this condition
is the gate on wiring it in.

### Sibling-path constraint (affects the skill loader)

Vendored skills reference shared files as `../nature-shared/core/<file>.md`.
14 unique such references exist across the seven directories, and all 14 resolve
**only because the seven directories are siblings** (verified 2026-10-22: every
referenced `nature-shared` file is present, zero dangling). The skill loader must
therefore preserve this flat sibling layout, or rewrite those paths at load
time. Do not nest these directories one level deeper.

### Code review result

All 5 executable files were reviewed before vendoring:

```
nature-shared/scripts/check_consistency.py            272 lines
nature-shared/tests/test_check_consistency.py
nature-response/scripts/check_package_consistency.py  311 lines
nature-response/tests/test_package_consistency.py
nature-response/tests/test_response_instruction_contracts.py
```

Imports are **stdlib only**: `argparse, json, re, sys, dataclasses, decimal,
pathlib, typing`. No network, no `subprocess`, no `eval`/`exec`, no
environment-variable or credential access, no filesystem writes outside the
paths given as arguments.

`nature-writing` (vendored 2026-10-22) adds **no executable files** — all 77
files are Markdown/YAML, with no `install.sh`, no `scripts/`, no caches — so
this review set is unchanged and there was nothing to strip.

`check_consistency.py` is a rule-based manuscript consistency checker
(terminology groups, unit conversion via `Decimal`, numeric agreement) that
emits structured `Finding` records as JSON. It is a usable reference
implementation for the numeric-fidelity check planned for the Verifier
sub-agent — no LLM call, no token cost.

## Not vendored, and why

### Hard exclusions

| Skill | Reason |
|---|---|
| `nature-downloader` | Obtains full text *"通过图书馆资源入口、Chrome 登录态和开放获取路径"* — drives the user's authenticated institutional browser session (CARSI / Web of Science) to fetch paywalled PDFs. Legally and ethically unacceptable for a product, and it is MIT-licensed third-party code we would inherit liability for. |
| `nature-academic-search/install.sh` | See "Installer review" below. |
| `nature-figure` | **34 MB**, 129 files, 38 scripts — mostly `assets/figures4papers/` example plots. Would dominate the repository. Also out of scope for the three product capabilities. |
| `nature-paper2ppt`, `nature-image2ppt` | Presentation generation is outside the agreed capability set. `nature-image2ppt` additionally carries its own MIT LICENSE (988 KB, 66 code files). |
| `nature-paper-to-patent`, `nature-paper-trans` | Outside scope (patent drafting; bulk translation). |

### Scope exclusions (revisit if scope changes)

`nature-writing` was vendored 2026-10-22 (see the Vendored table and its
boundary condition above), so it is no longer an exclusion.

| Skill | Reason |
|---|---|
| `nature-citation` | Restricted to Nature Portfolio / AAAS Science / Cell Press. Our users write degree theses, so `gb-t-7714` remains the priority. **The mechanism is worth stealing** — split passage into citable segments → search → bind → export RIS/BibTeX is our span-level binding design. |
| `nature-reviewer` | Mock peer review; pairs naturally with `nature-response`. Deferred, not rejected. |
| `nature-proposal-writer` | 开题报告 / 研究计划书 state machine (frontmatter name is `researchwrite`; MIT-licensed). Overlaps `nature-social-science`, which already covers 开题报告 and 选题论证. |
| `nature-literature-pipeline` | Stable, but cron-driven daily literature push is a different product shape. MIT-licensed. |
| `nature-reader`, `nature-data`, `nature-experiment-log`, `nature-paper-xray`, `nature-paper-card` | Cheap (mostly zero code) but outside the three agreed capabilities. |
| `nature-suite` | **Does not exist in the upstream repository** — it is present only in the locally installed copy under `~/.qoder/skills/`, whose provenance and license are unverified. Independently: it is a *router* skill ("This skill loads first and routes"), which would compete with our own intent cascade. Never vendor a second router. |

## Installer review — why `install.sh` must not be run

`nature-academic-search/install.sh` is retained here as a documented specimen
of the "third-party skill = arbitrary code execution" threat model. It is
**not vendored and must not be executed**. Observed behaviour:

- `pip install --quiet mcp requests toml lxml` — **no version pins**, no hashes
- writes into `~/.claude/mcp_servers/` and `~/.claude/skills/`
- **merges itself into `~/.claude/.mcp.json`** and appends itself to
  `settings.json`'s `enabledMcpjsonServers` — silent self-activation with no
  user approval step
- interpolates a shell argument (`PUBMED_EMAIL`) directly into a Python
  heredoc and into generated JSON
- the MCP server it installs exposes `search_google_scholar`, documented
  upstream as **scraped**, plus `search_scopus` and `search_webofscience`,
  which require institutional subscriptions

Contrast with the supply-chain posture we are adopting (exact pins,
`min-release-age`, `--ignore-scripts`, lifecycle-script allowlist).

If the literature-search MCP capability is wanted, vendor the Python sources
under `packages/mcp-servers/literature-search/`, pin dependencies, review the
code, and launch it as a subprocess from our own gateway behind a tool
whitelist. Note that CrossRef and arXiv require no API key and PubMed requires
only a contact email — so this path is **not** blocked on the Semantic Scholar
key.

## Local install ≠ upstream

The copy under `C:\Users\Administrator\.qoder\skills\` does **not** match the
pinned upstream commit. It contains `nature-academic-search`, `nature-citation`,
`nature-data` and `nature-suite`, which are absent from or different in the
repository skill index, and it has **no LICENSE file**. Its provenance is
unverified, so nothing was vendored from it. All files here came from the
GitHub clone at the pinned commit.

## Re-vendoring

To refresh to a newer upstream commit:

1. Clone and check out the target commit.
2. Re-run the code review above on every executable file. Any new dependency,
   network call, or subprocess is a blocking finding.
3. Re-copy the seven directories verbatim, then re-apply and re-record every
   entry in the modification log below.
4. Update the commit SHA and retrieval date in `NOTICE` and in this file.
5. Re-verify that all `../nature-shared/...` references still resolve.

## Modification log

No vendored file has been modified — all are verbatim copies as of `19be8d4`.
The log below records vendoring *events* (a skill added), not content edits.

| Date | File | Change |
|---|---|---|
| 2026-10-22 | `nature-writing/` (77 files) | Vendored verbatim from `19be8d4` (byte-identical on `main` `c6ae86c`); 0 executables, nothing stripped. It carries 13 unique `../nature-shared` refs (42 occurrences), 2 of them new to the tree — bringing the seven-directory unique total to 14 — and all resolve against the vendored `nature-shared`. No content modified. |

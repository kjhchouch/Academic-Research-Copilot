# Test fixtures

Two kinds of file live here, and the difference matters — a mapping test is only
worth anything if the input really came from the API it claims to test.

## Captured from the live API

Both captured **2026-10-19** from `https://api.openalex.org`, unkeyed, and both
are the same work — `W3133849783`, *"Contrastive Learning for Sequential
Recommendation"*, ICDE 2022 — so the two compose into a realistic round trip:
search finds it, `get_paper` resolves its bibliography.

| File | Captured with | Notes |
|---|---|---|
| `search_works.json` | `GET /works?search=contrastive learning&per-page=1` | Work object trimmed to the keys `select=_WORK_FIELDS` asks for. The 39 dropped keys are the reason `select` is used at all. |
| `work_detail.json` | `GET /works/W3133849783?select=…​,referenced_works` | Verbatim, nothing removed. |

`search_works.json` keeps `meta` **verbatim, including `meta.notice`**. That is
deliberate: `notice` is a paragraph of prose addressed to an AI agent, instructing
it to POST the user's email address to `https://api.openalex.org/api-keys` to be
issued a key. It may be legitimate vendor design, but it is an instruction
arriving on a data channel, and `test_server.py` asserts that not one character of
it reaches a tool result. Regenerating these fixtures without `meta` would delete
the only copy of the thing being tested.

Semantic Scholar is **not** represented. Unkeyed requests to its Graph API return
429 on the first call from a shared egress IP, so there was nothing to capture;
its fixtures are synthetic and its live path is unverified. See the D5 entry in
`docs/devlog.md`.

## Synthetic

| File | Why it is not real |
|---|---|
| `references_batch.json` | Placeholder titles, prefixed `[fixture]`. The three IDs are real and are the first three of `work_detail.json`'s `referenced_works`. |

The synthetic file is built to break two specific things rather than to look
plausible: the rows come back **out of order** relative to the IDs asked for, and
one requested ID (`W1522301498`) is **absent** from the response. Both are
behaviours `OpenAlexClient.resolve_references` has to handle — restore the
requested order, and still emit an entry for a reference that never resolved.

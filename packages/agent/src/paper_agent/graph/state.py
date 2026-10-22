"""Graph state.

Week 1 keeps this small on purpose. Every field added here is a field that has to
be checkpointed, migrated, and reasoned about in every node, so nothing joins
until a node actually reads it.

What is here, and who reads it:

    messages      the conversation. The `agent` node reads it and returns the
                  messages it produced; `add_messages` appends them.
    ui_context    editor signals (active_panel, cursor_section, selected_text,
                  paper_stage). The `route` node folds them into `RouteInput`.
    D4 routing    intent, guard, needs_clarify, path_deep, route_source,
                  route_confidence, route_from_cache. The `route` node writes all
                  of them -- the model-backed `Decisions` plus the L0 gate's
                  verdict. The conditional edge after `route` reads `guard` to
                  pick the exit; the gateway reads the rest to emit one `route`
                  SSE event. `guard` is what makes `clarify` and `refuse`
                  first-class graph exits rather than error paths, and rather than
                  good behaviour requested of a model in the system prompt.

Still planned, in the order they arrive:

    D4  slots, missing_slots -- what a `clarify` should ask for. Held back because
        no node reads them yet: `needs_clarify` is a single bit and the clarify
        message is fixed. They earn their place only once that message is
        generated from the specific missing slot instead of listing every option.
    Week 2  claims, spans, verdict, retry_count  -- grounding and the
        Verifier -> Drafter loop. retry_count lives in state (not in a node
        closure) so the bound survives a checkpoint restore.
    Week 3  retrieved  -- chunk ids and scores, not the chunk text. Large
        objects go to the blob store; a fat state makes every checkpoint write
        expensive and checkpoints get written often.
"""

from __future__ import annotations

from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

# Imported at runtime, not under TYPE_CHECKING: LangGraph resolves these
# annotations to build the state schema and find the `add_messages` reducer, so
# the names have to exist when the class is inspected. Pulling them from the
# routing package is a one-way dependency -- nothing under routing/ imports the
# graph -- and `graph/nodes.py` imports the same package for `guard_from`.
from paper_agent.routing.decision_model import Intent, RouteSource
from paper_agent.routing.guard import Guard


class AgentState(TypedDict):
    """`add_messages` is a reducer, not an assignment.

    That distinction is the whole reason LangGraph exists for this use case: a
    node returns only the messages it produced, and the reducer appends them
    (matching on id, so a returned message with an existing id replaces rather
    than duplicates). Without a reducer, two nodes writing `messages` in the
    same superstep would clobber each other.
    """

    messages: Annotated[list[AnyMessage], add_messages]

    # Editor signals: active_panel, cursor_section, selected_text, paper_stage.
    # No reducer, so it is last-write-wins -- correct, because these describe
    # where the user is right now, and a stale cursor position is worse than no
    # cursor position. Carried from D3 so the D4 cascade reads a field that
    # already exists instead of needing a state migration in the same change
    # that introduces routing.
    ui_context: NotRequired[dict[str, Any] | None]

    # --- D4 routing outcome -------------------------------------------------
    # Written together by the `route` node. NotRequired because a graph built
    # without a router -- the local-first boot with no backend configured, and
    # every existing graph test -- never runs that node, and a cold invoke has not
    # reached it yet. `guard` is the only one the graph itself branches on; the
    # rest ride along so the gateway can emit a complete `route` event and the
    # trace can attribute the decision to a backend (route_source), a confidence,
    # and a cache hit, without a second lookup.
    intent: NotRequired[Intent]
    guard: NotRequired[Guard]
    needs_clarify: NotRequired[bool]
    path_deep: NotRequired[bool]
    route_source: NotRequired[RouteSource]
    route_confidence: NotRequired[float]
    route_from_cache: NotRequired[bool]

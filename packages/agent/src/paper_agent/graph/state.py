"""Graph state.

Week 1 keeps this to one field on purpose. Every field added here is a field
that has to be checkpointed, migrated, and reasoned about in every node, so
nothing joins until a node actually reads it.

Planned additions, in the order they arrive:
    D4  intent, slots, missing_slots, guard, route_source  -- the routing
        cascade writes these and the Supervisor conditional edge reads them.
        `guard` is what makes `clarify` and `refuse` first-class graph exits
        rather than error paths.
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

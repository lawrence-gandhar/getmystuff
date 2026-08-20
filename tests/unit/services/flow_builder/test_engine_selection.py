"""
Tests for what a Menu/Dropdown selection carries into the rest of its turn.

A button reply is the one visitor message with no text in it: the widget sends an
empty ``message`` and puts the choice in ``selected_value``. For a long time the
engine used that value for exactly one thing — picking the outgoing edge — and then
dropped it, which broke the two places downstream that need to know what was picked:

* **An AI Fallback node reached straight from a Menu.** It was handed ``""`` as the
  visitor's question, so it searched its knowledge base for nothing and asked the
  model nothing. A chatbot with a scoped system prompt answers that with its
  out-of-scope refusal, which reads to the operator as a broken flow.
* **An If/Else node further down.** With the choice recorded nowhere, no branch
  could tell one option from another.

So both properties are asserted here by trying to break them, and the "unknown
selection re-asks" behaviour is pinned alongside them because it shares the branch.

These are deliberately DB-free: every function under test is pure apart from the
session object it mutates, and an in-memory model instance is a faithful stand-in.
"""

from __future__ import annotations

import pytest

from app.models.flow_builder import ChatbotFlowSession
from app.services.flow_builder.engine_service import (
    _deliver_reply_to_waiting_node,
    _effective_message,
    _selected_option,
)

PYTHON_OPTION_ID = "opt_python"
PHP_OPTION_ID = "opt_php"
MENU_ID = "menu_1"
AI_NODE_ID = "ai_1"


def _graph(menu_data: dict) -> dict:
    """A Start -> Menu -> AI Fallback graph, with only the Python option wired up."""
    return {
        "nodes": [
            {"id": "start", "type": "start", "data": {}},
            {"id": MENU_ID, "type": "menu", "data": menu_data},
            {"id": AI_NODE_ID, "type": "ai_fallback", "data": {}},
        ],
        "edges": [
            {"source": "start", "target": MENU_ID, "source_port": "default"},
            {"source": MENU_ID, "target": AI_NODE_ID, "source_port": PYTHON_OPTION_ID},
        ],
    }


def _menu_data(variable_name: str = "") -> dict:
    data = {
        "prompt_text": "select a department",
        "options": [
            {"id": PYTHON_OPTION_ID, "label": "Python", "value": "Python"},
            {"id": PHP_OPTION_ID, "label": "PHP", "value": "PHP"},
        ],
    }
    if variable_name:
        data["variable_name"] = variable_name
    return data


def _session_at(node_id: str) -> ChatbotFlowSession:
    session = ChatbotFlowSession()
    session.current_node_id = node_id
    session.variables = {}
    session.status = "active"
    return session


# --------------------------------------------------------------------------
# The chosen option becomes the turn's question
# --------------------------------------------------------------------------

def test_selection_is_carried_forward_as_the_visitor_message():
    """The label the visitor clicked is what the next node is asked about."""
    graph = _graph(_menu_data())
    session = _session_at(MENU_ID)

    option = _selected_option(graph, session, PYTHON_OPTION_ID)
    _deliver_reply_to_waiting_node(graph, session, "", PYTHON_OPTION_ID, option)

    assert session.current_node_id == AI_NODE_ID
    assert _effective_message("", option) == "Python"


def test_typed_text_wins_over_a_selection():
    """The visitor's own words are the better question when both are present."""
    option = {"id": PYTHON_OPTION_ID, "label": "Python", "value": "Python"}

    assert _effective_message("what were last month's numbers?", option) == (
        "what were last month's numbers?"
    )


def test_option_without_a_label_falls_back_to_its_value():
    assert _effective_message("", {"id": PHP_OPTION_ID, "value": "PHP"}) == "PHP"


def test_no_text_and_no_selection_is_an_empty_message():
    assert _effective_message("", None) == ""
    assert _effective_message(None, None) == ""


def test_selected_option_is_resolved_from_the_waiting_node_only():
    """
    An id that belongs to no option on the current node resolves to nothing —
    it must not reach into another node's options.
    """
    graph = _graph(_menu_data())

    assert _selected_option(graph, _session_at(MENU_ID), "opt_nonexistent") is None
    assert _selected_option(graph, _session_at(AI_NODE_ID), PYTHON_OPTION_ID) is None
    assert _selected_option(graph, _session_at(MENU_ID), "") is None


# --------------------------------------------------------------------------
# The chosen option is recorded for downstream branching
# --------------------------------------------------------------------------

def test_selection_is_stored_under_the_configured_variable():
    graph = _graph(_menu_data(variable_name="department"))
    session = _session_at(MENU_ID)

    option = _selected_option(graph, session, PYTHON_OPTION_ID)
    _deliver_reply_to_waiting_node(graph, session, "", PYTHON_OPTION_ID, option)

    assert session.variables == {"department": "Python"}


def test_variables_are_reassigned_not_mutated():
    """
    `variables` is a plain JSONB column, so an in-place write is invisible to
    SQLAlchemy's change tracking and would never persist.
    """
    graph = _graph(_menu_data(variable_name="department"))
    session = _session_at(MENU_ID)
    original = session.variables

    option = _selected_option(graph, session, PYTHON_OPTION_ID)
    _deliver_reply_to_waiting_node(graph, session, "", PYTHON_OPTION_ID, option)

    assert session.variables is not original
    assert original == {}


def test_menu_without_a_variable_name_stores_nothing():
    """Naming a variable stays optional — most menus only ever route."""
    graph = _graph(_menu_data())
    session = _session_at(MENU_ID)

    option = _selected_option(graph, session, PYTHON_OPTION_ID)
    _deliver_reply_to_waiting_node(graph, session, "", PYTHON_OPTION_ID, option)

    assert session.variables == {}
    assert session.current_node_id == AI_NODE_ID


# --------------------------------------------------------------------------
# Unwired and unknown options still re-ask
# --------------------------------------------------------------------------

@pytest.mark.parametrize("selected", [PHP_OPTION_ID, "opt_stale_from_an_old_graph"])
def test_a_selection_with_no_edge_re_asks_the_same_menu(selected: str):
    """
    PHP is a real option with no connector, the other id is left over from an
    edited flow. Neither can advance, and neither may move the session or record
    a variable — the visitor is simply asked again.
    """
    graph = _graph(_menu_data(variable_name="department"))
    session = _session_at(MENU_ID)

    option = _selected_option(graph, session, selected)
    result = _deliver_reply_to_waiting_node(graph, session, "", selected, option)

    assert result is not None
    assert result.type == "buttons"
    assert result.text == "select a department"
    assert session.current_node_id == MENU_ID
    assert session.variables == {}


# --------------------------------------------------------------------------
# Ask Input is unchanged by the shared storage helper
# --------------------------------------------------------------------------

def test_ask_input_still_stores_its_typed_answer():
    graph = {
        "nodes": [
            {"id": "ask_1", "type": "ask_input", "data": {"variable_name": "email"}},
            {"id": AI_NODE_ID, "type": "ai_fallback", "data": {}},
        ],
        "edges": [{"source": "ask_1", "target": AI_NODE_ID, "source_port": "default"}],
    }
    session = _session_at("ask_1")

    _deliver_reply_to_waiting_node(graph, session, "  me@example.com  ", None, None)

    assert session.variables == {"email": "me@example.com"}
    assert session.current_node_id == AI_NODE_ID

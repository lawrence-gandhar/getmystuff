"""
Tests for `_validate_download_file_data` — the save-time check that a Download File
block's named Create File block(s) are real, and its button colour is safe to put in a
`style` attribute.

`create_file_node_id` may be a single id (the shape saved before a Download File block
could name more than one) or a list of them (several branches sharing one hand-over
block — see `downloadFileFieldsHtml` in `flow_builder.js`). What matters at save time is
unchanged either way: every named id must exist on the canvas and be a Create File block.
Which one the *engine* hands over when several have run is a separate question, covered by
`TestSeveralSources` in `test_engine_file_nodes.py`.
"""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

from app.services.flow_builder.flow_service import _validate_download_file_data

MAKE_ID = "make_1"
OTHER_MAKE_ID = "make_2"
NOT_A_MAKER_ID = "end_1"

_NODE_BY_ID = {
    MAKE_ID: {"id": MAKE_ID, "type": "create_file", "data": {}},
    OTHER_MAKE_ID: {"id": OTHER_MAKE_ID, "type": "create_file", "data": {}},
    NOT_A_MAKER_ID: {"id": NOT_A_MAKER_ID, "type": "end", "data": {}},
}


class TestNamingNoBlockAtAll:
    def test_an_empty_string_is_refused(self) -> None:
        with pytest.raises(HTTPException) as caught:
            _validate_download_file_data({"create_file_node_id": ""}, _NODE_BY_ID)

        assert "must name at least one Create File block" in str(caught.value.detail)

    def test_an_empty_list_is_refused_the_same_way(self) -> None:
        with pytest.raises(HTTPException) as caught:
            _validate_download_file_data({"create_file_node_id": []}, _NODE_BY_ID)

        assert "must name at least one Create File block" in str(caught.value.detail)

    def test_a_list_of_only_blanks_is_refused(self) -> None:
        with pytest.raises(HTTPException) as caught:
            _validate_download_file_data({"create_file_node_id": ["", "  "]}, _NODE_BY_ID)

        assert "must name at least one Create File block" in str(caught.value.detail)


class TestTheSingleLegacyShape:
    """Every field saved before a Download File block could name more than one is a plain
    string. It must still validate exactly as it always did."""

    def test_a_real_create_file_block_passes(self) -> None:
        _validate_download_file_data(
            {"create_file_node_id": MAKE_ID, "show_button": False}, _NODE_BY_ID,
        )

    def test_a_block_that_is_not_on_the_canvas_is_refused(self) -> None:
        with pytest.raises(HTTPException) as caught:
            _validate_download_file_data(
                {"create_file_node_id": "missing"}, _NODE_BY_ID,
            )

        assert "must point at a Create File block" in str(caught.value.detail)

    def test_a_block_that_exists_but_is_not_a_create_file_block_is_refused(self) -> None:
        with pytest.raises(HTTPException) as caught:
            _validate_download_file_data(
                {"create_file_node_id": NOT_A_MAKER_ID}, _NODE_BY_ID,
            )

        assert "must point at a Create File block" in str(caught.value.detail)


class TestTheListShape:
    def test_several_real_create_file_blocks_pass(self) -> None:
        _validate_download_file_data(
            {"create_file_node_id": [MAKE_ID, OTHER_MAKE_ID], "show_button": False},
            _NODE_BY_ID,
        )

    def test_one_bad_id_among_good_ones_is_still_refused(self) -> None:
        """One dangling reference is enough — the block should not offer a link that
        might come from a block that no longer exists."""
        with pytest.raises(HTTPException) as caught:
            _validate_download_file_data(
                {"create_file_node_id": [MAKE_ID, "missing"]}, _NODE_BY_ID,
            )

        assert "must point at a Create File block" in str(caught.value.detail)

    def test_blank_entries_in_an_otherwise_good_list_are_dropped_not_refused(self) -> None:
        """A `[MAKE_ID, ""]` shape is not something the UI writes, but should not be
        refused for a reason unrelated to the block the operator actually named."""
        _validate_download_file_data(
            {"create_file_node_id": [MAKE_ID, ""], "show_button": False}, _NODE_BY_ID,
        )

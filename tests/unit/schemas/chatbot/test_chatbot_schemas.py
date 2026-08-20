"""
Tests for app/schemas/chatbot/chatbot_schemas.py.

This module covers the only *unauthenticated* payload in the application, and the
tests are weighted accordingly. ``PublicChatbotMessageRequest`` is reached by
anonymous visitors on third-party sites, every accepted message is a model call the
owner pays for, and the body previously went through ``(body or {}).get(...)`` —
which raised ``AttributeError`` and a 500 when the body parsed to a list.

The other cluster worth reading is the target-selection rule. What identifies a
chatbot's data depends on ``target_type``, and the two hand-rolled ``uuid.UUID()``
conversions this replaced were the only thing between a mistyped selection and a
database error.
"""

from __future__ import annotations

import uuid

import pytest
from litestar.exceptions import HTTPException

from app.models.chatbot import ACTION_HTTP_METHODS
from app.schemas.chatbot import (
    MAX_MESSAGE_LENGTH,
    MAX_SESSION_TOKEN_LENGTH,
    TARGET_TYPES,
    ChatbotActionAttachRequest,
    ChatbotActionRequest,
    ChatbotAiSettingsRequest,
    ChatbotCreateRequest,
    ChatbotDataAgentRequest,
    ChatbotFlowRequest,
    ChatbotKeyView,
    ChatbotSettingsTabQuery,
    ChatbotTurnResponse,
    ChatbotUpdateRequest,
    WidgetAppearanceRequest,
)

VALID_UUID = "3f4b2c1e-0000-4000-8000-000000000001"
OTHER_UUID = "3f4b2c1e-0000-4000-8000-000000000002"
AGENT_UUID = "3f4b2c1e-0000-4000-8000-000000000003"


def _detail(schema, data: dict) -> str:
    with pytest.raises(HTTPException) as exc_info:
        schema.parse(data)
    return str(exc_info.value.detail)


class _Turn:
    """
    Stand-in for chatbot_turn_service.TurnResult.

    A stub rather than the real dataclass because importing it would pull the deep
    agent stack — and langgraph with it — into a schema test. The cost is that it has
    to be kept in step by hand, so every field ``from_turn`` reads is listed here.
    """

    def __init__(self, **kwargs) -> None:
        self.status = "success"
        self.type = "text"
        self.summary = ""
        self.insights: list = []
        self.table = None
        self.options: list = []
        self.message = ""
        self.response_time_ms = 0
        self.download = None
        self.__dict__.update(kwargs)


# --------------------------------------------------------------------------
# Create — the target selection rule
# --------------------------------------------------------------------------

def _create(**extra) -> dict:
    return {"name": "Support bot", "datasource_id": VALID_UUID, **extra}


class TestChatbotCreate:
    def test_a_datasource_wide_target_needs_no_selection(self) -> None:
        payload = ChatbotCreateRequest.parse(_create(target_type="datasource"))
        assert payload.target_selection == []
        assert payload.target_names == []
        assert payload.file_ids == []

    def test_a_table_target_keeps_every_selected_name(self) -> None:
        """
        The multi-select case. Read as a single value this would scope the agent
        to one table when the user picked three.
        """
        payload = ChatbotCreateRequest.parse(
            _create(target_type="table", target_selection=["a", "b", "c"])
        )
        assert payload.target_names == ["a", "b", "c"]
        assert payload.file_ids == []

    def test_a_file_target_keeps_every_selected_uuid(self) -> None:
        payload = ChatbotCreateRequest.parse(
            _create(target_type="file", target_selection=[VALID_UUID, OTHER_UUID])
        )
        assert payload.file_ids == [VALID_UUID, OTHER_UUID]
        assert payload.target_names == []

    @pytest.mark.parametrize("target_type", ["table", "collection", "file"])
    def test_a_scoped_target_must_name_what_it_is_scoped_to(
        self, target_type: str
    ) -> None:
        assert _detail(
            ChatbotCreateRequest, _create(target_type=target_type, target_selection=[])
        ) == f"Please choose at least one {target_type}"

    def test_a_file_selection_that_is_not_a_uuid_is_refused(self) -> None:
        """Keeps the wording the route's own except-branch used."""
        assert _detail(
            ChatbotCreateRequest,
            _create(target_type="file", target_selection=["sales_data"]),
        ) == "Invalid file reference."

    def test_a_table_selection_that_could_break_an_identifier_is_refused(self) -> None:
        assert "is not a valid name" in _detail(
            ChatbotCreateRequest,
            _create(target_type="table", target_selection=["sales; drop table x"]),
        )

    def test_the_datasource_is_required(self) -> None:
        """Required for every target type but ``agent`` — see TestAgentTarget."""
        assert _detail(
            ChatbotCreateRequest, {"name": "b", "target_type": "datasource"}
        ) == "Please select a data source"

    def test_a_malformed_datasource_is_refused(self) -> None:
        assert _detail(
            ChatbotCreateRequest,
            {"name": "b", "datasource_id": "nope", "target_type": "datasource"},
        ) == "Data source is not a valid selection"

    def test_the_name_is_required(self) -> None:
        assert _detail(
            ChatbotCreateRequest,
            {"datasource_id": VALID_UUID, "target_type": "datasource"},
        ) == "Agent name is required"

    @pytest.mark.parametrize("target_type", sorted(TARGET_TYPES))
    def test_every_declared_target_type_is_accepted(self, target_type: str) -> None:
        """
        ``agent`` is built differently on purpose: it is the one target that
        carries a data agent *instead of* a datasource, so feeding it the shared
        ``_create`` payload would be asserting the opposite of the rule.
        """
        if target_type == "agent":
            data = {"name": "Support bot", "target_type": "agent",
                    "data_agent_id": AGENT_UUID}
        else:
            data = _create(target_type=target_type)
            if target_type != "datasource":
                data["target_selection"] = (
                    [VALID_UUID] if target_type == "file" else ["sales_data"]
                )
        assert ChatbotCreateRequest.parse(data).target_type == target_type


class TestAgentTarget:
    """
    ``target_type == "agent"`` — a widget with no datasource of its own, whose
    attached agent's tool configs are the scope.

    The rule is a pairing across three fields, which is why it lives in a model
    validator rather than on any one of them: an agent target needs an agent, and
    must *not* carry a datasource. Both halves matter. Accepting a datasource
    alongside would leave two answers to "what can this widget read?", and which one
    applied would depend on whether the agent happened to run.
    """

    def test_an_agent_target_needs_no_datasource(self) -> None:
        payload = ChatbotCreateRequest.parse(
            {"name": "b", "target_type": "agent", "data_agent_id": AGENT_UUID}
        )

        assert payload.datasource_id is None
        assert str(payload.data_agent_id) == AGENT_UUID
        assert payload.target_names == []
        assert payload.file_ids == []

    def test_a_workspace_may_ride_along(self) -> None:
        """The picker is a workspace -> agent cascade; the workspace is remembered
        so the form reopens on the right branch."""
        payload = ChatbotCreateRequest.parse({
            "name": "b",
            "target_type": "agent",
            "data_agent_id": AGENT_UUID,
            "workspace_id": OTHER_UUID,
        })

        assert str(payload.workspace_id) == OTHER_UUID

    def test_an_agent_target_without_an_agent_is_refused(self) -> None:
        """Selecting only a workspace lands here — a workspace groups agents and
        points at no data itself, so the widget would answer nothing."""
        assert _detail(
            ChatbotCreateRequest,
            {"name": "b", "target_type": "agent", "workspace_id": OTHER_UUID},
        ) == (
            "Please choose a data agent, or pick a data source for this widget to "
            "answer from"
        )

    def test_an_agent_target_carrying_a_datasource_is_refused(self) -> None:
        """Not ignored. A submission with both is a form that got out of step with
        itself, and dropping one answer silently is how a widget ends up scoped to
        something nobody chose."""
        assert "no data source of its own" in _detail(
            ChatbotCreateRequest,
            {
                "name": "b",
                "target_type": "agent",
                "data_agent_id": AGENT_UUID,
                "datasource_id": VALID_UUID,
            },
        )

    def test_a_datasource_target_still_requires_a_datasource(self) -> None:
        """The other half of the rule: attaching an agent does not excuse a widget
        that says it is scoped to a datasource from naming one."""
        assert _detail(
            ChatbotCreateRequest,
            {"name": "b", "target_type": "datasource", "data_agent_id": AGENT_UUID},
        ) == "Please select a data source"

    def test_a_table_target_with_an_agent_still_needs_its_tables(self) -> None:
        assert "at least one table" in _detail(
            ChatbotCreateRequest,
            {
                "name": "b",
                "target_type": "table",
                "datasource_id": VALID_UUID,
                "data_agent_id": AGENT_UUID,
            },
        )


    def test_an_untouched_agent_picker_means_no_agent(self) -> None:
        """The pre-Deep-Agents behaviour, preserved."""
        payload = ChatbotCreateRequest.parse(
            _create(target_type="datasource", workspace_id="", data_agent_id="")
        )
        assert payload.workspace_id is None
        assert payload.data_agent_id is None


class TestChatbotUpdate:
    def test_absent_fields_mean_leave_them_alone(self) -> None:
        payload = ChatbotUpdateRequest.parse({})
        assert payload.name is None
        assert payload.allowed_origins is None

    def test_a_blank_allow_list_stays_an_empty_string(self) -> None:
        """
        ``update_chatbot_key`` reads ``""`` as "clear the origin allow-list" — a
        security change the user meant to make. Collapsing it to ``None`` would
        make emptying the field a silent no-op.
        """
        assert ChatbotUpdateRequest.parse({"allowed_origins": ""}).allowed_origins == ""

    def test_the_datasource_target_cannot_be_edited(self) -> None:
        """
        Repointing a published widget at different data changes what every
        embedded copy answers about, so it is not offered.
        """
        for field in ("datasource_id", "target_type", "target_selection"):
            assert field not in ChatbotUpdateRequest.model_fields


class TestSettingsTabQuery:
    @pytest.mark.parametrize("tab", ["appearance", "ai", "actions"])
    def test_a_known_tab_is_kept(self, tab: str) -> None:
        assert ChatbotSettingsTabQuery.parse({"tab": tab}).tab == tab

    @pytest.mark.parametrize("tab", ["nope", "", None])
    def test_an_unknown_tab_falls_back_rather_than_erroring(self, tab) -> None:
        """A stale bookmark should still open the page."""
        assert ChatbotSettingsTabQuery.parse({"tab": tab}).tab == "appearance"


class TestAiSettings:
    def _valid(self, **extra) -> dict:
        return {
            "agent_name": "Ada",
            "system_prompt": "Answer only from the knowledge base.",
            "llm_mode": "in_built",
            **extra,
        }

    def test_a_valid_form(self) -> None:
        payload = ChatbotAiSettingsRequest.parse(self._valid())
        assert payload.llm_api_key_id == ""

    def test_the_prompt_is_required(self) -> None:
        assert _detail(
            ChatbotAiSettingsRequest, self._valid(system_prompt="  ")
        ) == "System prompt is required"

    def test_an_unknown_model_choice_is_refused(self) -> None:
        assert _detail(ChatbotAiSettingsRequest, self._valid(llm_mode="magic")) == (
            "Model choice is not one of the available options"
        )

    def test_the_key_id_stays_a_string_so_blank_keeps_its_meaning(self) -> None:
        """
        The service reads ``""`` as "resolve the user's active keys as usual" — a
        three-way meaning (a key / any key / invalid) it already implements.
        """
        assert ChatbotAiSettingsRequest.parse(
            self._valid(llm_api_key_id="")
        ).llm_api_key_id == ""


class TestFlowAttachment:
    def test_a_blank_selection_clears_the_flow(self) -> None:
        assert ChatbotFlowRequest.parse({"flow_id": ""}).flow_id is None

    def test_a_valid_selection_is_parsed(self) -> None:
        assert ChatbotFlowRequest.parse({"flow_id": VALID_UUID}).flow_id == (
            uuid.UUID(VALID_UUID)
        )

    def test_the_modules_own_wording_is_kept_for_a_bad_selection(self) -> None:
        assert _detail(ChatbotFlowRequest, {"flow_id": "nope"}) == (
            "That flow selection was not valid. Please pick a flow from the list."
        )


class TestDataAgentAttachment:
    def test_blank_selections_clear_the_attachment(self) -> None:
        payload = ChatbotDataAgentRequest.parse({"workspace_id": "", "data_agent_id": ""})
        assert payload.workspace_id is None
        assert payload.data_agent_id is None


class TestActionForm:
    def _valid(self, **extra) -> dict:
        return {"name": "Create ticket", "http_method": "POST", "url": "https://x/y", **extra}

    def test_a_valid_action(self) -> None:
        payload = ChatbotActionRequest.parse(self._valid())
        assert payload.timeout_seconds == 10
        assert payload.description is None

    @pytest.mark.parametrize("method", ACTION_HTTP_METHODS)
    def test_every_declared_method_is_accepted(self, method: str) -> None:
        assert ChatbotActionRequest.parse(
            self._valid(http_method=method)
        ).http_method == method

    def test_a_lowercase_method_is_normalized(self) -> None:
        assert ChatbotActionRequest.parse(self._valid(http_method="get")).http_method == (
            "GET"
        )

    def test_an_unsupported_method_is_refused(self) -> None:
        assert "must be one of" in _detail(
            ChatbotActionRequest, self._valid(http_method="TRACE")
        )

    @pytest.mark.parametrize(
        "url", ["file:///etc/passwd", "gopher://x", "ftp://x", "javascript:alert(1)"]
    )
    def test_a_non_http_scheme_never_reaches_the_http_client(self, url: str) -> None:
        assert _detail(ChatbotActionRequest, self._valid(url=url)) == (
            "URL must start with http:// or https://"
        )

    def test_a_blank_timeout_uses_the_default(self) -> None:
        assert ChatbotActionRequest.parse(
            self._valid(timeout_seconds="")
        ).timeout_seconds == 10

    @pytest.mark.parametrize("timeout", ["0", "31", "900"])
    def test_a_timeout_outside_the_services_range_is_refused(self, timeout: str) -> None:
        """
        The bounds mirror ``chatbot_action_service._TIMEOUT_RANGE``. If they
        drifted, the form would accept a value the save then rejected.
        """
        with pytest.raises(HTTPException):
            ChatbotActionRequest.parse(self._valid(timeout_seconds=timeout))

    def test_a_non_numeric_timeout_gets_a_sentence(self) -> None:
        assert _detail(ChatbotActionRequest, self._valid(timeout_seconds="soon")) == (
            "Timeout must be a whole number"
        )


class TestActionAttach:
    def test_a_valid_selection(self) -> None:
        assert ChatbotActionAttachRequest.parse({"action_id": VALID_UUID}).action_id == (
            uuid.UUID(VALID_UUID)
        )

    def test_an_empty_picker_keeps_the_tabs_own_wording(self) -> None:
        assert _detail(ChatbotActionAttachRequest, {"action_id": ""}) == (
            "Please select an action to add."
        )


class TestPublicMessage:
    """The one payload an anonymous caller controls completely."""

    def test_a_valid_turn(self) -> None:
        from app.schemas.chatbot import PublicChatbotMessageRequest

        payload = PublicChatbotMessageRequest.parse(
            {"api_key": "pk_1", "message": "hello", "session_id": "s-1"}
        )
        assert payload.selected_value is None

    def test_the_api_key_is_required(self) -> None:
        from app.schemas.chatbot import PublicChatbotMessageRequest

        assert _detail(PublicChatbotMessageRequest, {"message": "hi"}) == (
            "API key is required"
        )

    def test_the_message_is_bounded_because_each_one_is_a_paid_call(self) -> None:
        from app.schemas.chatbot import PublicChatbotMessageRequest

        at_cap = "m" * MAX_MESSAGE_LENGTH
        assert PublicChatbotMessageRequest.parse(
            {"api_key": "k", "message": at_cap}
        ).message == at_cap
        assert "cannot be longer than 4000" in _detail(
            PublicChatbotMessageRequest,
            {"api_key": "k", "message": "m" * (MAX_MESSAGE_LENGTH + 1)},
        )

    def test_the_session_token_is_bounded(self) -> None:
        from app.schemas.chatbot import PublicChatbotMessageRequest

        with pytest.raises(HTTPException):
            PublicChatbotMessageRequest.parse(
                {"api_key": "k", "session_id": "s" * (MAX_SESSION_TOKEN_LENGTH + 1)}
            )

    def test_an_empty_message_is_allowed_because_a_flow_turn_may_have_none(
        self,
    ) -> None:
        """A menu selection posts ``selected_value`` with no typed message."""
        from app.schemas.chatbot import PublicChatbotMessageRequest

        payload = PublicChatbotMessageRequest.parse(
            {"api_key": "k", "selected_value": "option-2"}
        )
        assert payload.message == ""
        assert payload.selected_value == "option-2"

    def test_a_wrongly_typed_field_is_a_400_not_an_attributeerror(self) -> None:
        """
        Regression guard. ``(body or {}).get("message", "")`` returned whatever
        was there, so a list reached the service and failed as a 500.
        """
        from app.schemas.chatbot import PublicChatbotMessageRequest

        with pytest.raises(HTTPException) as exc_info:
            PublicChatbotMessageRequest.parse({"api_key": "k", "message": ["a", "b"]})
        assert exc_info.value.status_code == 400


class TestTurnResponse:
    def test_a_success_carries_the_answer_and_no_message_key(self) -> None:
        payload = ChatbotTurnResponse.from_turn(
            _Turn(summary="42 units", insights=["up 3%"], response_time_ms=120)
        ).payload()

        assert payload["status"] == "success"
        assert payload["summary"] == "42 units"
        assert "message" not in payload

    def test_summary_is_duplicated_as_text_for_the_flow_node_types(self) -> None:
        """The menu / dropdown / ask-input nodes read their prompt from ``text``."""
        payload = ChatbotTurnResponse.from_turn(_Turn(summary="Pick one")).payload()
        assert payload["text"] == payload["summary"] == "Pick one"

    def test_an_error_carries_only_the_message_and_the_timing(self) -> None:
        payload = ChatbotTurnResponse.from_turn(
            _Turn(status="error", message="Could not reach the datasource", response_time_ms=8)
        ).payload()

        assert payload == {
            "status": "error",
            "message": "Could not reach the datasource",
            "response_time_ms": 8,
        }

    def test_the_timing_survives_both_branches(self) -> None:
        """
        What a visitor sees and what the owner sees in Chatbot Analytics come from
        the same number, so it must not be dropped on the error path.
        """
        assert ChatbotTurnResponse.from_turn(
            _Turn(status="error", message="x", response_time_ms=99)
        ).payload()["response_time_ms"] == 99


class TestWidgetAppearance:
    def test_all_twenty_appearance_fields_are_collected(self) -> None:
        """
        The route builds ``WidgetAppearanceInput(**appearance_values())``, so the
        two have to agree on the field set exactly.
        """
        values = WidgetAppearanceRequest.parse({}).appearance_values()
        assert len(values) == 20
        assert set(values) == set(WidgetAppearanceRequest.APPEARANCE_FIELDS)

    def test_the_appearance_field_names_match_the_services_dataclass(self) -> None:
        from dataclasses import fields

        from app.services.chatbot.chatbot_widget_settings_service import (
            WidgetAppearanceInput,
        )

        assert set(WidgetAppearanceRequest.APPEARANCE_FIELDS) == {
            field.name for field in fields(WidgetAppearanceInput)
        }

    def test_the_image_slots_match_the_services_dataclasses(self) -> None:
        from dataclasses import fields

        from app.services.chatbot.chatbot_widget_settings_service import (
            WidgetImageRemovals,
            WidgetImageUploads,
        )

        expected = {field.name for field in fields(WidgetImageUploads)}
        assert set(WidgetAppearanceRequest.IMAGE_FIELDS) == expected
        assert expected == {field.name for field in fields(WidgetImageRemovals)}

    def test_removal_flags_are_keyed_by_image_slot(self) -> None:
        removals = WidgetAppearanceRequest.parse({"remove_logo": "on"}).removal_values()
        assert removals["logo"] is True
        assert removals["bot_icon"] is False

    def test_an_unset_form_yields_empty_strings_for_the_service_to_reject(self) -> None:
        """
        The colour, size and font rules — with their own messages — live in
        ``chatbot_widget_settings_service``. This schema only guarantees shape and
        bounds, so a blank form passes here and is refused there.
        """
        values = WidgetAppearanceRequest.parse({}).appearance_values()
        assert values["brand_color"] == ""


class TestChatbotKeyView:
    def test_the_publishable_key_is_present_and_the_internal_id_is_not(self) -> None:
        """
        ``api_key`` is publishable — it goes into the embed snippet on the
        customer's own site, and its protection is the per-key origin allow-list,
        not secrecy.
        """
        payload = ChatbotKeyView.payload_for(
            {"id": 11, "uuid": "u-1", "name": "Support", "api_key": "pk_live_1"}
        )
        assert payload["api_key"] == "pk_live_1"
        assert "id" not in payload

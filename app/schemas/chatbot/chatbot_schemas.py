"""
app/schemas/chatbot/chatbot_schemas.py

Pydantic schemas for the Chatbot module — the agents ("Agents" in the sidebar,
``ChatbotApiKey`` in the database), their widget appearance, their AI and prompt
settings, their webhook actions, and the public endpoint the embedded widget calls.

This is the largest schema module in the application, and it covers the only
*unauthenticated* payload in it. That distinction drives everything below.

**The public endpoint is the hard boundary.** ``PublicChatbotMessageRequest`` and
``PublicWidgetConfigQuery`` are reached by anonymous visitors on third-party sites.
Every other schema here is behind ``require_auth``. So the public pair is bounded
tightly — a message length, a session token length — because these are the fields
an attacker controls completely, and because each accepted message costs a model
call. The key and origin checks stay in ``chatbot_service``: they need the database
and the request headers, which a schema has neither of.

**The appearance form is validated in the service, not here.** ``WidgetAppearanceInput``
holds twenty fields of colors, sizes and copy whose rules (hex format, pixel
ranges, allowed fonts) already live in ``chatbot_widget_settings_service`` with
their own messages. :class:`WidgetAppearanceRequest` therefore declares the *shape*
— every field present as text — and hands the values to that validator rather than
duplicating twenty range checks that would then have to be kept in step. It exists
so the route stops hand-assembling a dataclass out of twenty ``form.get`` calls.

**Target selection is a cross-field rule.** What identifies a chatbot's data
depends on ``target_type``: file uuids for ``file``, names for ``table`` and
``collection``, nothing for ``datasource``. That rule is checked once, in
:meth:`ChatbotCreateRequest.check_target`, so a half-specified target cannot reach
the service and resolve to something other than what was picked.
"""

import uuid as uuid_pkg
from typing import ClassVar, List, Optional

from pydantic import Field, field_validator, model_validator

from app.models.chatbot import (
    ACTION_HTTP_METHODS,
    ACTION_PARAMETER_TYPES,
    LLM_MODES,
    TARGET_TYPE_AGENT,
)
from app.models.chatbot import TARGET_TYPES as MODEL_TARGET_TYPES
from app.schemas.base import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    MAX_PROMPT_LENGTH,
    MAX_URL_LENGTH,
    CheckboxBool,
    FormRequest,
    JsonRequest,
    OptionalText,
    OptionalUUID,
    QueryRequest,
    RequiredText,
    RequiredUUID,
    ResponseSchema,
)
from app.utils.validators import require_object_name

#: The scopes a chatbot's data can be, taken from the model so the schema, the
#: service and the column cannot drift. ``chatbot_service`` still enforces it.
#: ``agent`` is the one that carries no datasource — see
#: :meth:`ChatbotCreateRequest.check_target`.
TARGET_TYPES: frozenset[str] = frozenset(MODEL_TARGET_TYPES)

#: Target types identified by object name rather than by file uuid.
NAMED_TARGET_TYPES: frozenset[str] = frozenset({"table", "collection"})

#: How a model is chosen for this agent.
LLM_MODE_VALUES: frozenset[str] = frozenset(value for value, _ in LLM_MODES)

#: A visitor's message. Bounded because every accepted message is a paid model
#: call, and because the widget's own input has no length limit of its own.
MAX_MESSAGE_LENGTH = 4000

#: The widget's session token. Opaque to the server, so only its size matters.
MAX_SESSION_TOKEN_LENGTH = 128

#: A value chosen from a flow menu or dropdown, posted back with the next turn.
MAX_SELECTED_VALUE_LENGTH = 1000

#: An agent can be embedded on this many domains. Bounded so the allow-list stays
#: a list a person maintains rather than an unbounded blob.
MAX_ALLOWED_ORIGINS_LENGTH = 4000

#: A chatbot's data can be scoped to at most this many objects.
MAX_TARGET_SELECTIONS = 50

#: The prompt variables the owner defines, as the JSON the form carries.
MAX_VARIABLES_JSON_LENGTH = 20_000

#: An action's request body template and header block.
MAX_ACTION_TEMPLATE_LENGTH = 20_000

#: An action's outbound timeout, in seconds. Mirrors
#: ``chatbot_action_service._TIMEOUT_RANGE`` — the service is still the enforcer,
#: so these two numbers have to move together or the form would accept a value the
#: save then rejects.
MIN_ACTION_TIMEOUT = 1
MAX_ACTION_TIMEOUT = 30

#: An inline SVG for the widget's send button.
MAX_SVG_LENGTH = 20_000

#: Widget copy — a welcome line, an idle nudge, a closing line.
MAX_WIDGET_TEXT_LENGTH = 500

#: The settings page's tabs.
SETTINGS_TABS: frozenset[str] = frozenset({"appearance", "ai", "actions"})


# --------------------------------------------------------------------------
# Agents — create / update
# --------------------------------------------------------------------------

class ChatbotCreateRequest(FormRequest):
    """
    The create-agent form.

    ``datasource_id`` is typed but **conditionally** required, which is the one
    subtlety in this schema. A widget answers either from a datasource target it
    nominates, or from an attached data agent's tool configs — and in the second
    case there is no datasource to name, and asking for one anyway would mean the
    operator picking a table the widget never reads. ``target_type == "agent"`` is
    that case; :meth:`check_target` is where the pairing is enforced, because it
    spans three fields and no single one of them can express it.

    ``workspace_id`` and ``data_agent_id`` are otherwise optional, and blank means
    "no agent" — which is the behaviour that existed before Deep Agents, so an
    untouched form still creates the chatbot it always did.
    """

    multi_fields = ("target_selection",)

    name: RequiredText = Field(title="Agent name", max_length=MAX_NAME_LENGTH)
    datasource_id: OptionalUUID = Field(default=None, title="Data source")
    target_type: RequiredText = Field(title="Target type")
    target_selection: List[str] = Field(
        default_factory=list, title="Target selection", max_length=MAX_TARGET_SELECTIONS
    )
    allowed_origins: str = Field(
        default="", title="Allowed origins", max_length=MAX_ALLOWED_ORIGINS_LENGTH
    )
    workspace_id: OptionalUUID = Field(default=None, title="Workspace")
    data_agent_id: OptionalUUID = Field(default=None, title="Data agent")

    @field_validator("target_type")
    @classmethod
    def validate_target_type(cls, v: str) -> str:
        if v not in TARGET_TYPES:
            raise ValueError(
                "Target type must be one of datasource, file, table, collection "
                "or agent"
            )
        return v

    @model_validator(mode="after")
    def check_target(self) -> "ChatbotCreateRequest":
        """
        A target must name what it is scoped to, and which kind of thing that is
        depends on ``target_type``:

        ============  ==========================================================
        ``agent``     A data agent, and **no** datasource. The agent's tool
                      configs are the scope, so a datasource here would be a
                      second, conflicting answer to "what can this widget read?".
        ``datasource``  A datasource, and nothing further — it means all of it.
        anything else   A datasource, plus at least one table, collection or file.
        ============  ==========================================================

        Rejecting a datasource sent *with* an agent target, rather than ignoring
        it, is deliberate: the form hides those fields when an agent is picked, so
        a submission carrying both is a form that got out of step with itself, and
        silently dropping one of the two answers is how a widget ends up scoped to
        something nobody chose.
        """
        if self.target_type == TARGET_TYPE_AGENT:
            if self.data_agent_id is None:
                raise ValueError(
                    "Please choose a data agent, or pick a data source for this "
                    "widget to answer from"
                )
            if self.datasource_id is not None:
                raise ValueError(
                    "A widget answered by a data agent has no data source of its "
                    "own — the agent's tools decide what it can read"
                )
            return self

        if self.datasource_id is None:
            raise ValueError("Please select a data source")

        if self.target_type == "datasource":
            return self

        if not self.target_selection:
            raise ValueError(f"Please choose at least one {self.target_type}")

        return self

    @property
    def file_ids(self) -> List[str]:
        """The selections, when they identify files."""
        return list(self.target_selection) if self.target_type == "file" else []

    @property
    def target_names(self) -> List[str]:
        """The selections, when they identify tables or collections."""
        if self.target_type not in NAMED_TARGET_TYPES:
            return []
        return list(self.target_selection)

    @model_validator(mode="after")
    def check_selection_shape(self) -> "ChatbotCreateRequest":
        """
        Each selection has to be the right *kind* of value for the target type.

        A ``file`` selection is a uuid and a ``table`` selection is an object name;
        both were previously converted with a bare ``uuid.UUID(s)`` or passed
        through untouched, so a table name reached the service where a file uuid
        was expected and failed as a database error.
        """
        if self.target_type == "file":
            for value in self.target_selection:
                try:
                    uuid_pkg.UUID(str(value))
                except (ValueError, AttributeError, TypeError) as exc:
                    raise ValueError("Invalid file reference.") from exc
            return self

        if self.target_type in NAMED_TARGET_TYPES:
            for value in self.target_selection:
                require_object_name(str(value), "Target selection")

        return self


class ChatbotUpdateRequest(FormRequest):
    """
    The edit-agent form: name and allowed origins only.

    The datasource target is deliberately absent. Repointing a published widget at
    different data is not an edit — it changes what every embedded copy answers
    about — so it is not offered.

    Plain ``Optional[str]`` rather than ``OptionalText``, because
    ``update_chatbot_key`` distinguishes an absent field ("leave it") from a blank
    one ("clear the origin allow-list", or for the name, an error). Collapsing
    ``""`` to ``None`` here would make an emptied allow-list a silent no-op — and
    an emptied allow-list is a *security* change the user meant to make.
    """

    name: Optional[str] = Field(
        default=None, title="Agent name", max_length=MAX_NAME_LENGTH
    )
    allowed_origins: Optional[str] = Field(
        default=None, title="Allowed origins", max_length=MAX_ALLOWED_ORIGINS_LENGTH
    )


class ChatbotSettingsTabQuery(QueryRequest):
    """Which tab of the settings page to open on."""

    tab: str = Field(default="appearance", title="Tab")

    @field_validator("tab", mode="before")
    @classmethod
    def validate_tab(cls, v: object) -> object:
        """An unknown tab falls back to the first one rather than erroring — a
        stale bookmark should still open the page."""
        tab = (str(v) if v is not None else "").strip()
        return tab if tab in SETTINGS_TABS else "appearance"


# --------------------------------------------------------------------------
# AI & prompt tab
# --------------------------------------------------------------------------

class ChatbotAiSettingsRequest(FormRequest):
    """
    The AI & Prompt tab: the agent's name, its system prompt, the owner-defined
    variables substituted into that prompt, and which model answers.

    ``llm_api_key_id`` stays a plain string rather than a ``UUID``: the service
    takes the public uuid as text and treats ``""`` as "resolve the user's active
    keys as usual", and that three-way meaning (a key / any key / invalid) is
    already implemented there.
    """

    agent_name: RequiredText = Field(title="Agent name", max_length=MAX_NAME_LENGTH)
    system_prompt: str = Field(
        title="System prompt", min_length=1, max_length=MAX_PROMPT_LENGTH
    )
    variables_json: str = Field(
        default="", title="Prompt variables", max_length=MAX_VARIABLES_JSON_LENGTH
    )
    llm_mode: RequiredText = Field(title="Model choice")
    llm_api_key_id: str = Field(default="", title="AI API key")

    @field_validator("llm_mode")
    @classmethod
    def validate_llm_mode(cls, v: str) -> str:
        if v not in LLM_MODE_VALUES:
            raise ValueError("Model choice is not one of the available options")
        return v


class ChatbotFlowRequest(FormRequest):
    """
    Attach a conversation flow to this agent, or clear it with a blank selection.

    Replaces a hand-rolled ``uuid.UUID(raw) if raw else None`` in a try/except
    whose message this keeps verbatim, since it is already the right sentence.
    """

    flow_id: OptionalUUID = Field(default=None, title="Flow")

    @field_validator("flow_id", mode="before")
    @classmethod
    def readable_failure(cls, v: object) -> object:
        """
        Keep the module's existing wording for a bad flow selection.

        ``OptionalUUID``'s generic "is not a valid selection" is correct but less
        specific than the sentence this form already used.
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return None

        try:
            return uuid_pkg.UUID(str(v).strip())
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(
                "That flow selection was not valid. Please pick a flow from the list."
            ) from exc


class ChatbotDataAgentRequest(FormRequest):
    """
    Attach a data agent, or clear it with a blank selection.

    Unlike the datasource target this *is* editable after creation: swapping which
    agent answers is a normal change.
    """

    workspace_id: OptionalUUID = Field(default=None, title="Workspace")
    data_agent_id: OptionalUUID = Field(default=None, title="Data agent")


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

class ChatbotActionRequest(FormRequest):
    """
    One webhook action, as both the Actions library and a chatbot's quick-create
    post it.

    ``headers_json`` and ``parameters_json`` stay raw text: their inner shape
    (a header map, a list of typed parameters) is validated by
    ``chatbot_action_service``, which also builds the model-facing tool schema from
    them and so has to interpret them anyway.

    ``timeout_seconds`` was previously read as a string and coerced downstream; a
    non-numeric value produced a parse error rather than a sentence. Typed here, it
    is refused with a range the user can act on.
    """

    name: RequiredText = Field(title="Action name", max_length=MAX_NAME_LENGTH)
    description: OptionalText = Field(
        default=None, title="Description", max_length=MAX_DESCRIPTION_LENGTH
    )
    http_method: RequiredText = Field(title="HTTP method")
    url: RequiredText = Field(title="URL", max_length=MAX_URL_LENGTH)
    headers_json: str = Field(
        default="", title="Headers", max_length=MAX_ACTION_TEMPLATE_LENGTH
    )
    body_template: str = Field(
        default="", title="Body template", max_length=MAX_ACTION_TEMPLATE_LENGTH
    )
    parameters_json: str = Field(
        default="", title="Parameters", max_length=MAX_ACTION_TEMPLATE_LENGTH
    )
    timeout_seconds: int = Field(
        default=10,
        title="Timeout",
        ge=MIN_ACTION_TIMEOUT,
        le=MAX_ACTION_TIMEOUT,
    )

    @field_validator("http_method", mode="before")
    @classmethod
    def normalize_method(cls, v: object) -> object:
        """Uppercased before the membership check, so ``get`` is accepted as ``GET``."""
        return str(v).strip().upper() if isinstance(v, str) else v

    @field_validator("http_method")
    @classmethod
    def validate_method(cls, v: str) -> str:
        if v not in ACTION_HTTP_METHODS:
            allowed = ", ".join(ACTION_HTTP_METHODS)
            raise ValueError(f"HTTP method must be one of {allowed}")
        return v

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """
        An action calls out over HTTP(S). Rejecting other schemes here keeps a
        ``file://`` or ``gopher://`` URL from reaching the HTTP client at all.
        """
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def default_blank_timeout(cls, v: object) -> object:
        """A blank field means "use the default", not "zero seconds"."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return 10
        return v


class ChatbotActionAttachRequest(FormRequest):
    """Add an existing library action to this agent."""

    action_id: RequiredUUID = Field(title="Action")

    @field_validator("action_id", mode="before")
    @classmethod
    def readable_failure(cls, v: object) -> object:
        """Keep the tab's existing wording for an empty picker."""
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ValueError("Please select an action to add.")
        return v


# --------------------------------------------------------------------------
# Widget appearance tab
# --------------------------------------------------------------------------

class WidgetAppearanceRequest(FormRequest):
    """
    The widget's branding, copy and sizing.

    Every field is text, including the numeric ones, because that is what
    ``WidgetAppearanceInput`` takes and what
    ``chatbot_widget_settings_service`` validates — hex colors against a pattern,
    sizes against their pixel ranges, the font and button style against their
    allow-lists, each with a message written for this form. Re-declaring those
    twenty rules here would create a second place to change them, and the service's
    copy is the one already on screen.

    What this schema adds is bounds and completeness: the blob fields cannot be
    unbounded, and the route no longer builds a twenty-field dataclass by hand out
    of twenty ``form.get`` calls where a typo in one silently sent an empty string.
    """

    brand_color: str = Field(default="", title="Brand color", max_length=64)
    background_color: str = Field(default="", title="Background color", max_length=64)
    header_background_color: str = Field(
        default="", title="Header background color", max_length=64
    )
    header_font: str = Field(default="", title="Header font", max_length=MAX_NAME_LENGTH)
    welcome_text: str = Field(
        default="", title="Welcome text", max_length=MAX_WIDGET_TEXT_LENGTH
    )
    idle_text: str = Field(default="", title="Idle text", max_length=MAX_WIDGET_TEXT_LENGTH)
    closing_text: str = Field(
        default="", title="Closing text", max_length=MAX_WIDGET_TEXT_LENGTH
    )
    send_button_style: str = Field(default="", title="Send button style", max_length=64)
    send_button_text: str = Field(
        default="", title="Send button text", max_length=MAX_NAME_LENGTH
    )
    send_button_font_size: str = Field(default="", title="Send button font size", max_length=16)
    send_button_font_color: str = Field(
        default="", title="Send button font color", max_length=64
    )
    send_button_icon_svg: str = Field(
        default="", title="Send button icon SVG", max_length=MAX_SVG_LENGTH
    )
    send_button_border_radius: str = Field(
        default="", title="Send button border radius", max_length=16
    )
    input_border_radius: str = Field(default="", title="Input border radius", max_length=16)
    watermark_opacity: str = Field(default="", title="Watermark opacity", max_length=16)
    bot_message_bg_color: str = Field(
        default="", title="Bot message background color", max_length=64
    )
    bot_message_text_color: str = Field(
        default="", title="Bot message text color", max_length=64
    )
    user_message_text_color: str = Field(
        default="", title="User message text color", max_length=64
    )
    widget_width: str = Field(default="", title="Widget width", max_length=16)
    widget_height: str = Field(default="", title="Widget height", max_length=16)

    # The five images can each be replaced by an upload or cleared by a checkbox.
    # The uploads themselves are read with app.utils.file_utils.read_upload_field;
    # only the clear flags are form values, so only they are declared here.
    remove_logo: CheckboxBool = Field(default=False, title="Remove logo")
    remove_background_image: CheckboxBool = Field(
        default=False, title="Remove background image"
    )
    remove_bot_icon: CheckboxBool = Field(default=False, title="Remove bot icon")
    remove_send_button_icon: CheckboxBool = Field(
        default=False, title="Remove send button icon"
    )
    remove_watermark_image: CheckboxBool = Field(
        default=False, title="Remove watermark image"
    )

    #: The appearance fields, in the order ``WidgetAppearanceInput`` declares them.
    APPEARANCE_FIELDS: ClassVar[tuple[str, ...]] = (
        "brand_color",
        "background_color",
        "header_background_color",
        "header_font",
        "welcome_text",
        "idle_text",
        "closing_text",
        "send_button_style",
        "send_button_text",
        "send_button_font_size",
        "send_button_font_color",
        "send_button_icon_svg",
        "send_button_border_radius",
        "input_border_radius",
        "watermark_opacity",
        "bot_message_bg_color",
        "bot_message_text_color",
        "user_message_text_color",
        "widget_width",
        "widget_height",
    )

    #: The image slots, and the ``remove_*`` field that clears each.
    IMAGE_FIELDS: ClassVar[tuple[str, ...]] = (
        "logo",
        "background_image",
        "bot_icon",
        "send_button_icon",
        "watermark_image",
    )

    def appearance_values(self) -> dict:
        """The appearance fields as the service's input dataclass expects them."""
        return {name: getattr(self, name) for name in self.APPEARANCE_FIELDS}

    def removal_values(self) -> dict:
        """The clear-this-image flags, keyed by image slot."""
        return {name: getattr(self, f"remove_{name}") for name in self.IMAGE_FIELDS}


# --------------------------------------------------------------------------
# Public widget endpoint — the only unauthenticated payload in the application
# --------------------------------------------------------------------------

class PublicWidgetConfigQuery(QueryRequest):
    """
    The api_key a widget identifies itself with when fetching its appearance.

    Required rather than defaulted: the endpoint has nothing to answer without it,
    and a blank key previously produced the same 404 as a wrong one — which is
    correct for a wrong key and unhelpful for a misconfigured embed.
    """

    api_key: RequiredText = Field(title="API key", max_length=MAX_NAME_LENGTH)


class PublicChatbotMessageRequest(JsonRequest):
    """
    One turn from an embedded widget: the key, the visitor's message, the session
    it belongs to, and — when the last answer offered choices — which one was
    picked.

    The bounds here matter more than anywhere else in this layer. This body comes
    from an anonymous caller on someone else's website, and every accepted message
    is a model call that the owner pays for.

    The key and origin checks are *not* here: they need the database and the
    request's ``Origin`` header, and ``chatbot_service`` already owns both. What
    this schema guarantees is that a body reaching those checks is a JSON object
    with fields of sane types and sizes — where before, a body that parsed to a
    list turned ``(body or {}).get(...)`` into an ``AttributeError`` and a 500.
    """

    invalid_body_message = "Invalid request body"

    api_key: RequiredText = Field(title="API key", max_length=MAX_NAME_LENGTH)
    message: str = Field(default="", title="Message", max_length=MAX_MESSAGE_LENGTH)
    session_id: str = Field(
        default="", title="Session", max_length=MAX_SESSION_TOKEN_LENGTH
    )
    selected_value: Optional[str] = Field(
        default=None, title="Selected value", max_length=MAX_SELECTED_VALUE_LENGTH
    )


class PublicChatbotStreamQuery(QueryRequest):
    """
    The same turn as :class:`PublicChatbotMessageRequest`, off the query string.

    A separate schema because the source is genuinely different: ``EventSource`` can only
    issue a GET, so a streamed turn arrives as query parameters rather than as a JSON
    body. The bounds are the same ones — and they matter for the same reason, since this
    is still an anonymous caller on someone else's website and every accepted message is a
    model call the owner pays for.

    No ``selected_value``: a button or dropdown reply is a Flow Builder answer, and a flow
    turn does not stream (``chatbot_turn_service.stream_turn`` says why). Accepting the
    field here would advertise something this endpoint cannot honour.
    """

    api_key: str = Field(default="", title="API key", max_length=MAX_NAME_LENGTH)
    message: str = Field(default="", title="Message", max_length=MAX_MESSAGE_LENGTH)
    session_id: str = Field(
        default="", title="Session", max_length=MAX_SESSION_TOKEN_LENGTH
    )


class ChatbotTurnResponse(ResponseSchema):
    """
    One answered turn, as the widget reads it.

    Always sent with HTTP 200, including for an answering failure: the widget
    renders the payload either way, and a non-2xx here would be
    indistinguishable from the key and origin rejections.

    ``text`` duplicates ``summary``. The flow node types (menu / dropdown /
    ask-input) read their prompt from that field, and both are sent so neither
    reader needs to know which kind of turn it is.
    """

    status: str = Field(title="Status")
    type: str = Field(default="text", title="Type")
    summary: str = Field(default="", title="Summary")
    text: str = Field(default="", title="Text")
    insights: list = Field(default_factory=list, title="Insights")
    table: Optional[dict] = Field(default=None, title="Table")
    options: list = Field(default_factory=list, title="Options")
    message: Optional[str] = Field(default=None, title="Message")
    response_time_ms: int = Field(default=0, title="Response time (ms)")
    #: The export this turn started or reported on, already serialised by
    #: ``DownloadNoticeView`` — carried as a plain dict for the same reason ``table``
    #: is, so this schema need not import another feature's package. None on the
    #: overwhelming majority of turns, and the widget renders nothing when it is.
    download: Optional[dict] = Field(default=None, title="Download")

    @classmethod
    def from_turn(cls, result) -> "ChatbotTurnResponse":
        """
        Build from a ``chatbot_turn_service.TurnResult``.

        An error turn carries only the message and the timing — the answer fields
        are left at their defaults rather than sent as empty values that a widget
        might try to render.
        """
        if result.status == "error":
            return cls(
                status="error",
                message=result.message,
                response_time_ms=result.response_time_ms,
            )

        return cls(
            status="success",
            type=result.type,
            summary=result.summary,
            text=result.summary,
            insights=list(result.insights or []),
            table=result.table,
            options=list(result.options or []),
            response_time_ms=result.response_time_ms,
            download=result.download,
        )

    def payload(self) -> dict:
        """
        The turn as the widget's JSON.

        ``message`` is dropped from a success and the answer fields from an error,
        so each payload holds only the keys that mean something for that outcome —
        the same split the hand-built dicts had.
        """
        data = super().payload()

        if self.status == "error":
            return {
                "status": "error",
                "message": data["message"],
                "response_time_ms": data["response_time_ms"],
            }

        data.pop("message", None)
        return data


class WidgetConfigResponse(ResponseSchema):
    """
    A widget's appearance and copy, fetched by the widget script at runtime so a
    dashboard change applies on the next page load without a re-download.

    ``extra="allow"`` — the exception to this layer's rule — because
    ``build_widget_public_config`` owns the key set, defaults included, and it is
    read by the widget script rather than by any server-side code. Narrowing it
    here would silently drop a key the script needs the moment one is added, so the
    schema guarantees the envelope (``status`` plus a JSON object) and leaves the
    contents to the one place that defines them.
    """

    model_config = {"extra": "allow"}

    status: str = Field(default="success", title="Status")
    title: str = Field(default="", title="Title")

    @classmethod
    def from_config(cls, config: dict) -> "WidgetConfigResponse":
        return cls.build({"status": "success", **config})


class ChatbotKeyView(ResponseSchema):
    """
    One row of the agents table.

    ``api_key`` is present because it is *publishable* — it goes into the embed
    snippet on the customer's own site, and its protection is the per-key origin
    allow-list, not secrecy. That is the one deliberate exception to this layer's
    "no secrets in a view" rule, and it is why ``allowed_origins`` sits beside it.
    """

    uuid: str = Field(title="Agent")
    name: str = Field(title="Name")
    api_key: str = Field(default="", title="API key")
    target_type: str = Field(default="", title="Target type")
    allowed_origins: Optional[str] = Field(default=None, title="Allowed origins")
    is_active: bool = Field(default=True, title="Active")


class ChatbotActionView(ResponseSchema):
    """One row of the actions table, in the library and on an agent's Actions tab."""

    uuid: str = Field(title="Action")
    name: str = Field(title="Name")
    description: Optional[str] = Field(default=None, title="Description")
    http_method: str = Field(default="", title="HTTP method")
    url: str = Field(default="", title="URL")
    is_active: bool = Field(default=True, title="Active")
    parameter_count: int = Field(default=0, title="Parameters")
    attached_count: int = Field(default=0, title="Attached to")


# Re-exported so a template rendering the action form can offer exactly the values
# ChatbotActionRequest accepts — one source for the dropdown and the check.
ACTION_METHOD_CHOICES = ACTION_HTTP_METHODS
ACTION_PARAMETER_TYPE_CHOICES = ACTION_PARAMETER_TYPES

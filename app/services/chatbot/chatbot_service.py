"""
Business logic for embeddable chatbot widgets (Chatbot Settings module).

A chatbot API key is a *publishable* key: it ends up embedded in a
downloadable client-side JS file, so unlike the secret AI Settings provider
keys it is never encrypted at rest, and it is never treated as confidential.
The real security boundary is scope (one key -> one or more datasource
targets: the whole datasource, one or more tables/collections, or one or
more files) and an allow-list of embedding origins, enforced in
answer_message / validate_origin.

The actual AI-answering logic (load a real data sample -> compute a stats
profile -> ask the resolved provider) is NOT duplicated here — it's shared
with the authenticated "Ask AI" flow via
ai_analytics_service.run_grounded_prompt.
"""

import re
import uuid
from typing import List, Optional

from litestar.exceptions import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.chatbot.queries import get_or_create_ai_settings
from app.db.db_utils import CRUDQueryBuilder
from app.models.chatbot import (
    TARGET_TYPE_AGENT,
    TARGET_TYPES,
    ChatbotApiKey,
    ChatbotMessage,
)
from app.models.datasource import DatasourceFile
from app.services.ai_analytics.ai_analytics_service import datasource_crud, run_grounded_prompt
from app.services.data_agents import data_agent_service
from app.services.workspaces import workspace_service

chatbot_key_crud = CRUDQueryBuilder(ChatbotApiKey)

_MAX_MESSAGE_LEN = 2000
_VALID_TARGET_TYPES = frozenset(TARGET_TYPES)
_HISTORY_PAGE_SIZE = 50

# Full origin only — scheme + host + optional port, no path/trailing slash.
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$")


def _parse_allowed_origins(raw: str) -> List[str]:
    origins = [o.strip().rstrip("/") for o in re.split(r"[\n,]+", raw or "") if o.strip()]
    if not origins:
        raise HTTPException(status_code=400, detail="At least one allowed domain is required")
    for origin in origins:
        if not _ORIGIN_RE.match(origin):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid domain: {origin!r}. Use a full origin, e.g. https://example.com",
            )
    return origins


def validate_origin(chatbot_key: ChatbotApiKey, origin: Optional[str]) -> bool:
    if not origin:
        return False
    return origin.rstrip("/") in (chatbot_key.allowed_origins or [])


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------

async def get_user_chatbot_keys(db: AsyncSession, user_id: int) -> List[ChatbotApiKey]:
    return await chatbot_key_crud.get_many(
        db, filters={"user_id": user_id}, order_by="created_at", desc=True
    )


async def get_chatbot_key(db: AsyncSession, user_id: int, key_id: uuid.UUID) -> ChatbotApiKey:
    existing = await chatbot_key_crud.get_by_uuid(db, key_id, extra_filters={"user_id": user_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Chatbot key not found")
    return existing


async def get_active_key_by_value(db: AsyncSession, api_key: str) -> Optional[ChatbotApiKey]:
    """Public lookup — no user_id, since anonymous widget requests carry only the key itself."""
    return await chatbot_key_crud.get_one(db, filters={"api_key": api_key, "is_active": True})


async def get_conversation_history(
    db: AsyncSession,
    user_id: int,
    key_id: uuid.UUID,
) -> List[ChatbotMessage]:
    key = await get_chatbot_key(db, user_id, key_id)  # ownership check
    result = await db.execute(
        select(ChatbotMessage)
        .where(ChatbotMessage.chatbot_key_id == key.id)
        .order_by(ChatbotMessage.created_at.desc())
        .limit(_HISTORY_PAGE_SIZE)
    )
    return list(result.scalars().all())


# --------------------------------------------------------------------------
# Write — key management
# --------------------------------------------------------------------------

async def _resolve_file_targets(
    db: AsyncSession,
    datasource_id: int,
    file_ids: List[uuid.UUID],
) -> List[DatasourceFile]:
    """Validate every file_id belongs to an active file under this datasource
    and return the matching rows (used to derive display names server-side —
    never trust client-submitted file labels)."""
    file_ids = list(dict.fromkeys(file_ids))  # de-duplicate, preserve order
    if not file_ids:
        raise HTTPException(status_code=400, detail="At least one file is required")

    result = await db.execute(
        select(DatasourceFile).where(
            DatasourceFile.uuid.in_(file_ids),
            DatasourceFile.datasource_id == datasource_id,
            DatasourceFile.is_active == True,  # noqa: E712
        )
    )
    files = {f.uuid: f for f in result.scalars().all()}

    missing = [str(fid) for fid in file_ids if fid not in files]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"File(s) not found or inactive: {', '.join(missing)}",
        )

    return [files[fid] for fid in file_ids]


async def create_chatbot_key(
    db: AsyncSession,
    user_id: int,
    name: str,
    datasource_id: Optional[uuid.UUID],
    target_type: str,
    target_names: List[str],
    file_ids: List[uuid.UUID],
    allowed_origins_raw: str,
    workspace_id: Optional[uuid.UUID] = None,
    data_agent_id: Optional[uuid.UUID] = None,
) -> ChatbotApiKey:
    """
    Create a chatbot widget key.

    ``workspace_id`` / ``data_agent_id`` are the optional Deep Agent attachment,
    picked as a Workspace -> Data Agent cascade on the form. Both default to None,
    which is what every chatbot created before this feature has — and NULL means
    "answer from a data profile", the original behaviour.

    ``target_type == "agent"`` is the one case where ``datasource_id`` may be None:
    the attached agent's tool configs are the scope, so there is no datasource to
    nominate and no tables to tick. It requires an agent — that is the whole
    definition of the mode — and it is checked here rather than only in the form,
    because a chatbot in that mode with no agent could not answer anything.
    """
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    if target_type not in _VALID_TARGET_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid target_type: {target_type!r}")

    resolved_workspace_id, resolved_agent_id = await _resolved_agent_attachment(
        db, user_id, workspace_id, data_agent_id,
    )

    if target_type == TARGET_TYPE_AGENT:
        if resolved_agent_id is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Pick a data agent, or choose a data source for this widget to "
                    "answer from."
                ),
            )

        return await _create_key(db, {
            "user_id": user_id,
            "name": name,
            # No datasource target of its own: the agent's tools are the scope.
            "datasource_id": None,
            "target_type": target_type,
            "target_names": [],
            "file_ids": [],
            "allowed_origins": _parse_allowed_origins(allowed_origins_raw),
            "workspace_id": resolved_workspace_id,
            "data_agent_id": resolved_agent_id,
        })

    if datasource_id is None:
        raise HTTPException(status_code=400, detail="Please select a data source")

    datasource = await datasource_crud.get_by_uuid(db, datasource_id, extra_filters={"user_id": user_id})
    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    resolved_target_names: List[str] = []
    resolved_file_ids: List[int] = []

    if target_type == "file":
        files = await _resolve_file_targets(db, datasource.id, file_ids)
        resolved_target_names = [f.original_filename for f in files]
        resolved_file_ids = [f.id for f in files]
    elif target_type in ("table", "collection"):
        seen = []
        for raw_name in target_names:
            stripped = (raw_name or "").strip()
            if stripped and stripped not in seen:
                seen.append(stripped)
        if not seen:
            raise HTTPException(
                status_code=400,
                detail=f"At least one {target_type} is required",
            )
        resolved_target_names = seen
    # target_type == "datasource": no further target validation needed.

    allowed_origins = _parse_allowed_origins(allowed_origins_raw)

    return await _create_key(db, {
        "user_id": user_id,
        "name": name,
        "datasource_id": datasource.id,
        "target_type": target_type,
        "target_names": resolved_target_names,
        "file_ids": [str(fid) for fid in resolved_file_ids],
        "allowed_origins": allowed_origins,
        "workspace_id": resolved_workspace_id,
        "data_agent_id": resolved_agent_id,
    })


async def _create_key(db: AsyncSession, fields: dict) -> ChatbotApiKey:
    """
    Write the row and seed its persona.

    Shared by both creation paths so neither can forget the second half: a chatbot
    is never left without AI settings. This seeds its agent name, default system
    prompt and prompt variables straight away, so the very first visitor message is
    answered with a real prompt rather than a placeholder created later on first
    settings-page visit.
    """
    chatbot_key = await chatbot_key_crud.create(db, fields)

    await get_or_create_ai_settings(db, chatbot_key.id)

    return chatbot_key


async def update_chatbot_key(
    db: AsyncSession,
    user_id: int,
    key_id: uuid.UUID,
    name: Optional[str] = None,
    allowed_origins_raw: Optional[str] = None,
) -> ChatbotApiKey:
    """Edit a key's name and/or allowed origins. Datasource/target is immutable."""
    existing = await get_chatbot_key(db, user_id, key_id)

    data = {}

    if name is not None:
        name = name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name is required")
        data["name"] = name

    if allowed_origins_raw is not None:
        data["allowed_origins"] = _parse_allowed_origins(allowed_origins_raw)

    if not data:
        return existing

    return await chatbot_key_crud.update(db, existing.id, data)


async def set_chatbot_data_agent(
    db: AsyncSession,
    user_id: int,
    key_id: uuid.UUID,
    workspace_id: Optional[uuid.UUID] = None,
    data_agent_id: Optional[uuid.UUID] = None,
) -> ChatbotApiKey:
    """
    Attach, change or clear this chatbot's data agent.

    Editable after creation, unlike the datasource target: swapping which agent
    answers is a normal operational change (a new tool set, a replacement agent),
    whereas repointing a widget at different data would silently change what a
    published key can reach.

    Passing no agent clears the attachment, and the chatbot goes back to answering
    from a data profile — *if* it has a datasource target to profile. An
    agent-backed widget (``target_type == "agent"``) has none, so clearing its agent
    is refused rather than performed: it would leave a published key that answers
    nothing, with no way back through the form, since the datasource target is
    immutable after creation. Swapping one agent for another is still allowed, which
    is the operation that case actually needs.
    """
    chatbot_key = await get_chatbot_key(db, user_id, key_id)

    resolved_workspace_id, resolved_agent_id = await _resolved_agent_attachment(
        db, user_id, workspace_id, data_agent_id,
    )

    if resolved_agent_id is None and chatbot_key.target_type == TARGET_TYPE_AGENT:
        raise HTTPException(
            status_code=400,
            detail=(
                "This widget has no data source of its own — its agent is what it "
                "reads. Choose a different agent instead of removing this one, or "
                "delete the widget."
            ),
        )

    return await chatbot_key_crud.update(db, chatbot_key.id, {
        "workspace_id": resolved_workspace_id,
        "data_agent_id": resolved_agent_id,
    })


async def _resolved_agent_attachment(
    db: AsyncSession,
    user_id: int,
    workspace_id: Optional[uuid.UUID],
    data_agent_id: Optional[uuid.UUID],
) -> tuple[Optional[int], Optional[int]]:
    """
    Turn the submitted workspace and agent uuids into internal FK values.

    Both lookups go through their own service, which is what scopes them to this
    user — otherwise pasting another user's agent uuid into the form would attach
    their agent, and with it their datasource credentials, to this chatbot. That is
    the whole reason this is not a straight assignment from the form.

    The workspace is only remembered so the picker can re-open on the right branch;
    it is deliberately not required to match the agent's own workspace. An agent may
    have no workspace at all, and one that is moved later must not silently detach
    itself from every chatbot using it.
    """
    if data_agent_id is None:
        # No agent means no attachment at all — keeping a workspace here would leave
        # the picker showing a branch with nothing selected in it.
        return None, None

    agent = await data_agent_service.get_data_agent(db, user_id, data_agent_id)

    resolved_workspace_id = None
    if workspace_id is not None:
        workspace = await workspace_service.get_workspace(db, user_id, workspace_id)
        resolved_workspace_id = workspace.id

    return resolved_workspace_id, agent.id


async def toggle_active_status(db: AsyncSession, user_id: int, key_id: uuid.UUID) -> ChatbotApiKey:
    existing = await get_chatbot_key(db, user_id, key_id)
    return await chatbot_key_crud.update(db, existing.id, {"is_active": not existing.is_active})


async def delete_chatbot_key(db: AsyncSession, user_id: int, key_id: uuid.UUID) -> None:
    existing = await get_chatbot_key(db, user_id, key_id)  # ownership check
    await chatbot_key_crud.delete(db, existing.id)


# --------------------------------------------------------------------------
# Public entry point — answering a visitor's message
# --------------------------------------------------------------------------

async def answer_message(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    message: str,
    extra_instructions: str = "",
    forced_key_uuid: Optional[uuid.UUID] = None,
    use_inbuilt_llm: bool = False,
    system_prompt_override: str = "",
    action_context: str = "",
):
    """
    Answer a widget visitor's message. Returns an AnalyticsResult.

    Persisting the exchange is deliberately *not* done here: one visitor turn
    can reach this function more than once (a Flow Builder AI Fallback node
    answering inside a flow), so the conversation/performance log is written
    once per turn by chatbot_turn_service instead.

    Every optional argument is a passthrough to run_grounded_prompt, defaulting
    to "unset" so no caller is forced to know about features it doesn't use:

    * `extra_instructions`, `forced_key_uuid`, `use_inbuilt_llm` — the Flow
      Builder AI Fallback node's guardrails/prompt and LLM choice.
    * `system_prompt_override` — the chatbot's own configured system prompt
      (see chatbot_ai_settings_service.render_system_prompt).
    * `action_context` — the response from a webhook action that already ran
      for this turn (the action itself is logged by chatbot_action_service).
    """

    message = (message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")
    if len(message) > _MAX_MESSAGE_LEN:
        raise HTTPException(status_code=400, detail=f"Message must not exceed {_MAX_MESSAGE_LEN} characters")

    if chatbot_key.datasource_id is None:
        # An agent-backed widget has no datasource target to profile. Reaching here
        # means its agent could not run and chatbot_reply_service tried to fall back
        # — there is nothing to fall back *to*, and saying so is the honest answer.
        # Guarded before the lookup because filtering on id=None would otherwise
        # produce the "no longer available" message, which is wrong: it never had
        # one.
        raise HTTPException(
            status_code=409,
            detail=(
                "This chatbot answers through its data agent and has no data source "
                "of its own to fall back on."
            ),
        )

    datasource = await datasource_crud.get_one(
        db, filters={"id": chatbot_key.datasource_id, "user_id": chatbot_key.user_id}
    )
    if not datasource:
        raise HTTPException(status_code=404, detail="This chatbot's data source is no longer available")

    return await run_grounded_prompt(
        db,
        chatbot_key.user_id,
        datasource,
        chatbot_key.target_type,
        target_names=chatbot_key.target_names,
        file_ids=[int(fid) for fid in chatbot_key.file_ids],
        prompt=message,
        extra_instructions=extra_instructions,
        forced_key_uuid=forced_key_uuid,
        use_inbuilt_llm=use_inbuilt_llm,
        system_prompt_override=system_prompt_override,
        action_context=action_context,
    )


# --------------------------------------------------------------------------
# Embeddable widget file generation
# --------------------------------------------------------------------------

# Self-contained IIFE: injects its own <style> + DOM, no external dependencies.
#
# This file is 100% generic — identical for every chatbot key, nothing is
# templated into it. The embedding page must set
# ``window.GMSChatbotConfig = { apiBase, apiKey }`` in an inline <script>
# BEFORE this file's <script> tag (see the embed snippet on the Widget
# Settings page); that's the only thing the embedder needs to configure by
# hand. ``apiBase`` may be omitted when the API answers on the embedding
# page's own origin, which makes every request same-origin and immune to the
# scheme mismatch described in ``blockedRequestHint`` below. Every appearance
# setting (colors, fonts, logo/icon URLs, welcome/idle/closing text) and every
# API-key/origin validity check is resolved server-side, fetched at runtime
# from GET /public/chatbot/widget-config — so changing settings in the
# dashboard takes effect on the next page load, and this downloaded file never
# goes stale or needs replacing after a settings change.
#
# **Every network failure in here is reported to the console.** The widget is
# built to degrade rather than break — a failed config fetch still renders, a
# failed message still answers the visitor politely — and that is right for a
# visitor and actively misleading for the operator, because a widget showing
# default branding is indistinguishable from a working one. So each fallback
# path calls ``warnFailure`` with the URL it tried and what came back. Three
# separate causes (an origin not on the allow-list, an HTTPS page pointed at an
# http:// apiBase, an unreachable API) otherwise present as the identical
# symptom: a healthy-looking widget titled "Chat with us".
_WIDGET_SCRIPT_TEMPLATE = r"""
(function () {
  "use strict";

  var CFG = window.GMSChatbotConfig || {};
  var API_KEY = CFG.apiKey;
  var IDLE_TIMEOUT_MS = 45000;

  // apiBase is OPTIONAL. Omit it when the API answers on the same origin as the
  // embedding page (a shared domain, or a reverse proxy in front of both) and every
  // request becomes a same-origin relative one — which cannot be blocked by a
  // scheme mismatch, and needs no CORS at all.
  //
  // A trailing slash is stripped rather than rejected: every request appends a path
  // beginning with "/", so "https://api.example.com/" would otherwise produce a
  // double slash and a 404 that looks nothing like a configuration mistake.
  var API_BASE = String(CFG.apiBase == null ? "" : CFG.apiBase).trim().replace(/\/+$/, "");

  /**
   * A URL the server gave us, made fetchable from this page.
   *
   * The download URLs arrive absolute when SITE_URL is set on the server, because a
   * relative one would be resolved against the embedding page and ask the operator's
   * own site for a file it has never heard of. Prefixing an already-absolute URL with
   * API_BASE would produce "https://api.example.com/https://api.example.com/..." — so
   * anything already carrying a scheme is passed through untouched, and only a bare
   * path gets the prefix.
   */
  function apiUrl(url) {
    var value = String(url == null ? "" : url);

    if (/^https?:\/\//i.test(value)) return value;

    return API_BASE + value;
  }

  if (!API_KEY) {
    console.error(
      "GetMyStuff chatbot widget: set window.GMSChatbotConfig = { apiKey } before " +
      "loading this script (apiBase too, unless the API is on this same origin)."
    );
    return;
  }

  /**
   * The reason a request to API_BASE is likely to be blocked by the browser before
   * it is ever sent, or "" when there is no such reason.
   *
   * This one case is worth naming explicitly because it is invisible everywhere
   * else: an HTTPS page requesting a plain-HTTP address is stopped client-side, so
   * the server logs nothing at all, and the browser's own Network panel reports it
   * as a "CORS error" with no status and no response body — which points at a
   * server misconfiguration that does not exist.
   */
  function blockedRequestHint() {
    if (!API_BASE) return "";
    if (window.location.protocol !== "https:") return "";
    if (API_BASE.indexOf("http://") !== 0) return "";

    return (
      "This page is served over HTTPS but apiBase is \"" + API_BASE + "\", which is " +
      "plain HTTP. Browsers block that combination before the request leaves the " +
      "page and report it as a CORS error even though the server never saw it. Use " +
      "an HTTPS apiBase, or omit apiBase entirely if the API answers on this origin."
    );
  }

  /**
   * Tell the operator, in their console, exactly which request failed and why.
   *
   * Every failure below is otherwise silent by design — the widget falls back to
   * its default look, or shows the visitor a neutral message — and silence is what
   * makes a misconfigured widget look like a working one. The visitor still sees
   * nothing technical; this is for whoever installed the snippet.
   */
  function warnFailure(what, url, detail) {
    var hint = blockedRequestHint();

    console.warn(
      "GetMyStuff chatbot widget: " + what +
      "\n  request: " + url +
      (detail ? "\n  reason:  " + detail : "") +
      // Its own labelled line, not appended to the reason — "Failed to fetch This
      // page is served over HTTPS" reads as one broken sentence and buries the
      // part that actually tells the operator what to change.
      (hint ? "\n  likely cause: " + hint : "")
    );
  }

  var DEFAULT_CONFIG = {
    title: "Chat with us",
    brand_color: "#2563eb",
    header_background_color: "#2563eb",
    header_font: "system-ui",
    background_color: "#f8f9fa",
    background_image_url: "",
    watermark_image_url: "",
    watermark_opacity: 15,
    logo_url: "",
    bot_icon_url: "",
    bot_message_bg_color: "#e9ecef",
    bot_message_text_color: "#212529",
    user_message_text_color: "#ffffff",
    welcome_text: "Hi! How can I help you today?",
    idle_text: "",
    closing_text: "",
    send_button_style: "text-only",
    send_button_text: "Send",
    send_button_font_size: 13,
    send_button_font_color: "#ffffff",
    send_button_icon_url: "",
    send_button_border_radius: 6,
    input_border_radius: 6,
    widget_width: 340,
    widget_height: 460
  };

  // Falling back to DEFAULT_CONFIG is right — a widget that renders with default
  // branding still works, and refusing to render at all would be worse for a
  // visitor. But the fallback is indistinguishable from a correctly configured
  // widget whose settings happen to be the defaults, so every path out of here
  // that is *not* the settings the dashboard holds says so in the console.
  function fetchConfig() {
    var url = API_BASE + "/public/chatbot/widget-config?api_key=" + encodeURIComponent(API_KEY);

    return fetch(url)
      .then(function (r) {
        // A rejection body is JSON too ({"status","message"}), but an error page
        // from a proxy in front of the API is not — so a parse failure is handled
        // rather than becoming an unexplained throw.
        return r.json().then(
          function (data) { return { status: r.status, data: data }; },
          function () { return { status: r.status, data: null }; }
        );
      })
      .then(function (res) {
        if (res.data && res.data.status === "success") return res.data;

        warnFailure(
          "could not load its settings, so it is showing the default appearance " +
          "and welcome message rather than the ones configured in the dashboard.",
          url,
          "HTTP " + res.status + ((res.data && res.data.message) ? " — " + res.data.message : "")
        );
        return DEFAULT_CONFIG;
      })
      .catch(function (err) {
        warnFailure(
          "could not reach the API at all, so it is showing the default appearance " +
          "and welcome message. Sending a message will fail for the same reason.",
          url,
          (err && err.message) || "the request did not complete"
        );
        return DEFAULT_CONFIG;
      });
  }

  function buildStyle(cfg) {
    var messagesBg = cfg.background_image_url
      ? "background-image:url(" + JSON.stringify(cfg.background_image_url) + ");background-size:cover;background-position:center;"
      : "background:" + cfg.background_color + ";";

    return (
      ".gms-chatbot-launcher{position:fixed;bottom:20px;right:20px;width:56px;height:56px;" +
      "border-radius:50%;background:" + cfg.brand_color + ";color:#fff;border:none;box-shadow:0 4px 14px rgba(0,0,0,.25);" +
      "cursor:pointer;font-size:24px;z-index:999999;display:flex;align-items:center;justify-content:center;" +
      "overflow:hidden;padding:0;}" +
      ".gms-chatbot-launcher img{width:100%;height:100%;object-fit:cover;}" +
      ".gms-chatbot-panel{position:fixed;bottom:88px;right:20px;width:" + cfg.widget_width + "px;max-width:calc(100vw - 40px);" +
      "height:" + cfg.widget_height + "px;max-height:calc(100vh - 120px);background:#fff;border-radius:12px;" +
      "box-shadow:0 8px 30px rgba(0,0,0,.2);display:none;flex-direction:column;overflow:hidden;" +
      "z-index:999999;font-family:'" + cfg.header_font + "',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;}" +
      ".gms-chatbot-panel.gms-open{display:flex;}" +
      ".gms-chatbot-header{background:" + cfg.header_background_color + ";color:#fff;padding:12px 14px;font-weight:600;" +
      "display:flex;justify-content:space-between;align-items:center;font-size:14px;gap:8px;}" +
      ".gms-chatbot-header-title{display:flex;align-items:center;gap:8px;overflow:hidden;}" +
      ".gms-chatbot-header-logo{width:22px;height:22px;border-radius:50%;object-fit:cover;flex:none;}" +
      ".gms-chatbot-header-actions{display:flex;align-items:center;gap:10px;flex:none;}" +
      ".gms-chatbot-restart{background:none;border:none;color:#fff;font-size:16px;cursor:pointer;line-height:1;opacity:.9;}" +
      ".gms-chatbot-close{background:none;border:none;color:#fff;font-size:18px;cursor:pointer;line-height:1;}" +
      ".gms-chatbot-messages{position:relative;flex:1;overflow-y:auto;padding:12px;" + messagesBg + "}" +
      ".gms-chatbot-watermark{position:absolute;inset:0;background-image:url(" + JSON.stringify(cfg.watermark_image_url) + ");" +
      "background-size:contain;background-position:center;background-repeat:no-repeat;" +
      "opacity:" + (cfg.watermark_opacity / 100) + ";pointer-events:none;z-index:0;}" +
      // align-items:flex-start keeps the bot avatar level with the FIRST line of
      // its bubble; flex-end would sink it to the bottom of a tall reply (a
      // table or a bullet list), reading as if it belonged to the next message.
      ".gms-chatbot-msg-row{position:relative;z-index:1;display:flex;align-items:flex-start;gap:6px;max-width:100%;}" +
      ".gms-chatbot-msg-avatar{width:22px;height:22px;border-radius:50%;object-fit:cover;flex:none;}" +
      ".gms-chatbot-msg{position:relative;z-index:1;margin-bottom:10px;font-size:13px;line-height:1.4;max-width:85%;" +
      "padding:8px 10px;border-radius:8px;white-space:pre-wrap;}" +
      ".gms-chatbot-msg-row .gms-chatbot-msg{max-width:calc(85% - 28px);}" +
      // The visitor's own messages sit on the right, the bot's on the left, so
      // who said what is readable at a glance. Every bubble lives inside a
      // .gms-chatbot-msg-row flex container for this reason: a bare block div
      // stretches to the full panel width and ignores auto margins entirely.
      ".gms-chatbot-msg-row-user{justify-content:flex-end;}" +
      ".gms-chatbot-msg-row-user .gms-chatbot-msg{max-width:85%;}" +
      ".gms-chatbot-msg-user{background:" + cfg.brand_color + ";color:" + cfg.user_message_text_color + ";" +
      "border-bottom-right-radius:2px;}" +
      ".gms-chatbot-msg-bot{background:" + cfg.bot_message_bg_color + ";color:" + cfg.bot_message_text_color + ";" +
      "margin-right:auto;border-bottom-left-radius:2px;}" +
      ".gms-chatbot-msg-error{background:#f8d7da;color:#842029;margin-right:auto;}" +
      ".gms-chatbot-msg-idle{background:#fff3cd;color:#664d03;margin-right:auto;font-style:italic;}" +
      ".gms-chatbot-msg ul{margin:6px 0 0;padding-left:18px;}" +
      // Menu/Dropdown choices stack vertically — a horizontal row overflowed
      // the panel and forced a sideways scrollbar as soon as a flow had more
      // than two options. Indented to line up with the bot bubble it follows.
      ".gms-chatbot-options{position:relative;z-index:1;display:flex;flex-direction:column;" +
      "align-items:stretch;gap:6px;margin:0 0 10px;max-width:85%;}" +
      ".gms-chatbot-options-indent{margin-left:28px;}" +
      ".gms-chatbot-option{display:block;width:100%;text-align:left;background:#fff;" +
      "color:" + cfg.brand_color + ";border:1px solid " + cfg.brand_color + ";" +
      "border-radius:8px;padding:8px 12px;font-size:13px;font-family:inherit;" +
      "line-height:1.3;cursor:pointer;}" +
      ".gms-chatbot-option:hover{background:" + cfg.brand_color + ";color:#fff;}" +
      // The download button a Download File block offers. Its own block under the reply,
      // like the options wrap above it, because it is not part of any one message: the
      // sentence that mentions it is the operator's Send Message block, which may not even
      // be next to it. The background colour is set per button from the node's setting, so
      // it is deliberately absent here.
      ".gms-chatbot-file{position:relative;z-index:1;display:flex;flex-direction:column;" +
      "align-items:flex-start;margin:0 0 10px;max-width:85%;}" +
      ".gms-chatbot-file-indent{margin-left:28px;}" +
      ".gms-chatbot-file-btn{display:inline-flex;align-items:center;gap:8px;" +
      "text-decoration:none;color:#fff;border:0;border-radius:8px;padding:9px 14px;" +
      "font-size:13px;font-family:inherit;line-height:1.3;cursor:pointer;}" +
      ".gms-chatbot-file-btn:hover{filter:brightness(.92);}" +
      ".gms-chatbot-file-meta{margin-top:4px;font-size:11px;color:#6c757d;}" +
      ".gms-chatbot-table{border-collapse:collapse;margin-top:6px;font-size:11px;}" +
      ".gms-chatbot-table th,.gms-chatbot-table td{border:1px solid #ced4da;padding:3px 6px;" +
      "text-align:left;white-space:nowrap;}" +
      ".gms-chatbot-table th{background:rgba(0,0,0,.04);font-weight:600;}" +
      // The panel is ~340px and a wide result is not. The wrapper scrolls so the
      // table never forces the whole chat window wider than the page allows.
      ".gms-chatbot-table-wrap{overflow-x:auto;max-width:100%;}" +
      ".gms-chatbot-msg p{margin:0 0 6px;}" +
      ".gms-chatbot-msg p:last-child{margin-bottom:0;}" +
      ".gms-chatbot-msg ul,.gms-chatbot-msg ol{margin:4px 0;padding-left:18px;}" +
      ".gms-chatbot-msg li{margin:2px 0;}" +
      ".gms-chatbot-msg code{background:rgba(0,0,0,.07);border-radius:3px;" +
      "padding:1px 4px;font-size:11px;font-family:ui-monospace,Menlo,Consolas,monospace;}" +
      ".gms-chatbot-md-h{font-weight:600;margin:6px 0 2px;}" +
      ".gms-chatbot-input-row{display:flex;border-top:1px solid #dee2e6;padding:8px;gap:6px;background:#fff;}" +
      ".gms-chatbot-input{flex:1;border:1px solid #ced4da;border-radius:" + cfg.input_border_radius + "px;padding:8px;" +
      "font-size:13px;resize:none;font-family:inherit;}" +
      ".gms-chatbot-send{background:" + cfg.brand_color + ";border:none;border-radius:" + cfg.send_button_border_radius + "px;padding:0 14px;" +
      "cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px;min-width:40px;}" +
      ".gms-chatbot-send:disabled{opacity:.5;cursor:default;}" +
      ".gms-chatbot-send-icon{width:16px;height:16px;object-fit:contain;}" +
      ".gms-chatbot-typing{font-size:12px;color:#6c757d;padding:0 12px 8px;}" +
      // Sits under the bubble it belongs to, indented past the avatar so it
      // lines up with the reply text rather than the icon.
      ".gms-chatbot-meta{position:relative;z-index:1;font-size:10px;color:#6c757d;" +
      "margin:-6px 0 10px;display:flex;align-items:center;gap:4px;}" +
      // The download card. Its own block under the bubble that announced it rather
      // than inside that bubble: it outlives the message, updating while the visitor
      // carries on asking other things, and a bubble is a record of something said.
      ".gms-chatbot-file{position:relative;z-index:1;max-width:85%;margin:0 0 10px;" +
      "border:1px solid #dee2e6;border-radius:10px;padding:10px 12px;background:#fff;}" +
      ".gms-chatbot-file-indent{margin-left:28px;}" +
      ".gms-chatbot-file-name{display:flex;align-items:center;gap:6px;font-size:12px;" +
      "font-weight:600;color:#212529;word-break:break-all;}" +
      ".gms-chatbot-file-sub{font-size:11px;color:#6c757d;margin-top:3px;}" +
      // The progress bar is a real fraction of records written, never a fake crawl.
      ".gms-chatbot-file-bar{height:4px;border-radius:2px;background:#e9ecef;" +
      "margin-top:8px;overflow:hidden;}" +
      ".gms-chatbot-file-fill{height:100%;width:0;border-radius:2px;background:" +
      cfg.brand_color + ";transition:width .4s ease;}" +
      // The shimmer that says "still working". A moving highlight over the text, which
      // reads as activity at a glance in a way a static line does not — and costs one
      // CSS animation rather than a timer redrawing the DOM.
      ".gms-chatbot-file-working{background:linear-gradient(90deg," +
      "#adb5bd 25%,#212529 50%,#adb5bd 75%);background-size:200% 100%;" +
      "-webkit-background-clip:text;background-clip:text;color:transparent;" +
      "animation:gms-chatbot-shimmer 2s linear infinite;}" +
      "@keyframes gms-chatbot-shimmer{0%{background-position:200% 0;}" +
      "100%{background-position:-200% 0;}}" +
      // Reduced-motion is honoured: the shimmer is decoration, and the words still
      // change, so nothing is lost by holding it still.
      "@media (prefers-reduced-motion:reduce){.gms-chatbot-file-working{" +
      "animation:none;background:none;-webkit-background-clip:border-box;" +
      "background-clip:border-box;color:#495057;}}" +
      ".gms-chatbot-file-btn{display:inline-flex;align-items:center;gap:6px;" +
      "margin-top:8px;background:" + cfg.brand_color + ";color:#fff;border:none;" +
      "border-radius:8px;padding:8px 14px;font-size:13px;font-family:inherit;" +
      "font-weight:600;line-height:1.3;cursor:pointer;text-decoration:none;}" +
      ".gms-chatbot-file-btn:hover{opacity:.9;color:#fff;text-decoration:none;}" +
      ".gms-chatbot-file-failed{color:#842029;font-size:11px;margin-top:4px;}"
    );
  }

  function injectStyle(cfg) {
    var styleEl = document.createElement("style");
    styleEl.textContent = buildStyle(cfg);
    document.head.appendChild(styleEl);
  }

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.textContent = str;
    // textContent -> innerHTML escapes & < >, which is everything that matters while
    // the result only ever lands between tags — which is the case for every caller
    // here, because nothing in this widget builds an attribute out of message text.
    // The quotes are escaped anyway: it costs one pass, and it means the day someone
    // does write `title="' + escapeHtml(x) + '"` they get a working escape instead of
    // an attribute break. Defence against a future edit, not against today's code.
    return div.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /**
   * Render an assistant message written in Markdown.
   *
   * THE SAFETY RULE, AND IT IS THE WHOLE DESIGN: the text is escaped FIRST, before
   * a single markdown pattern is looked at. After that line there is no `<` or `>`
   * left in the string — a model that emitted `<script>` is holding `&lt;script&gt;`
   * and will still be holding it when this returns. Every tag in the output was
   * written by the code below, from a fixed set, with no attribute ever built from
   * message text.
   *
   * That ordering is what makes this safe to put in innerHTML on a public widget
   * embedded in somebody else's website. Reversing it — parsing first and escaping
   * after, or "sanitising" a model's raw HTML — is the version of this that is an
   * XSS hole, and no allowlist bolted on afterwards recovers from it.
   *
   * **Links and images are deliberately not supported.** `[text](javascript:…)` is
   * the classic way markdown becomes script execution, and grounding rule 10 already
   * forbids the model writing a URL at all. Unsupported syntax is left as the literal
   * text the model wrote, which is honest and inert.
   *
   * Supported: tables, headings, bullet and numbered lists, **bold**, *italic* and
   * `code`. Tables are the reason this exists — a query result is a table, and the
   * escaped-text renderer this replaces showed one as a wall of `|` characters.
   */
  function renderMarkdown(text) {
    var lines = escapeHtml(String(text == null ? "" : text)).split(/\r?\n/);
    var out = [];
    var index = 0;

    while (index < lines.length) {
      if (!lines[index].trim()) { index += 1; continue; }

      if (startsTable(lines, index)) {
        var table = renderTableBlock(lines, index);
        out.push(table.html);
        index = table.next;
        continue;
      }

      if (isListItem(lines[index])) {
        var list = renderListBlock(lines, index);
        out.push(list.html);
        index = list.next;
        continue;
      }

      var heading = lines[index].match(/^\s*#{1,6}\s+(.*)$/);
      if (heading) {
        out.push('<div class="gms-chatbot-md-h">' + inlineMarkdown(heading[1]) + "</div>");
        index += 1;
        continue;
      }

      // A paragraph runs until a blank line or the start of any block above. The
      // first line is taken unconditionally: it has already been tested against
      // every block form and is none of them.
      var paragraph = [inlineMarkdown(lines[index].trim())];
      index += 1;

      while (index < lines.length && lines[index].trim() && !startsBlock(lines, index)) {
        paragraph.push(inlineMarkdown(lines[index].trim()));
        index += 1;
      }

      out.push("<p>" + paragraph.join("<br>") + "</p>");
    }

    return out.join("");
  }

  function isListItem(line) {
    return /^\s*(?:[-*+]|\d+\.)\s+/.test(line);
  }

  function isTableRow(line) {
    return /^\s*\|.*\|\s*$/.test(line);
  }

  // The `|---|---|` line under the header. Requiring it is what stops a sentence
  // that happens to contain pipes being read as a table.
  function isTableDivider(line) {
    return /^\s*\|[\s:|-]+\|\s*$/.test(line) && line.indexOf("-") !== -1;
  }

  function startsTable(lines, index) {
    return isTableRow(lines[index]) &&
      index + 1 < lines.length &&
      isTableDivider(lines[index + 1]);
  }

  function startsBlock(lines, index) {
    return isListItem(lines[index]) ||
      /^\s*#{1,6}\s+/.test(lines[index]) ||
      startsTable(lines, index);
  }

  function tableCells(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|")
      .map(function (cell) { return cell.trim(); });
  }

  function renderTableBlock(lines, start) {
    var header = tableCells(lines[start]);
    var index = start + 2;
    var body = [];

    while (index < lines.length && isTableRow(lines[index])) {
      body.push(tableCells(lines[index]));
      index += 1;
    }

    // Same class as the structured-result table, so one stylesheet describes both.
    // Wrapped because a widget is around 340px wide and a six-column result is not:
    // the wrapper scrolls, rather than the table forcing the whole panel wider.
    var html = '<div class="gms-chatbot-table-wrap"><table class="gms-chatbot-table"><thead><tr>' +
      header.map(function (cell) { return "<th>" + inlineMarkdown(cell) + "</th>"; }).join("") +
      "</tr></thead><tbody>" +
      body.map(function (row) {
        return "<tr>" + row.map(function (cell) {
          return "<td>" + inlineMarkdown(cell) + "</td>";
        }).join("") + "</tr>";
      }).join("") +
      "</tbody></table></div>";

    return { html: html, next: index };
  }

  function renderListBlock(lines, start) {
    var tag = /^\s*\d+\.\s+/.test(lines[start]) ? "ol" : "ul";
    var items = [];
    var index = start;

    while (index < lines.length && isListItem(lines[index])) {
      items.push(inlineMarkdown(lines[index].replace(/^\s*(?:[-*+]|\d+\.)\s+/, "")));
      index += 1;
    }

    return {
      html: "<" + tag + ">" +
        items.map(function (item) { return "<li>" + item + "</li>"; }).join("") +
        "</" + tag + ">",
      next: index
    };
  }

  /**
   * Inline emphasis, on text that is ALREADY escaped.
   *
   * Never call this on raw message text. Every caller above passes a slice of the
   * string escapeHtml produced at the top of renderMarkdown, and the one caller
   * outside it (insights) escapes first for the same reason.
   */
  function inlineMarkdown(text) {
    return text
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
      .replace(/(^|[^\w_])_([^_\s][^_]*)_/g, "$1<em>$2</em>");
  }

  function buildDom(cfg) {
    var launcher = document.createElement("button");
    launcher.className = "gms-chatbot-launcher";
    launcher.setAttribute("aria-label", "Open chat");
    if (cfg.bot_icon_url) {
      var iconImg = document.createElement("img");
      iconImg.src = cfg.bot_icon_url;
      iconImg.alt = "";
      launcher.appendChild(iconImg);
    } else {
      launcher.textContent = String.fromCodePoint(0x1F4AC);
    }

    var panel = document.createElement("div");
    panel.className = "gms-chatbot-panel";
    panel.innerHTML =
      '<div class="gms-chatbot-header"><span class="gms-chatbot-header-title"></span>' +
      '<div class="gms-chatbot-header-actions">' +
      '<button class="gms-chatbot-restart" aria-label="Restart conversation" title="Restart conversation">&#8635;</button>' +
      '<button class="gms-chatbot-close" aria-label="Close chat">×</button>' +
      "</div></div>" +
      '<div class="gms-chatbot-messages"></div>' +
      '<div class="gms-chatbot-typing" style="display:none;">Thinking…</div>' +
      '<div class="gms-chatbot-input-row">' +
      '<textarea class="gms-chatbot-input" rows="1" placeholder="Ask a question…"></textarea>' +
      '<button class="gms-chatbot-send">Send</button></div>';

    var titleEl = panel.querySelector(".gms-chatbot-header-title");
    if (cfg.logo_url) {
      var logoImg = document.createElement("img");
      logoImg.className = "gms-chatbot-header-logo";
      logoImg.src = cfg.logo_url;
      logoImg.alt = "";
      titleEl.appendChild(logoImg);
    }
    var titleText = document.createElement("span");
    titleText.textContent = cfg.title;
    titleEl.appendChild(titleText);

    if (cfg.watermark_image_url) {
      var watermark = document.createElement("div");
      watermark.className = "gms-chatbot-watermark";
      panel.querySelector(".gms-chatbot-messages").appendChild(watermark);
    }

    renderSendButton(panel.querySelector(".gms-chatbot-send"), cfg);

    document.body.appendChild(launcher);
    document.body.appendChild(panel);
    return { launcher: launcher, panel: panel };
  }

  function renderSendButton(btn, cfg) {
    var style = cfg.send_button_style || "text-only";
    var hasIcon = !!cfg.send_button_icon_url;
    var wantIcon = style === "icon-only" || style === "icon-with-text";
    // Falls back to text if an icon-only style has no icon configured yet,
    // so the button is never left empty/unusable.
    var wantText = style === "text-only" || style === "icon-with-text" || (wantIcon && !hasIcon);

    btn.innerHTML = "";
    btn.style.color = cfg.send_button_font_color || "#ffffff";
    btn.style.fontSize = (cfg.send_button_font_size || 13) + "px";

    if (wantIcon && hasIcon) {
      var img = document.createElement("img");
      img.className = "gms-chatbot-send-icon";
      img.src = cfg.send_button_icon_url;
      img.alt = "";
      btn.appendChild(img);
    }
    if (wantText) {
      var span = document.createElement("span");
      span.textContent = cfg.send_button_text || "Send";
      btn.appendChild(span);
    }
  }

  function renderUserMessage(container, text) {
    var row = document.createElement("div");
    row.className = "gms-chatbot-msg-row gms-chatbot-msg-row-user";

    var bubble = document.createElement("div");
    bubble.className = "gms-chatbot-msg gms-chatbot-msg-user";
    bubble.textContent = text;

    row.appendChild(bubble);
    container.appendChild(row);
  }

  // Wraps a bot-side bubble (AI response / idle nudge) with the configured
  // bot icon as an avatar, so it's visible against every AI response —
  // not just the floating launcher button.
  function appendBotSideMessage(container, bubble, cfg) {
    var row = document.createElement("div");
    row.className = "gms-chatbot-msg-row";
    if (cfg.bot_icon_url) {
      var avatar = document.createElement("img");
      avatar.className = "gms-chatbot-msg-avatar";
      avatar.src = cfg.bot_icon_url;
      avatar.alt = "";
      row.appendChild(avatar);
    }
    row.appendChild(bubble);
    container.appendChild(row);
  }

  // How long the server took to produce the reply above, in the units a
  // person reads fastest: whole milliseconds under a second, one decimal of a
  // second beyond that. Skipped entirely when the server sent no timing (an
  // unreachable service, or a locally-rendered message like the welcome text),
  // so the line never appears claiming "0 ms".
  function formatDuration(ms) {
    if (ms < 1000) return Math.round(ms) + " ms";
    return (ms / 1000).toFixed(1) + " s";
  }

  function renderResponseTime(container, ms, cfg) {
    if (typeof ms !== "number" || !isFinite(ms) || ms <= 0) return;

    var meta = document.createElement("div");
    meta.className = "gms-chatbot-meta";
    if (cfg.bot_icon_url) meta.style.marginLeft = "28px";
    meta.textContent = "⏱ " + formatDuration(ms);
    container.appendChild(meta);
  }

  // Bot-side like the idle nudge: an error is the chatbot talking, so it gets
  // the same avatar + left-hand placement rather than stretching full width.
  function renderErrorMessage(container, message, cfg) {
    var bubble = document.createElement("div");
    bubble.className = "gms-chatbot-msg gms-chatbot-msg-error";
    bubble.textContent = message;
    appendBotSideMessage(container, bubble, cfg);
  }

  function renderIdleMessage(container, text, cfg) {
    var bubble = document.createElement("div");
    bubble.className = "gms-chatbot-msg gms-chatbot-msg-idle";
    bubble.textContent = text;
    appendBotSideMessage(container, bubble, cfg);
  }

  function renderBotMessage(container, result, cfg) {
    var bubble = document.createElement("div");
    bubble.className = "gms-chatbot-msg gms-chatbot-msg-bot";

    var html = renderMarkdown(result.summary || "");

    if (result.insights && result.insights.length) {
      html += "<ul>" + result.insights.map(function (i) {
        // Escaped first, then emphasis — the same order renderMarkdown enforces, and
        // the reason inlineMarkdown must never be handed raw text.
        return "<li>" + inlineMarkdown(escapeHtml(i)) + "</li>";
      }).join("") + "</ul>";
    }

    if (result.table && result.table.columns && result.table.columns.length) {
      html += '<table class="gms-chatbot-table"><thead><tr>' +
        result.table.columns.map(function (c) { return "<th>" + escapeHtml(c) + "</th>"; }).join("") +
        "</tr></thead><tbody>" +
        result.table.rows.map(function (row) {
          return "<tr>" + row.map(function (cell) {
            return "<td>" + escapeHtml(String(cell)) + "</td>";
          }).join("") + "</tr>";
        }).join("") +
        "</tbody></table>";
    }

    // Nothing to say — skip the bubble entirely. An empty one renders as a
    // stray blank rectangle that reads as a broken reply.
    if (!html.trim()) return;

    bubble.innerHTML = html;
    appendBotSideMessage(container, bubble, cfg);
  }

  // Renders a Menu/Buttons or Dropdown flow prompt: the prompt text as a
  // normal bot bubble, followed by clickable option chips (or a select +
  // confirm for dropdown). Clicking/confirming re-sends via onSelect with
  // the option's value, reusing send()'s own network/loading-state plumbing.
  function renderOptionsMessage(container, data, cfg, onSelect) {
    if (data.text) renderBotMessage(container, { summary: data.text }, cfg);
    renderResponseTime(container, data.response_time_ms, cfg);

    var options = data.options || [];
    var wrap = document.createElement("div");
    wrap.className = "gms-chatbot-options" + (cfg.bot_icon_url ? " gms-chatbot-options-indent" : "");

    if (data.type === "dropdown") {
      var select = document.createElement("select");
      select.className = "gms-chatbot-input";
      options.forEach(function (opt) {
        var optionEl = document.createElement("option");
        optionEl.value = opt.value;
        optionEl.textContent = opt.label;
        select.appendChild(optionEl);
      });
      var confirmBtn = document.createElement("button");
      confirmBtn.className = "gms-chatbot-option";
      confirmBtn.textContent = "Select";
      confirmBtn.addEventListener("click", function () {
        wrap.remove();
        onSelect(select.value, select.selectedOptions[0] ? select.selectedOptions[0].textContent : select.value);
      });
      wrap.appendChild(select);
      wrap.appendChild(confirmBtn);
    } else {
      options.forEach(function (opt) {
        var btn = document.createElement("button");
        btn.className = "gms-chatbot-option";
        btn.type = "button";
        btn.textContent = opt.label;
        btn.addEventListener("click", function () {
          wrap.remove();
          onSelect(opt.value, opt.label);
        });
        wrap.appendChild(btn);
      });
    }

    container.appendChild(wrap);
  }

  // ---------------------------------------------------------------------
  // The download button
  // ---------------------------------------------------------------------
  //
  // A file a flow's Download File block is handing over. Nothing to poll and nothing to
  // update: the file already exists by the time this payload is sent, which is the whole
  // difference between this and the card below — that one watches an export being built.
  //
  // A plain <a download>, not a fetch. The link carries the widget key and the session
  // token in its query string and the route authorises on both, so the browser's own
  // download machinery is all that is needed — and a fetch would have to hold the whole
  // file in memory to hand it back to the same browser.
  function renderFileButton(container, payload, cfg) {
    if (!payload || !payload.url) return;

    var wrap = document.createElement("div");
    wrap.className = "gms-chatbot-file" + (cfg.bot_icon_url ? " gms-chatbot-file-indent" : "");

    var link = document.createElement("a");
    link.className = "gms-chatbot-file-btn";
    link.href = apiUrl(payload.url);
    // The name the operator's block chose, so the visitor's disk gets "invoice_10432.csv"
    // rather than whatever the URL's last segment happens to be.
    link.setAttribute("download", payload.file_name || "");
    link.target = "_blank";
    link.rel = "noopener";
    // textContent, never innerHTML: the label is operator-authored text that may have had
    // a placeholder — and therefore a visitor's own words — substituted into it.
    // (Spelled out rather than shown: this file must contain no template braces at all,
    // which is what proves nothing here is rendered per chatbot key.)
    link.textContent = "\u2b07  " + (payload.label || "Download file");
    // Validated server-side three times over (see FileButtonView); the fallback is here so
    // a payload from an older server still draws a button rather than an unstyled link.
    link.style.background = payload.colour || cfg.brand_color;

    wrap.appendChild(link);

    var meta = fileMeta(payload);
    if (meta) {
      var note = document.createElement("div");
      note.className = "gms-chatbot-file-meta";
      note.textContent = meta;
      wrap.appendChild(note);
    }

    container.appendChild(wrap);
    container.scrollTop = container.scrollHeight;
  }

  // "CSV · 12.4 KB" under the button. The format because a visitor about to click deserves
  // to know what they are getting, the size because a slow connection makes it matter.
  function fileMeta(payload) {
    var parts = [];
    if (payload.file_format) parts.push(String(payload.file_format).toUpperCase());
    var size = formatBytes(payload.byte_size);
    if (size) parts.push(size);
    return parts.join(" \u00b7 ");
  }

  // ---------------------------------------------------------------------
  // The download card
  // ---------------------------------------------------------------------
  //
  // A file the visitor asked for, from the moment it is queued to the moment it can
  // be clicked. It is its own block under the reply that announced it, never inside
  // that bubble, for one reason: it outlives the message. A visitor can carry on
  // asking other things while a hundred thousand records are written, new bubbles
  // appear below, and this keeps updating in place — which is only possible if it was
  // never part of a message in the first place.
  //
  // Nothing here touches the input, the send button or the typing indicator. That is
  // what "you can keep asking while it builds" means in practice: the turn that
  // started the build finished when the reply arrived, and the build is not a turn.

  // The words beside the progress. They rotate so a long build reads as alive rather
  // than stuck. Deliberately vague about which batch is in flight — the record count
  // under them is the precise part, and a label naming a step it is not on would be
  // worse than one naming none.
  var WORKING_WORDS = [
    "Gathering the records",
    "Reading the next batch",
    "Writing rows",
    "Packing the file",
    "Nearly there"
  ];

  var WORD_INTERVAL_MS = 2600;
  var STATUS_POLL_MS = 4000;

  function formatCount(value) {
    var n = Number(value);
    if (!isFinite(n)) return "";
    return n.toLocaleString ? n.toLocaleString() : String(n);
  }

  function formatBytes(value) {
    var n = Number(value);
    if (!isFinite(n) || n <= 0) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function fileLabel(download) {
    var format = String(download.file_format || "csv").toUpperCase();
    return format === "XLS" ? "Excel" : format;
  }

  // Everything the card needs to keep itself up to date, in one object, so the SSE
  // handler and the polling fallback update the same nodes rather than two copies.
  function renderDownloadCard(container, download, cfg) {
    if (!download || !download.uuid) return null;

    var card = document.createElement("div");
    card.className = "gms-chatbot-file" +
      (cfg.bot_icon_url ? " gms-chatbot-file-indent" : "");

    var name = document.createElement("div");
    name.className = "gms-chatbot-file-name";
    name.textContent = "\u{1F4C4} " + (download.file_name || (fileLabel(download) + " file"));

    var sub = document.createElement("div");
    sub.className = "gms-chatbot-file-sub";

    var bar = document.createElement("div");
    bar.className = "gms-chatbot-file-bar";
    var fill = document.createElement("div");
    fill.className = "gms-chatbot-file-fill";
    bar.appendChild(fill);

    var foot = document.createElement("div");

    card.appendChild(name);
    card.appendChild(sub);
    card.appendChild(bar);
    card.appendChild(foot);
    container.appendChild(card);
    container.scrollTop = container.scrollHeight;

    var state = {
      container: container,
      cfg: cfg,
      download: download,
      nameEl: name,
      subEl: sub,
      barEl: bar,
      fillEl: fill,
      footEl: foot,
      wordTimer: null,
      pollTimer: null,
      source: null,
      settled: false,
      wordIndex: 0
    };

    apply(state, download);
    return card;
  }

  // One place that decides what the card looks like, whatever told it to. The turn
  // payload, an SSE frame and a status poll all arrive in the same shape for exactly
  // this reason — three renderers for three sources would drift.
  function apply(state, view) {
    var status = String(view.status || "");

    if (status === "ready" && view.download_url) {
      paintReady(state, view);
      return;
    }
    if (status === "failed" || status === "expired") {
      paintFailed(state, view);
      return;
    }

    paintWorking(state, view);
  }

  function paintWorking(state, view) {
    var written = Number(view.rows_written || 0);
    var total = Number(view.total_rows || state.download.total_rows || 0);

    if (total > 0) {
      // A real fraction of records written. Capped at 99% until the artifact exists,
      // because a full bar next to "still working" is the one thing a progress bar
      // must never say.
      var pct = Math.max(2, Math.min(99, Math.round((written / total) * 100)));
      state.fillEl.style.width = pct + "%";
    }

    var word = WORKING_WORDS[state.wordIndex % WORKING_WORDS.length];

    state.subEl.innerHTML = "";
    var working = document.createElement("span");
    working.className = "gms-chatbot-file-working";
    working.textContent = word + "…";
    state.subEl.appendChild(working);

    if (total > 0 && written > 0) {
      var counts = document.createElement("span");
      counts.textContent = "  " + formatCount(written) + " of " +
        formatCount(total) + " records";
      state.subEl.appendChild(counts);
    } else if (total > 0) {
      var pending = document.createElement("span");
      pending.textContent = "  " + formatCount(total) + " records";
      state.subEl.appendChild(pending);
    }

    if (!state.wordTimer) {
      state.wordTimer = window.setInterval(function () {
        state.wordIndex += 1;
        var el = state.subEl.querySelector(".gms-chatbot-file-working");
        if (el) {
          el.textContent = WORKING_WORDS[state.wordIndex % WORKING_WORDS.length] + "…";
        }
      }, WORD_INTERVAL_MS);
    }

    watch(state);
  }

  function paintReady(state, view) {
    settle(state);

    state.fillEl.style.width = "100%";
    state.barEl.style.display = "none";

    var parts = [];
    var total = view.total_rows || state.download.total_rows;
    if (total) parts.push(formatCount(total) + " records");
    var size = formatBytes(view.byte_size);
    if (size) parts.push(size);

    state.subEl.textContent = parts.join("  ·  ") || "Ready";
    if (view.file_name) state.nameEl.textContent = "\u{1F4C4} " + view.file_name;

    state.footEl.innerHTML = "";

    // An anchor rather than a button: it is a link to a file, so a middle-click, a
    // right-click "save as" and a keyboard Enter all do what the visitor expects,
    // none of which a button with a click handler would give them.
    //
    // The href must name this application's host, and getting that wrong is silent.
    // A bare path is resolved by the browser against the *embedding page*, so it asks
    // the operator's own site for the file and the visitor is told it is unavailable
    // for a file that exists and is being served perfectly a hostname away. The server
    // sends an absolute URL when SITE_URL is configured; apiUrl() covers the case where
    // it is not and apiBase is what names the host instead.
    var link = document.createElement("a");
    link.className = "gms-chatbot-file-btn";
    link.href = apiUrl(view.download_url);
    link.setAttribute("download", view.file_name || "");
    link.rel = "noopener";
    link.textContent = "⬇  Download " + fileLabel(state.download);
    state.footEl.appendChild(link);
    state.container.scrollTop = state.container.scrollHeight;
  }

  function paintFailed(state, view) {
    settle(state);

    state.barEl.style.display = "none";
    state.subEl.textContent = "";
    state.footEl.innerHTML = "";

    var problem = document.createElement("div");
    problem.className = "gms-chatbot-file-failed";
    problem.textContent = view.error_message ||
      "The file could not be created at the moment. Please try again.";
    state.footEl.appendChild(problem);
  }

  // Stops every timer and socket the card owns. Called on any terminal state, and
  // that is the whole of the card's cleanup — a card left holding a live EventSource
  // would have the browser reopen it forever, re-running the progress stream.
  function settle(state) {
    state.settled = true;
    if (state.wordTimer) { window.clearInterval(state.wordTimer); state.wordTimer = null; }
    if (state.pollTimer) { window.clearInterval(state.pollTimer); state.pollTimer = null; }
    if (state.source) { state.source.close(); state.source = null; }
  }

  // Live progress while the file is built. The stream is the fast path; the status
  // poll is what keeps the card honest when that stream drops, which a long build
  // makes likely — the server bounds how long one progress stream stays open, and a
  // proxy between us and it may bound it harder.
  function watch(state) {
    if (state.settled || state.source || state.pollTimer) return;

    var progressUrl = state.download.progress_url;

    if (!progressUrl || typeof window.EventSource === "undefined") {
      pollStatus(state);
      return;
    }

    var source;
    try {
      source = new EventSource(apiUrl(progressUrl));
    } catch (ignored) {
      pollStatus(state);
      return;
    }

    state.source = source;

    ["progress", "retry"].forEach(function (kind) {
      source.addEventListener(kind, function (message) {
        try {
          apply(state, JSON.parse(message.data));
        } catch (ignored) {
          // A frame we cannot read changes nothing; the next one will.
        }
      });
    });

    source.addEventListener("ready", function (message) {
      try { apply(state, JSON.parse(message.data)); } catch (ignored) { pollStatus(state); }
    });

    source.addEventListener("failed", function (message) {
      try { apply(state, JSON.parse(message.data)); } catch (ignored) { settle(state); }
    });

    source.addEventListener("error", function () {
      // Closed first: the browser reopens a stream that ended by itself, and every
      // close arrives here whether the export finished or the socket died. If the
      // export had finished we would already have settled, so reaching this means
      // the connection went and the build has not.
      if (state.source) { state.source.close(); state.source = null; }
      if (!state.settled) pollStatus(state);
    });
  }

  function pollStatus(state) {
    if (state.settled || state.pollTimer || !state.download.status_url) return;

    state.pollTimer = window.setInterval(function () {
      fetch(apiUrl(state.download.status_url), { credentials: "omit" })
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (view) { if (view) apply(state, view); })
        .catch(function (err) {
          // Reported once, not once every few seconds. The operator needs to know the
          // card has gone blind, and a poll that failed will almost certainly keep
          // failing — repeating it would bury everything else in their console.
          if (state.pollWarned) return;
          state.pollWarned = true;
          warnFailure(
            "could not read download progress, so the file card will stop updating",
            apiUrl(state.download.status_url),
            err && err.message
          );
        });
    }, STATUS_POLL_MS);
  }

  function newSessionToken() {
    return (window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID() : String(Date.now()) + Math.random().toString(36).slice(2);
  }

  function getSessionId() {
    var storageKey = "gms_chatbot_session_" + API_KEY;
    try {
      var existing = window.localStorage.getItem(storageKey);
      if (existing) return existing;
      var fresh = newSessionToken();
      window.localStorage.setItem(storageKey, fresh);
      return fresh;
    } catch (e) {
      return ""; // localStorage unavailable (e.g. private mode) — flows just won't track state
    }
  }

  // Mints a brand-new session token so the next /message call starts this
  // visitor's flow over from its Start node (see the widget's Restart
  // button) — the old session row, if any, is just left behind unused
  // rather than deleted, matching this feature's lazy-expiry philosophy.
  function resetSessionId() {
    try {
      window.localStorage.setItem("gms_chatbot_session_" + API_KEY, newSessionToken());
    } catch (e) {
      // localStorage unavailable — getSessionId() already no-ops in this case too.
    }
  }

  function init(cfg) {
    injectStyle(cfg);
    var dom = buildDom(cfg);
    var messagesEl = dom.panel.querySelector(".gms-chatbot-messages");
    var typingEl = dom.panel.querySelector(".gms-chatbot-typing");
    var inputEl = dom.panel.querySelector(".gms-chatbot-input");
    var sendBtn = dom.panel.querySelector(".gms-chatbot-send");
    var closeBtn = dom.panel.querySelector(".gms-chatbot-close");
    var restartBtn = dom.panel.querySelector(".gms-chatbot-restart");

    var welcomed = false;
    var idleShown = false;
    var idleTimer = null;

    function clearIdleTimer() {
      if (idleTimer) {
        clearTimeout(idleTimer);
        idleTimer = null;
      }
    }

    function armIdleTimer() {
      clearIdleTimer();
      if (!cfg.idle_text || idleShown) return;
      idleTimer = setTimeout(function () {
        idleShown = true;
        renderIdleMessage(messagesEl, cfg.idle_text, cfg);
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }, IDLE_TIMEOUT_MS);
    }

    dom.launcher.addEventListener("click", function () {
      dom.panel.classList.toggle("gms-open");
      if (dom.panel.classList.contains("gms-open")) {
        inputEl.focus();
        if (!welcomed && cfg.welcome_text) {
          welcomed = true;
          renderBotMessage(messagesEl, { summary: cfg.welcome_text }, cfg);
        }
        armIdleTimer();
      } else {
        clearIdleTimer();
      }
    });
    closeBtn.addEventListener("click", function () {
      clearIdleTimer();
      if (cfg.closing_text) {
        renderBotMessage(messagesEl, { summary: cfg.closing_text }, cfg);
        messagesEl.scrollTop = messagesEl.scrollHeight;
        setTimeout(function () {
          dom.panel.classList.remove("gms-open");
        }, 900);
      } else {
        dom.panel.classList.remove("gms-open");
      }
    });
    restartBtn.addEventListener("click", function () {
      clearIdleTimer();
      resetSessionId();
      messagesEl.innerHTML = "";
      welcomed = false;
      idleShown = false;
      if (cfg.welcome_text) {
        welcomed = true;
        renderBotMessage(messagesEl, { summary: cfg.welcome_text }, cfg);
      }
      inputEl.focus();
      armIdleTimer();
    });

    // opts: { text, selectedValue, displayText, skipUserBubble } — free-text
    // turns pass just text; a button/dropdown reply passes selectedValue
    // (sent to the server) plus displayText (what the visitor "said", shown
    // in their own chat bubble).
    function send(opts) {
      opts = opts || {};
      var text = opts.text != null ? opts.text : inputEl.value.trim();
      var selectedValue = opts.selectedValue;
      if (!text && !selectedValue) return;

      clearIdleTimer();
      if (!opts.skipUserBubble) {
        renderUserMessage(messagesEl, opts.displayText || text);
      }
      inputEl.value = "";
      inputEl.disabled = true;
      sendBtn.disabled = true;
      typingEl.style.display = "block";
      messagesEl.scrollTop = messagesEl.scrollHeight;

      var messageUrl = API_BASE + "/public/chatbot/message";

      // Stream the reply when we can. A data-agent turn runs real queries and can
      // take a long time, and a typing indicator that says nothing for a minute is
      // indistinguishable from a broken widget. A button/dropdown reply is never
      // streamed — it is a flow answer, which arrives whole — and neither is a turn
      // for a chatbot with no agent attached; both cases come back as one `fallback`
      // event and this function retries the POST below.
      if (!selectedValue && typeof window.EventSource !== "undefined") {
        if (streamSend(text)) return;
      }

      postMessage(text, selectedValue, messageUrl);
    }

    // Opens the SSE turn. Returns false when it could not even be started, so the
    // caller falls straight back to the POST.
    function streamSend(text) {
      var url = API_BASE + "/public/chatbot/message-stream" +
        "?api_key=" + encodeURIComponent(API_KEY) +
        "&message=" + encodeURIComponent(text) +
        "&session_id=" + encodeURIComponent(getSessionId());

      var source;
      try {
        source = new EventSource(url);
      } catch (ignored) {
        return false;
      }

      var bubble = null;
      var answer = "";
      var settled = false;   // something arrived, so the stream owns this turn
      var finished = false;  // `done` arrived, so the disconnect that follows is expected

      function finish() {
        if (source) { source.close(); source = null; }
        inputEl.disabled = false;
        sendBtn.disabled = false;
        typingEl.style.display = "none";
        messagesEl.scrollTop = messagesEl.scrollHeight;
        inputEl.focus();
        armIdleTimer();
      }

      // The bubble is created on the first token rather than up front, so a turn
      // that falls back or fails never leaves an empty rectangle behind.
      //
      // Rendered through renderMarkdown, exactly as the non-streamed reply is
      // (renderBotMessage). Painting the raw text instead — which this did — showed
      // the visitor the literal `**bold**` and `| a | b |` the model was told to write
      // by grounding rule 14, so a streamed turn displayed a table as a wall of pipes
      // while the identical answer arriving by POST rendered correctly.
      //
      // Re-rendering the whole answer per token rather than appending is deliberate:
      // markdown is block-structured, so the last line of a partial stream can change
      // meaning when the next one arrives (a table row is only a table once its
      // divider is read). A few KB re-parsed per token is not a cost worth splitting
      // the renderer in two for.
      //
      // innerHTML is safe here for the reason it is safe in renderBotMessage, and only
      // that reason: renderMarkdown escapes before it parses. Never assign `answer`
      // itself here.
      function paint() {
        if (!bubble) {
          bubble = document.createElement("div");
          bubble.className = "gms-chatbot-msg gms-chatbot-msg-bot";
          appendBotSideMessage(messagesEl, bubble, cfg);
          typingEl.style.display = "none";
        }
        bubble.innerHTML = renderMarkdown(answer);
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }

      source.addEventListener("fallback", function () {
        settled = true;
        if (source) { source.close(); source = null; }
        postMessage(text, undefined, API_BASE + "/public/chatbot/message");
      });

      source.addEventListener("token", function (message) {
        settled = true;
        try {
          answer += JSON.parse(message.data).text || "";
        } catch (ignored) { return; }
        paint();
      });

      source.addEventListener("done", function (message) {
        settled = true;
        finished = true;

        var download = null;

        try {
          var payload = JSON.parse(message.data);
          if (payload.answer) { answer = payload.answer; paint(); }
          download = payload.download || null;
        } catch (ignored) {
          // Whatever was streamed already stands.
        }

        finish();

        // After finish(), so the input is already back in the visitor's hands before
        // the card appears. The build is not a turn and must not hold the widget.
        if (download) renderDownloadCard(messagesEl, download, cfg);
      });

      source.addEventListener("error", function (message) {
        // Closed first: the browser reopens a stream that ended on its own, and that
        // re-runs the whole turn.
        if (source) { source.close(); source = null; }

        if (finished) {
          // The turn ended, so the stream ended. The browser reports every close as an
          // error, success included — this one is not one. Without this check a turn
          // that answered with no text at all would be overwritten by a failure notice.
          return;
        }

        // Two completely different things arrive at this one listener, and telling
        // them apart is what `data` is for. The server's own `error` event carries a
        // JSON payload — it is a turn that ran and failed for a reason the visitor
        // should be told (a misconfigured agent, a timeout, a rate limit). A transport
        // failure carries nothing.
        //
        // Treating the first as the second is not a cosmetic mistake: it re-POSTs the
        // whole turn, so a failing chatbot silently answers every question twice and
        // bills the owner for both, while the actual reason never reaches the screen.
        var detail = "";
        if (message && message.data) {
          settled = true;
          try { detail = JSON.parse(message.data).message || ""; } catch (ignored) {}
        }

        if (!settled) {
          // Nothing arrived at all — the endpoint may not exist on this server, or
          // a proxy is buffering the stream. Fall back rather than fail: the POST
          // is the path that has always worked.
          postMessage(text, undefined, API_BASE + "/public/chatbot/message");
          return;
        }

        if (!answer) {
          renderErrorMessage(messagesEl, detail || "Something went wrong. Please try again.", cfg);
        }
        finish();
      });

      return true;
    }

    function postMessage(text, selectedValue, messageUrl) {
      typingEl.style.display = "block";

      fetch(messageUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          api_key: API_KEY,
          message: text,
          session_id: getSessionId(),
          selected_value: selectedValue
        })
      })
        .then(function (r) {
          return r.json().then(
            function (data) { return { ok: r.ok, status: r.status, data: data }; },
            function () { return { ok: r.ok, status: r.status, data: null }; }
          );
        })
        .then(function (res) {
          if (res.ok && res.data && res.data.status === "success") {
            renderResponse(res.data);
            return;
          }

          // The server's own message is shown to the visitor — it is written for
          // one ("I cannot retrieve that figure right now"). The operator gets the
          // status code alongside it, because a rejected key or a domain that is
          // not allow-listed reads identically to a failed answer from in here.
          var reason = (res.data && res.data.message) || "";
          warnFailure(
            "the API rejected a visitor's message.",
            messageUrl,
            "HTTP " + res.status + (reason ? " — " + reason : "")
          );
          renderErrorMessage(messagesEl, reason || "Something went wrong. Please try again.", cfg);
          renderResponseTime(messagesEl, res.data && res.data.response_time_ms, cfg);
        })
        .catch(function (err) {
          // No response at all — the request was blocked, the API is unreachable, or
          // the network dropped. The visitor is told the truth without being shown
          // infrastructure; the reason and the likely cause go to the console.
          warnFailure(
            "could not reach the API to send a visitor's message.",
            messageUrl,
            (err && err.message) || "the request did not complete"
          );
          renderErrorMessage(messagesEl, "Could not reach the chatbot service. Please try again.", cfg);
        })
        .then(function () {
          inputEl.disabled = false;
          sendBtn.disabled = false;
          typingEl.style.display = "none";
          messagesEl.scrollTop = messagesEl.scrollHeight;
          inputEl.focus();
          armIdleTimer();
        });
    }

    // Dispatches a successful /message response by its `type` — additive on
    // top of the pre-existing plain-text rendering, so a chatbot with no
    // active flow (type always "text") behaves exactly as before.
    function renderResponse(data) {
      var type = data.type || "text";
      if (type === "buttons" || type === "dropdown") {
        // Renders its own timing line, between the prompt and the choices.
        renderOptionsMessage(messagesEl, data, cfg, function (value, label) {
          send({ text: "", selectedValue: value, displayText: label });
        });
      } else if (type === "text_prompt") {
        renderBotMessage(messagesEl, { summary: data.text || "" }, cfg);
        renderResponseTime(messagesEl, data.response_time_ms, cfg);
      } else if (type === "flow_ended") {
        // No special rendering — input stays enabled for the visitor to keep chatting.
      } else {
        renderBotMessage(messagesEl, data, cfg);
        renderResponseTime(messagesEl, data.response_time_ms, cfg);
      }

      // After the reply, whatever kind it was. The card is what the sentence above it
      // is about, and it goes on updating long after this turn is over.
      if (data.download) renderDownloadCard(messagesEl, data.download, cfg);

      // And the button, likewise after the reply — under the words the operator wrote
      // about it rather than instead of them. Only the POST path draws one: a flow turn
      // never streams (see chatbot_turn_service.stream_turn), so a Download File block's
      // payload cannot arrive on the SSE path, and wiring it there would imply it could.
      if (data.file_download) renderFileButton(messagesEl, data.file_download, cfg);
    }

    sendBtn.addEventListener("click", function () { send(); });
    inputEl.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });
  }

  function start() {
    fetchConfig().then(init);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
"""


def build_widget_script() -> str:
    """
    Return the static, generic widget shell — byte-identical for every
    chatbot key, since nothing key-specific is templated into it. The
    embedding page supplies ``apiKey`` (and optionally ``apiBase``, which can be
    left out when the API shares the page's origin) via
    ``window.GMSChatbotConfig`` (see the embed snippet on the Widget
    Settings page), and every appearance/behavior setting is fetched by the
    script itself at runtime from GET /public/chatbot/widget-config.
    """
    return _WIDGET_SCRIPT_TEMPLATE

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
from app.models.chatbot import ChatbotApiKey, ChatbotMessage
from app.models.datasource import DatasourceFile
from app.services.ai_analytics.ai_analytics_service import datasource_crud, run_grounded_prompt
from app.services.data_agents import data_agent_service
from app.services.workspaces import workspace_service

chatbot_key_crud = CRUDQueryBuilder(ChatbotApiKey)

_MAX_MESSAGE_LEN = 2000
_VALID_TARGET_TYPES = {"datasource", "file", "table", "collection"}
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
    datasource_id: uuid.UUID,
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
    """
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    if target_type not in _VALID_TARGET_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid target_type: {target_type!r}")

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

    resolved_workspace_id, resolved_agent_id = await _resolved_agent_attachment(
        db, user_id, workspace_id, data_agent_id,
    )

    chatbot_key = await chatbot_key_crud.create(db, {
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

    # A chatbot is never left without a persona: this seeds its agent name,
    # default system prompt and prompt variables straight away, so the very
    # first visitor message is answered with a real prompt rather than a
    # placeholder created later on first settings-page visit.
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
    from a data profile.
    """
    chatbot_key = await get_chatbot_key(db, user_id, key_id)

    resolved_workspace_id, resolved_agent_id = await _resolved_agent_attachment(
        db, user_id, workspace_id, data_agent_id,
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
# hand. Every appearance setting (colors, fonts, logo/icon URLs,
# welcome/idle/closing text) and every API-key/origin validity check is
# resolved server-side, fetched at runtime from GET
# /public/chatbot/widget-config — so changing settings in the dashboard
# takes effect on the next page load, and this downloaded file never goes
# stale or needs replacing after a settings change.
_WIDGET_SCRIPT_TEMPLATE = r"""
(function () {
  "use strict";

  var CFG = window.GMSChatbotConfig || {};
  var API_BASE = CFG.apiBase;
  var API_KEY = CFG.apiKey;
  var IDLE_TIMEOUT_MS = 45000;

  if (!API_BASE || !API_KEY) {
    console.error(
      "GetMyStuff chatbot widget: set window.GMSChatbotConfig = { apiBase, apiKey } " +
      "before loading this script."
    );
    return;
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

  function fetchConfig() {
    return fetch(API_BASE + "/public/chatbot/widget-config?api_key=" + encodeURIComponent(API_KEY))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data && data.status === "success") return data;
        return DEFAULT_CONFIG;
      })
      .catch(function () { return DEFAULT_CONFIG; });
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
      ".gms-chatbot-table{border-collapse:collapse;margin-top:6px;font-size:11px;}" +
      ".gms-chatbot-table th,.gms-chatbot-table td{border:1px solid #ced4da;padding:3px 6px;}" +
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
      "margin:-6px 0 10px;display:flex;align-items:center;gap:4px;}"
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
    return div.innerHTML;
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

    var html = escapeHtml(result.summary || "");

    if (result.insights && result.insights.length) {
      html += "<ul>" + result.insights.map(function (i) {
        return "<li>" + escapeHtml(i) + "</li>";
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

      fetch(API_BASE + "/public/chatbot/message", {
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
          return r.json().then(function (data) { return { ok: r.ok, data: data }; });
        })
        .then(function (res) {
          if (res.ok && res.data && res.data.status === "success") {
            renderResponse(res.data);
          } else {
            renderErrorMessage(messagesEl, (res.data && res.data.message) || "Something went wrong. Please try again.", cfg);
            renderResponseTime(messagesEl, res.data && res.data.response_time_ms, cfg);
          }
        })
        .catch(function () {
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
    embedding page supplies ``apiBase``/``apiKey`` via
    ``window.GMSChatbotConfig`` (see the embed snippet on the Widget
    Settings page), and every appearance/behavior setting is fetched by the
    script itself at runtime from GET /public/chatbot/widget-config.
    """
    return _WIDGET_SCRIPT_TEMPLATE

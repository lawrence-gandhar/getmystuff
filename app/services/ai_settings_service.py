"""
Business logic for user-managed AI provider API keys (AI Settings module).

Rule enforced here (not in the schema): activating a key deactivates any
other active key for the same (user, provider) pair, so at most one key per
provider is ever "in use" at a time.
"""

import uuid
from typing import List, Optional

from litestar.exceptions import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.models.ai_settings import AI_PROVIDER_DISPLAY, AI_PROVIDER_VALUES, AIApiKey
from app.utils.crypto import decrypt_password, encrypt_password

ai_key_crud = CRUDQueryBuilder(AIApiKey)

_KEY_VISIBLE_SUFFIX = 4


def _mask_key(decrypted_key: str) -> str:
    if len(decrypted_key) <= _KEY_VISIBLE_SUFFIX:
        return "*" * len(decrypted_key)
    return "••••••••" + decrypted_key[-_KEY_VISIBLE_SUFFIX:]


def _validate_provider(provider: str) -> None:
    if provider not in AI_PROVIDER_VALUES:
        raise HTTPException(status_code=400, detail=f"Invalid provider: {provider!r}")


def _validate_provider_fields(
    provider: str,
    base_url: Optional[str],
    model_name: Optional[str],
) -> None:
    """
    OpenAI-compatible providers need to know which endpoint and model to call:
      - "other" (Cerebras, Groq, Together, a self-hosted server, ...): both
        the base URL and model name are unknown to us, so both are required.
      - "openai": the base URL can default to OpenAI's own API, but the
        model name can't be safely guessed, so it's still required.
    Every other provider is stored but not wired into any AI call yet, so no
    extra fields are required for them.
    """
    if provider == "other":
        if not base_url:
            raise HTTPException(status_code=400, detail="Base URL is required for a custom (Other) provider")
        if not model_name:
            raise HTTPException(status_code=400, detail="Model name is required for a custom (Other) provider")
    elif provider == "openai":
        if not model_name:
            raise HTTPException(status_code=400, detail="Model name is required for OpenAI")


async def _deactivate_other_keys(
    db: AsyncSession,
    user_id: int,
    provider: str,
    exclude_id: Optional[int] = None,
) -> None:
    """Clear is_active on every other key for this (user, provider) pair."""
    stmt = (
        update(AIApiKey)
        .where(
            AIApiKey.user_id == user_id,
            AIApiKey.provider == provider,
            AIApiKey.is_active == True,  # noqa: E712
        )
        .values(is_active=False)
    )
    if exclude_id is not None:
        stmt = stmt.where(AIApiKey.id != exclude_id)
    await db.execute(stmt)


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------

async def get_user_api_keys(db: AsyncSession, user_id: int) -> List[AIApiKey]:
    """
    Return every API key belonging to the user, each annotated (as transient,
    non-persisted attributes) with a masked key preview and a human-readable
    provider label for direct use in templates.
    """
    result = await db.execute(
        select(AIApiKey)
        .where(AIApiKey.user_id == user_id)
        .order_by(AIApiKey.provider, AIApiKey.created_at.desc())
    )
    keys = list(result.scalars().all())

    for key in keys:
        key.masked_key = _mask_key(decrypt_password(key.api_key_encrypted))
        key.provider_display = AI_PROVIDER_DISPLAY.get(key.provider, key.provider)

    return keys


async def get_active_key_details(
    db: AsyncSession,
    user_id: int,
    provider: str,
) -> Optional[dict]:
    """
    Return the active key for (user, provider) as
    {"api_key", "base_url", "model_name"}, or None if no active key exists.
    """
    result = await db.execute(
        select(AIApiKey).where(
            AIApiKey.user_id == user_id,
            AIApiKey.provider == provider,
            AIApiKey.is_active == True,  # noqa: E712
        )
    )
    key = result.scalar_one_or_none()
    if not key:
        return None
    return {
        "api_key": decrypt_password(key.api_key_encrypted),
        "base_url": key.base_url,
        "model_name": key.model_name,
    }


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------

async def create_api_key(
    db: AsyncSession,
    user_id: int,
    provider: str,
    label: str,
    api_key: str,
    is_active: bool = False,
    base_url: Optional[str] = None,
    model_name: Optional[str] = None,
) -> AIApiKey:
    _validate_provider(provider)

    label = (label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="Label is required")

    api_key = (api_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required")

    base_url = (base_url or "").strip() or None
    model_name = (model_name or "").strip() or None
    _validate_provider_fields(provider, base_url, model_name)

    if is_active:
        await _deactivate_other_keys(db, user_id, provider)

    return await ai_key_crud.create(db, {
        "user_id": user_id,
        "provider": provider,
        "label": label,
        "api_key_encrypted": encrypt_password(api_key),
        "is_active": is_active,
        "base_url": base_url,
        "model_name": model_name,
    })


async def update_api_key(
    db: AsyncSession,
    user_id: int,
    key_id: uuid.UUID,
    label: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model_name: Optional[str] = None,
) -> AIApiKey:
    """Edit a key's label, secret value, base URL, and/or model name. Provider is immutable."""
    existing = await ai_key_crud.get_by_uuid(db, key_id, extra_filters={"user_id": user_id})
    if not existing:
        raise HTTPException(status_code=404, detail="API key not found")

    data = {}

    if label is not None:
        label = label.strip()
        if not label:
            raise HTTPException(status_code=400, detail="Label is required")
        data["label"] = label

    if api_key:
        data["api_key_encrypted"] = encrypt_password(api_key.strip())

    if base_url is not None:
        data["base_url"] = base_url.strip() or None

    if model_name is not None:
        data["model_name"] = model_name.strip() or None

    if "base_url" in data or "model_name" in data:
        _validate_provider_fields(
            existing.provider,
            data.get("base_url", existing.base_url),
            data.get("model_name", existing.model_name),
        )

    if not data:
        return existing

    return await ai_key_crud.update(db, existing.id, data)


async def toggle_active_status(
    db: AsyncSession,
    user_id: int,
    key_id: uuid.UUID,
) -> AIApiKey:
    existing = await ai_key_crud.get_by_uuid(db, key_id, extra_filters={"user_id": user_id})
    if not existing:
        raise HTTPException(status_code=404, detail="API key not found")

    new_status = not existing.is_active
    if new_status:
        await _deactivate_other_keys(db, user_id, existing.provider, exclude_id=existing.id)

    return await ai_key_crud.update(db, existing.id, {"is_active": new_status})


async def delete_api_key(db: AsyncSession, user_id: int, key_id: uuid.UUID) -> None:
    existing = await ai_key_crud.get_by_uuid(db, key_id, extra_filters={"user_id": user_id})
    if not existing:
        raise HTTPException(status_code=404, detail="API key not found")
    await ai_key_crud.delete(db, existing.id)

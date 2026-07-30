import uuid as uuid_pkg
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import jwt, JWTError, ExpiredSignatureError
from passlib.context import CryptContext

from litestar.connection import Request
from litestar.exceptions import HTTPException

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.models import User


# =====================================================
# CONFIG (Move to ENV in production)
# =====================================================

SECRET_KEY = "super-secret-key"
ALGORITHM = "HS256"

ACCESS_TOKEN_EXPIRE_MINUTES = 60
REFRESH_TOKEN_EXPIRE_DAYS = 7


# =====================================================
# PASSWORD HASHING
# =====================================================

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _truncate_password(password: str) -> bytes:
    """bcrypt only processes the first 72 bytes of a password."""
    return password.encode("utf-8")[:72]


def hash_password(password: str) -> str:
    return pwd_context.hash(_truncate_password(password))


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(_truncate_password(plain_password), hashed_password)


# =====================================================
# TOKEN CREATION
# =====================================================

def _create_token(data: dict, expires_delta: timedelta, token_type: str) -> str:
    expire = datetime.now(timezone.utc) + expires_delta

    payload = {
        **data,
        "exp": expire,
        "type": token_type,
        "iat": datetime.now(timezone.utc),
    }

    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(user_uuid: str) -> str:
    return _create_token(
        data={"sub": str(user_uuid)},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        token_type="access",
    )


def create_refresh_token(user_uuid: str) -> str:
    return _create_token(
        data={"sub": str(user_uuid)},
        expires_delta=timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        token_type="refresh",
    )


# =====================================================
# TOKEN DECODING
# =====================================================

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


# =====================================================
# AUTHENTICATION
# =====================================================

async def authenticate_user(
    db: AsyncSession,
    email: str,
    password: str
) -> Optional[User]:

    stmt = select(User).where(User.email == email)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        return None

    if not verify_password(password, user.password):
        return None

    return user


# =====================================================
# GET CURRENT USER FROM TOKEN
# =====================================================

async def get_current_user(
    db: AsyncSession,
    token: str
) -> User:

    payload = decode_token(token)

    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")

    user_uuid = payload.get("sub")

    if not user_uuid:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    try:
        user_uuid = uuid_pkg.UUID(user_uuid)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid token payload")

    stmt = select(User).where(User.uuid == user_uuid)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user


# =====================================================
# ROLE CHECK
# =====================================================

def require_role(user: User, role: str):
    if getattr(user, "role", None) != role:
        raise HTTPException(status_code=403, detail="Insufficient permissions")


# =====================================================
# LITESTAR DEPENDENCY
# =====================================================

async def require_auth(request: Request, db: AsyncSession) -> User:
    token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = await get_current_user(db, token)

    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    return user
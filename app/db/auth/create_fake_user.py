"""
DEV ONLY seeder for the test admin account.

Called from main.py's on_startup so a fresh database is immediately usable, and
still runnable by hand against an already-running stack:

    docker compose exec app python -m app.db.auth.create_fake_user
"""

import asyncio
import logging

from sqlalchemy import select

from app.db.db_sessions import AsyncSessionLocal
from app.db.models import User

# Imported from the implementation module rather than the `app.db.auth` package:
# the package's __init__ re-exports this module, so going back through it would
# be a circular import.
from app.db.auth.auth import hash_password

logger = logging.getLogger(__name__)

FAKE_ADMIN_EMAIL = "admin@test.com"
FAKE_ADMIN_PASSWORD = "admin123"


async def create_fake_user() -> None:
    """
    Create the test admin if it is not already present.

    Idempotent: safe to call on every boot. Errors are NOT swallowed — a seeder
    that fails against a reachable database means the schema or the connection
    is wrong, and booting into a half-provisioned app hides that.
    """
    async with AsyncSessionLocal() as db:  # type: AsyncSession

        result = await db.execute(select(User).where(User.email == FAKE_ADMIN_EMAIL))
        existing_user = result.scalar_one_or_none()

        if existing_user:
            logger.info("Test admin '%s' already exists — skipping seed.", FAKE_ADMIN_EMAIL)
            return

        fake_user = User(
            email=FAKE_ADMIN_EMAIL,
            password=hash_password(FAKE_ADMIN_PASSWORD),
            role="admin",
            is_active=True,
        )

        db.add(fake_user)
        await db.commit()

        logger.info(
            "Seeded test admin — email: %s / password: %s",
            FAKE_ADMIN_EMAIL, FAKE_ADMIN_PASSWORD,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    asyncio.run(create_fake_user())

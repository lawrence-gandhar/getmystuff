import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_sessions import AsyncSessionLocal
from app.db.models import User
from app.db.auth import hash_password


async def create_fake_user():
    async with AsyncSessionLocal() as db:  # type: AsyncSession

        email = "admin@test.com"

        # Check if user already exists
        result = await db.execute(select(User).where(User.email == email))
        existing_user = result.scalar_one_or_none()

        if existing_user:
            print("User already exists.")
            return

        fake_user = User(
            email=email,
            password=hash_password("admin123"),
            role="admin",
            is_active=True,
        )

        db.add(fake_user)
        await db.commit()

        print("Fake admin user created!")
        print("Email:", email)
        print("Password: admin123")


if __name__ == "__main__":
    asyncio.run(create_fake_user())
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
)

from dotenv import load_dotenv
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


# Create async engine
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
)


# Create session factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
)


# Correct dependency (NO decorator)
async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
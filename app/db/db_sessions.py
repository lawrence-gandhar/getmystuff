from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
)

DATABASE_URL = "postgresql+asyncpg://postgres:1234@localhost:5432/getmystuff"


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
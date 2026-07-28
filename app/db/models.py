"""
Model registry — import every SQLAlchemy model here so that
``Base.metadata`` is fully populated before Alembic or the async
engine creates/migrates tables.  The imports are intentional
side-effects; linter "unused import" warnings are expected.
"""

from app.db.base import Base  # noqa: F401
from app.models.user import User  # noqa: F401
from app.models.datasource import (  # noqa: F401
    DataSource,
    DatasourceToolBaseConfig,
    DataSourceAgentConfig,
    DatasourceFile,
)
from app.models.prompt_history import PromptHistory  # noqa: F401
from app.models.ai_settings import AIApiKey  # noqa: F401

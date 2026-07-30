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
from app.models.ai_analytics import PromptHistory  # noqa: F401
from app.models.ai_settings import AIApiKey  # noqa: F401
from app.models.chatbot import ChatbotApiKey, ChatbotMessage  # noqa: F401
from app.models.flow_builder import (  # noqa: F401
    ChatbotFlow,
    ChatbotFlowSession,
    FlowNodeKnowledgeBase,
    FlowNodeKnowledgeDocument,
)
from app.models.ai_inbuilt import KnowledgeChunk  # noqa: F401

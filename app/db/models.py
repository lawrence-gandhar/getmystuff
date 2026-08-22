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
from app.models.chatbot import (  # noqa: F401
    ChatbotAction,
    ChatbotActionLink,
    ChatbotAiSettings,
    ChatbotApiKey,
    ChatbotMessage,
    ChatbotWidgetSettings,
)
from app.models.flow_builder import (  # noqa: F401
    ChatbotFlow,
    ChatbotFlowSession,
    FlowNodeKnowledgeBase,
    FlowNodeKnowledgeDocument,
)
from app.models.ai_inbuilt import KnowledgeChunk  # noqa: F401
from app.models.workspaces import Workspace  # noqa: F401
from app.models.data_agents import DataAgent  # noqa: F401
from app.models.tool_configs import ToolConfig, ToolConfigLink  # noqa: F401
from app.models.downloader_agents import (  # noqa: F401
    DownloadExport,
    DownloadExportPart,
    DownloadJob,
)
from app.models.graph_designer import (  # noqa: F401
    ToolGraph,
    ToolGraphRun,
    ToolGraphRunStep,
)
from app.models.email_dispatch import (  # noqa: F401
    EmailMessage,
    EmailMessageAttempt,
    EmailSmtpConfig,
    EmailTemplate,
    EmailTrigger,
)
from app.models.integrations import (  # noqa: F401
    IntegrationConnection,
    IntegrationCredential,
    IntegrationCredentialEvent,
    IntegrationCursor,
    IntegrationFlow,
    IntegrationFlowVersion,
    IntegrationOAuthState,
    IntegrationRateCounter,
    IntegrationRestOperation,
    IntegrationRun,
    IntegrationRunJob,
    IntegrationRunRecord,
    IntegrationRunStep,
    IntegrationSyncKey,
    IntegrationTrigger,
)

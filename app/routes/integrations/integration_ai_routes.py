"""
HTTP layer for drafting a workflow from a sentence.

Two handlers and one rule between them: **generating and saving are separate requests.**
``POST /generate`` returns a drawing and saves nothing; ``POST /save-draft`` is a second
request a person makes by pressing Save. That is not a UI preference — it is what makes
"a refusal leaves zero rows behind" a property rather than a hope, and it is what the
hallucination tests assert on. An implementation that saved and then validated would pass
a test that only checked the refusal.

**Every handler wraps its service call in ``record_turn()``.** All three provider paths
already call ``record_llm_call``, and it is a **no-op when no turn is open** — miss this and
the feature's entire token spend is invisible, silently, with nothing anywhere saying so.
That is the one trap in this file that costs money rather than correctness.

**Both panels are HTMX partials loaded on click**, so a 503 from ``resolve_provider`` lands
inside a div rather than failing the canvas. The canvas works without any of this: the
palette, the mapping grid and the run dock are all reachable with the AI panel never
opened, which is the rule every AI surface in this application follows.
"""

import logging
import uuid as uuid_pkg
from typing import Optional

from litestar import Controller, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.schemas.integrations import FlowCreateRequest
from app.services.integrations import flow_service
from app.services.integrations.ai import workflow_author
from app.services.integrations.ai.workflow_author import DraftRefused
from app.services.integrations.errors import FlowValidationError
from app.utils.turn_recorder import record_turn

logger = logging.getLogger(__name__)

_DRAFT_TEMPLATE = "integrations/partials/ai_draft.htm"
_ERROR_TEMPLATE = "integrations/partials/ai_error.htm"

#: How long an instruction may be. Generous — somebody describing a sync across three
#: systems needs room — and bounded, because everything past this is prompt budget spent on
#: text the model will not use.
MAX_INSTRUCTION_LENGTH = 2000


class IntegrationAIController(Controller):
    """Drafting a workflow from a sentence, and saving the draft a person accepted."""

    path = "/integrations/ai"
    dependencies = {"user": require_auth}

    @post("/generate")
    async def generate(
        self, request: Request, db: AsyncSession, user: User
    ) -> Template:
        """
        A sentence in, a draft out — or a refusal naming what is missing.

        **Saves nothing.** The drawing comes back in a hidden field on the partial, and
        Save is a separate request. A generated workflow is always a draft a human
        publishes.

        Every failure renders into the panel rather than raising, because the canvas
        underneath holds work that is stored nowhere else. That includes a provider being
        unreachable: "the AI is not answering" belongs inside the div somebody just opened,
        not in place of the page they were drawing on.
        """
        form = await request.form()
        instruction = str(form.get("instruction") or "").strip()

        if not instruction:
            return _refusal("Say what you want the workflow to do.")

        if len(instruction) > MAX_INSTRUCTION_LENGTH:
            return _refusal(
                f"That description is longer than {MAX_INSTRUCTION_LENGTH} characters. "
                "A sentence or two is enough — the connections you have are already listed "
                "for the model."
            )

        use_inbuilt = str(form.get("use_inbuilt_llm") or "").strip().lower() in (
            "true", "on", "1", "yes"
        )

        try:
            # `record_turn` or the spend is invisible. See the module docstring.
            with record_turn():
                draft = await workflow_author.draft_workflow(
                    db, user.id, instruction, use_inbuilt_llm=use_inbuilt
                )
        except DraftRefused as refusal:
            return _refusal(
                str(refusal),
                problems=refusal.problems,
                alternatives=refusal.alternatives,
            )
        except HTTPException as exc:
            # A provider that is not configured, or one that answered with an error. The
            # analytics service has already turned it into a sentence for a person.
            return _refusal(str(exc.detail))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Workflow generation failed for user %s", user.id)
            return _refusal(
                "The AI could not be reached just now. The canvas still works — you can "
                "build this by hand, or try again in a moment."
            )

        return Template(
            template_name=_DRAFT_TEMPLATE,
            context={
                "draft": {
                    "name": draft.name,
                    "description": draft.description,
                    "assumptions": draft.assumptions,
                    "warnings": draft.warnings,
                    "node_count": draft.node_count,
                },
                "graph_data_json": _json(draft.graph_data),
                "instruction": instruction,
            },
        )

    @post("/save-draft")
    async def save_draft(
        self, request: Request, db: AsyncSession, user: User
    ) -> Template:
        """
        Store a draft somebody read and accepted.

        Goes through ``flow_service.create_flow`` like every other new workflow, so the
        name uniqueness, the drawing validation and ``is_active = False`` are the same rules
        — a generated workflow is not created by a second path with second rules.

        ``created_by_ai`` is recorded, and that is the only thing about this row that
        differs. It earns the badge on the list page: somebody looking at a workflow that
        fires at 3am is entitled to know a model drafted it.
        """
        form = await request.form()

        try:
            graph_data = _parsed(str(form.get("graph_data") or ""))
        except ValueError:
            return _refusal(
                "That draft could not be read. Generate it again and save from the new one."
            )

        try:
            payload = FlowCreateRequest.parse(
                {
                    "name": str(form.get("name") or "").strip(),
                    "description": str(form.get("description") or "").strip(),
                }
            )
            flow = await flow_service.create_flow(
                db,
                user.id,
                payload.name,
                description=payload.description,
                graph_data=graph_data,
                created_by_ai=True,
            )
        except FlowValidationError as exc:
            # A drawing that resolved when it was generated and does not now — a connection
            # deleted in between, most likely. Named rather than swallowed.
            return _refusal(str(exc))
        except HTTPException as exc:
            return _refusal(str(exc.detail))

        return Template(
            template_name=_DRAFT_TEMPLATE,
            context={
                "saved": {
                    "uuid": str(flow.uuid),
                    "name": flow.name,
                    "canvas_url": f"/integrations/{flow.uuid}/canvas",
                },
            },
        )


def _refusal(
    message: str,
    *,
    problems: Optional[list] = None,
    alternatives: Optional[list] = None,
) -> Template:
    """
    A refusal rendered into the panel.

    ``alternatives`` is what makes it useful: "there is no 'Shopify Prod'" leaves somebody
    guessing, and the same sentence followed by the three connection names they do have is
    one they can act on.
    """
    return Template(
        template_name=_ERROR_TEMPLATE,
        context={
            "error": message,
            "problems": problems or [],
            "alternatives": alternatives or [],
        },
    )


def _json(value) -> str:  # noqa: ANN001
    import json

    return json.dumps(value, default=str)


def _parsed(raw: str):  # noqa: ANN201
    """
    The drawing out of the hidden field, as an object.

    Refused rather than defaulted to ``{}``: an empty drawing would be saved as a workflow
    with no steps, and the person who pressed Save on a draft they had just read would get
    a blank canvas with no explanation.
    """
    import json

    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("not an object")
    return parsed

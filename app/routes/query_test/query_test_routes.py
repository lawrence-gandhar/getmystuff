"""
HTTP layer for Test Query — one endpoint, shared by the two panels that hold an
unsaved query.

The Tool Configs form and the Ask AI panel both post the form as it currently stands
(``hx-include``) and both swap the same partial into a target of their own. One
endpoint rather than one per panel: the question is identical — *will the database
run this* — and the answer has to be identical too, or a query could pass the test in
one panel and fail in the other.

There is no error branch here. ``query_test_service.test_query`` answers every
outcome as a payload with ``passed`` false, because a query the database refuses is
what this endpoint was asked to find out, not a fault in the request.
"""

from litestar import Controller, post
from litestar.connection import Request
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.schemas.query_test import QueryTestRequest, QueryTestResponse
from app.services.query_test import query_test_service

_RESULT_TEMPLATE = "query_test/partials/result.htm"


class QueryTestController(Controller):
    """Run an unsaved query once, against the user's own datasource."""

    path = "/query-test"
    dependencies = {"user": require_auth}

    @post("/")
    async def run(self, request: Request, db: AsyncSession, user: User) -> Template:
        """
        Test the query the open form is holding and render the verdict.

        A POST because the payload carries a whole query — too long for a query
        string — and because reading it re-reflects the datasource. Nothing is
        written: the statement is read-only by construction and only
        ``query_executor.PROBE_ROWS`` rows are fetched.
        """
        payload = await QueryTestRequest.from_form(request)

        result = await query_test_service.test_query(
            db,
            user.id,
            datasource_id=payload.datasource_id,
            table_names=payload.table_names,
            query_mode=payload.query_mode,
            config=payload.config_json,
            sql_query=payload.sql_query,
            children=payload.children_json,
            sql_params=payload.sql_params_json,
            test_values=payload.test_values_json,
        )

        return Template(
            template_name=_RESULT_TEMPLATE,
            context=QueryTestResponse.build(result).payload(),
        )

"""
Tests for main.py — app assembly, the exception handler, and lifecycle hooks.

main.app itself is never served here (its on_startup touches the real database
and the network). The exception handler is exercised through a real test client
carrying purpose-built routes that raise: it depends on a fully-formed Litestar
request, so hand-rolled fakes cannot reach the code that matters.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from litestar import get
from litestar.exceptions import HTTPException
from litestar.status_codes import HTTP_500_INTERNAL_SERVER_ERROR

import main


@get("/boom/{status_code:int}", sync_to_thread=False)
def boom(status_code: int) -> None:
    raise HTTPException(status_code=status_code, detail="Deliberate test failure")


class TestHttpExceptionHandler:
    def test_401_redirects_a_browser_to_the_login_page(self, client_factory) -> None:
        with client_factory(boom) as client:
            response = client.get("/boom/401", follow_redirects=False)

        assert response.status_code in (301, 302, 307)
        assert response.headers["location"] == "/auth/login"

    def test_401_on_an_htmx_request_uses_hx_redirect(self, client_factory) -> None:
        """
        A plain redirect would swap the whole login page into whatever element
        issued the request, so HTMX gets a 200 plus HX-Redirect and the browser
        performs a real navigation.
        """
        with client_factory(boom) as client:
            response = client.get(
                "/boom/401", headers={"HX-Request": "true"}, follow_redirects=False
            )

        assert response.status_code == 200
        assert response.headers["HX-Redirect"] == "/auth/login"
        assert response.text == ""

    @pytest.mark.parametrize("status_code", [403, 404, 409, 422])
    def test_other_statuses_keep_their_status_code(self, client_factory, status_code) -> None:
        """A 404 must render as a 404, not get swallowed into a 500."""
        with client_factory(boom) as client:
            response = client.get(f"/boom/{status_code}", follow_redirects=False)

        assert response.status_code == status_code

    def test_the_detail_reaches_the_client(self, client_factory) -> None:
        with client_factory(boom) as client:
            response = client.get("/boom/404", follow_redirects=False)

        assert "Deliberate test failure" in response.text

    def test_an_unrouted_path_is_a_404_not_a_500(self, client_factory) -> None:
        """
        Regression guard: the handler must RETURN rather than re-raise. Raising
        escapes the exception middleware, so uvicorn reports an uncaught ASGI
        error and the client gets a 500 — which is how a plain 404 once did.
        """
        with client_factory(boom) as client:
            response = client.get("/no/such/path", follow_redirects=False)

        assert response.status_code == 404

    def test_a_500_is_still_a_500(self, client_factory) -> None:
        with client_factory(boom) as client:
            response = client.get(
                f"/boom/{HTTP_500_INTERNAL_SERVER_ERROR}", follow_redirects=False
            )

        assert response.status_code == HTTP_500_INTERNAL_SERVER_ERROR


class TestLifecycleHooks:
    async def test_startup_creates_tables_seeds_a_user_and_preloads_models(self) -> None:
        with (
            patch.object(main, "engine") as engine,
            patch.object(main, "create_fake_user", new=AsyncMock()) as seed,
            patch.object(main.ollama_client, "preload_models", new=AsyncMock()) as preload,
        ):
            engine.begin.return_value.__aenter__.return_value = AsyncMock()
            await main.on_startup()

        seed.assert_awaited_once()
        preload.assert_awaited_once()

    async def test_shutdown_releases_the_pooled_http_client(self) -> None:
        with patch.object(main.ollama_client, "close_client", new=AsyncMock()) as close:
            await main.on_shutdown()
        close.assert_awaited_once()


class TestAppAssembly:
    # One representative path per registered controller. A controller that is
    # imported but never added to route_handlers is a whole feature silently
    # missing from the running app, and nothing else would catch it.
    EXPECTED_PATHS = [
        "/",
        "/auth/login",
        "/user/dashboard",
        "/datasource",
        "/datasource/configurations",
        "/query-runner",
        "/ai-settings",
        "/chatbot-settings",
        "/actions",
        "/chatbot-analytics",
        "/flow-builder",
        "/workspaces",
        "/data-agents",
        "/tool-configs",
        "/sql-assist/form",
        "/deep-agents/agent-options",
        "/public/chatbot/message",
    ]

    def test_every_controller_is_registered(self) -> None:
        registered = {route.path for route in main.app.routes}
        missing = [path for path in self.EXPECTED_PATHS if path not in registered]
        assert not missing, f"routes not registered: {missing}"

    def test_the_db_dependency_is_provided_app_wide(self) -> None:
        assert "db" in main.app.dependencies

    def test_the_session_middleware_is_installed(self) -> None:
        assert main.app.middleware

    def test_the_http_exception_handler_is_wired_up(self) -> None:
        assert HTTPException in main.app.exception_handlers

    def test_the_htmx_request_class_is_used(self) -> None:
        """Handlers read request.headers['HX-Request'] via the HTMX request type."""
        from litestar.plugins.htmx import HTMXRequest

        assert main.app.request_class is HTMXRequest

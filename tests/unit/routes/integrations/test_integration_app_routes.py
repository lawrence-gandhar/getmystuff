"""
Tests for ``app/routes/integrations/app_routes.py`` — the Apps gallery.

Four properties carry the suite.

**The connector comes from the path and cannot be talked out of it.** A form posting a
different ``connector_id`` than the tile it was opened from must create the tile's
connector or nothing: the override is what stops a Brevo dialog storing a credential under
generic REST's rules, where a typed base URL is allowed.

**A credential never comes back.** Asserted by searching the gallery, the dialog and the
connect response for the key that was posted, rather than by checking that no *named* field
holds it — a named check passes the day somebody adds a differently-named field.

**A refusal keeps the dialog open.** Every failure here is a 200 carrying the sentence and
no ``data-success`` marker, because the marker is what the form reads to decide whether to
close. A 400 would replace the page holding what somebody typed.

**"Connected" is not "there is a row".** A connection whose credential was revoked still
has one, and a tile calling that working would tell somebody their sync is fine while it
fails every night.
"""

from __future__ import annotations

import pytest

from app.routes.integrations import IntegrationAppController
from app.services.integrations import connection_service

SECRET = "xkeysib-app-route-test-value"


@pytest.fixture
def client(auth_client_factory):  # noqa: ANN001, ANN201
    return auth_client_factory(IntegrationAppController)


def connect(client, connector_id: str = "brevo", **fields):  # noqa: ANN001, ANN201
    data = {"label": f"{connector_id} account", "api_key": SECRET}
    data.update(fields)
    return client.post(f"/integrations/apps/{connector_id}/connect", data=data)


class TestTheGallery:
    async def test_it_lists_every_connector_in_the_build(self, client) -> None:
        response = client.get("/integrations/apps/")

        assert response.status_code == 200
        for label in ("Brevo", "Shopify", "REST API"):
            assert label in response.text

    async def test_an_app_with_no_connection_says_so(self, client) -> None:
        response = client.get("/integrations/apps/")

        assert "Not connected" in response.text

    async def test_it_shows_what_a_connected_app_has(self, client) -> None:
        connect(client, "brevo")

        response = client.get("/integrations/apps/")

        assert "1 working" in response.text

    async def test_a_revoked_connection_is_not_counted_as_working(
        self, db, client, user
    ) -> None:
        """
        The claim the three counts exist for. A revoked credential leaves the row behind —
        workflows point at it by uuid — so a page reading "connected" off the row's
        existence would say a sync is fine while every run of it fails.
        """
        connect(client, "brevo")
        connection = (await connection_service.list_connections(db, user.id))[0]
        await connection_service.revoke_connection(
            db, user.id, connection.uuid, reason="test"
        )

        apps = await connection_service.list_apps(db, user.id)
        brevo = next(app for app in apps if app["connector_id"] == "brevo")

        assert brevo["connection_count"] == 1
        assert brevo["ready_count"] == 0
        assert brevo["attention_count"] == 1

    async def test_a_switched_off_connection_is_its_own_bucket(
        self, db, client, user
    ) -> None:
        """
        Neither working nor wanting attention. Parking a connection is a decision, and a
        tile that counted it as a problem would train somebody to ignore the badge that
        means a token was revoked.
        """
        connect(client, "brevo")
        connection = (await connection_service.list_connections(db, user.id))[0]
        await connection_service.set_connection_active(
            db, user.id, connection.uuid, False
        )

        apps = await connection_service.list_apps(db, user.id)
        brevo = next(app for app in apps if app["connector_id"] == "brevo")

        assert brevo["paused_count"] == 1
        assert brevo["attention_count"] == 0
        assert brevo["ready_count"] == 0

    async def test_the_buckets_add_up_to_the_connection_count(
        self, db, client, user
    ) -> None:
        connect(client, "brevo", label="Working")
        connect(client, "brevo", label="Parked")
        connect(client, "brevo", label="Revoked")

        connections = await connection_service.list_connections(db, user.id)
        by_label = {c.label: c for c in connections}
        await connection_service.set_connection_active(
            db, user.id, by_label["Parked"].uuid, False
        )
        await connection_service.revoke_connection(
            db, user.id, by_label["Revoked"].uuid, reason="test"
        )

        apps = await connection_service.list_apps(db, user.id)
        brevo = next(app for app in apps if app["connector_id"] == "brevo")

        assert (
            brevo["ready_count"] + brevo["attention_count"] + brevo["paused_count"]
            == brevo["connection_count"]
            == 3
        )
        assert (brevo["ready_count"], brevo["attention_count"], brevo["paused_count"]) == (
            1,
            1,
            1,
        )

    async def test_another_users_connections_are_not_counted(
        self, db, client, make_user
    ) -> None:
        other = await make_user(email="other@example.com")
        await connection_service.create_connection(
            db, other.id,
            connector_id="brevo", label="Someone else's Brevo", api_key=SECRET,
        )

        response = client.get("/integrations/apps/")

        assert "Not connected" in response.text
        assert "Someone else's Brevo" not in response.text


class TestTheConnectDialog:
    async def test_an_app_that_computes_its_address_asks_for_neither(
        self, client
    ) -> None:
        """Brevo is one API at one hostname for every account: a name and a key, and no
        third question to get wrong."""
        response = client.get("/integrations/apps/brevo/connect-form")

        assert response.status_code == 200
        assert 'name="api_key"' in response.text
        assert 'name="base_url"' not in response.text
        assert 'name="external_account_id"' not in response.text

    async def test_an_app_with_an_account_asks_for_it_with_its_own_pattern(
        self, client
    ) -> None:
        from app.services.integrations.connectors.shopify.hooks import SHOP_DOMAIN_PATTERN

        response = client.get("/integrations/apps/shopify/connect-form")

        assert 'name="external_account_id"' in response.text
        assert SHOP_DOMAIN_PATTERN in response.text
        assert 'name="base_url"' not in response.text

    async def test_an_app_that_is_told_its_address_asks_for_one(self, client) -> None:
        response = client.get("/integrations/apps/rest_generic/connect-form")

        assert 'name="base_url"' in response.text
        assert 'name="external_account_id"' not in response.text

    async def test_an_unknown_app_is_a_sentence_inside_the_dialog(
        self, client
    ) -> None:
        """Rendered into the dialog somebody just opened rather than as an error page: the
        registry's own sentence already names what is available."""
        response = client.get("/integrations/apps/not_a_connector/connect-form")

        assert response.status_code == 200
        assert "not available in this version" in response.text
        assert "data-success" not in response.text


class TestConnecting:
    async def test_it_creates_a_connection_to_that_app(self, db, client, user) -> None:
        response = connect(client, "brevo", label="Marketing")

        assert response.status_code == 200
        assert 'data-success="true"' in response.text

        connections = await connection_service.list_connections(db, user.id)
        assert [(c.label, c.connector_id) for c in connections] == [
            ("Marketing", "brevo")
        ]

    async def test_the_key_never_comes_back(self, client) -> None:
        """**Searched, not reviewed.** See the module docstring."""
        created = connect(client, "brevo")
        gallery = client.get("/integrations/apps/")
        dialog = client.get("/integrations/apps/brevo/connect-form")

        for response in (created, gallery, dialog):
            assert SECRET not in response.text

    async def test_the_path_decides_the_connector_not_the_form(
        self, db, client, user
    ) -> None:
        """
        The security property in the module docstring. A posted ``connector_id`` reaching
        the service would let the Brevo dialog — which asks for no address — store a
        credential under generic REST's rules, where a typed one is allowed.
        """
        # Posted straight rather than through the helper: `connector_id` here is a *form
        # field*, deliberately disagreeing with the path segment.
        client.post(
            "/integrations/apps/brevo/connect",
            data={
                "label": "Sneaky",
                "api_key": SECRET,
                "connector_id": "rest_generic",
                "base_url": "https://attacker.example.com",
            },
        )

        connections = await connection_service.list_connections(db, user.id)

        assert [c.connector_id for c in connections] == ["brevo"]
        assert connections[0].base_url is None

    async def test_the_tiles_come_back_with_the_new_count(self, client) -> None:
        """Out-of-band, because the tile behind the dialog is now wrong — and a tile still
        reading "Not connected" is how the same credential gets added twice."""
        response = connect(client, "brevo")

        assert 'id="integrationAppCards"' in response.text
        assert 'hx-swap-oob="true"' in response.text
        assert "1 working" in response.text

    async def test_a_refusal_is_a_200_with_no_success_marker(self, client) -> None:
        response = connect(client, "shopify", external_account_id="evil.example.com")

        assert response.status_code == 200
        assert 'data-success="true"' not in response.text
        assert "shop domain" in response.text.lower()

    async def test_nothing_is_written_when_it_is_refused(
        self, db, client, user
    ) -> None:
        connect(client, "shopify", external_account_id="evil.example.com")

        assert await connection_service.list_connections(db, user.id) == []

    async def test_a_missing_name_is_refused_with_a_sentence(self, client) -> None:
        response = connect(client, "brevo", label="")

        assert response.status_code == 200
        assert 'data-success="true"' not in response.text

    async def test_connecting_an_unknown_app_creates_nothing(
        self, db, client, user
    ) -> None:
        response = connect(client, "not_a_connector")

        assert response.status_code == 200
        assert 'data-success="true"' not in response.text
        assert await connection_service.list_connections(db, user.id) == []

    async def test_the_same_app_can_be_connected_twice(self, db, client, user) -> None:
        """Three Shopify stores and forty locations are the ordinary case. Brevo has no
        account id, so nothing makes a second connection a duplicate."""
        connect(client, "brevo", label="Marketing")
        connect(client, "brevo", label="Support")

        assert len(await connection_service.list_connections(db, user.id)) == 2

    async def test_the_second_connection_to_one_shopify_store_is_refused(
        self, client
    ) -> None:
        connect(client, "shopify", label="EU", external_account_id="demo.myshopify.com")
        response = connect(
            client, "shopify", label="EU again", external_account_id="demo.myshopify.com"
        )

        assert 'data-success="true"' not in response.text
        assert "EU" in response.text


class TestAuthentication:
    async def test_the_gallery_needs_a_session(self, client_factory) -> None:  # noqa: ANN001
        anonymous = client_factory(IntegrationAppController)

        response = anonymous.get("/integrations/apps/", follow_redirects=False)

        assert response.status_code in (302, 303, 307, 401)

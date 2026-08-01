"""
Contract tests covering every mapped model at once.

CLAUDE.md fixes one identifier rule for the whole codebase: ``id`` is a
BigInteger primary key used only internally and for foreign keys, and ``uuid``
is the sole public identifier that may appear in a URL, template or JSON
payload. That rule is invisible to a per-model test — a new model can forget it
and every existing test still passes — so it is asserted here across
``Base.metadata`` as a whole, which means a model added tomorrow is checked the
day it lands.

This module also imports ``app/models/ai_analytics/prompt_configurations.py``.
That file is empty, and nothing else in the application imports it, so coverage
could not see it at all: it was absent from the report rather than listed at 0%
(see documentations/TESTING.md). Importing it here is what makes it measurable.
"""

from __future__ import annotations

import importlib
import importlib.util
import uuid as uuid_pkg

import pytest
from sqlalchemy import BigInteger, inspect

import app.db.models  # noqa: F401  (populates Base.metadata)
from app.db.base import Base

# The registry of every model the application actually maps.
MAPPED_MODELS = [m.class_ for m in Base.registry.mappers]
MODEL_IDS = [m.__name__ for m in MAPPED_MODELS]


class TestEmptyModule:
    def test_prompt_configurations_is_importable_and_empty(self) -> None:
        """
        Pinned as a finding. The module is zero bytes — a placeholder that was
        never written. Importing it is harmless and makes coverage measure it,
        but it defines nothing, so if a PromptConfiguration model is ever meant
        to exist this test is where its absence is recorded.
        """
        module = importlib.import_module("app.models.ai_analytics.prompt_configurations")

        public_names = [n for n in vars(module) if not n.startswith("__")]
        assert public_names == []


class TestSubscriptionsModuleIsGone:
    """
    Regression test for a fixed defect.

    ``app/models/subscriptions/`` held a second ``UserSubscription`` mapped to
    the same ``user_subscriptions`` table as the one in ``models/user/user.py``,
    and could not even be imported: it contained

        is_active = Mapped[bool] = mapped_column(Boolean, default=True)

    which Python parses as a chained assignment, attempting item assignment on a
    type object and raising ``TypeError`` at import. It was unreferenced dead
    code and has been deleted.
    """

    def test_the_package_no_longer_exists(self) -> None:
        assert importlib.util.find_spec("app.models.subscriptions") is None

    def test_the_live_user_subscription_model_is_the_one_in_models_user(self) -> None:
        """The table is mapped exactly once, by the module that survived."""
        from app.models.user.user import UserSubscription

        assert UserSubscription.__tablename__ == "user_subscriptions"
        assert UserSubscription.__table__ is Base.metadata.tables["user_subscriptions"]


class TestIdentifierContract:
    def test_the_registry_is_not_empty(self) -> None:
        """Guards the rest of this module: if the import above stopped
        populating the registry every other test here would vacuously pass."""
        assert len(MAPPED_MODELS) > 15

    @pytest.mark.parametrize("model", MAPPED_MODELS, ids=MODEL_IDS)
    def test_primary_key_is_a_single_bigint_column_named_id(self, model) -> None:  # noqa: ANN001
        primary_key = list(model.__table__.primary_key.columns)

        assert len(primary_key) == 1, f"{model.__name__} has a composite primary key"
        assert primary_key[0].name == "id"
        assert isinstance(primary_key[0].type, BigInteger)
        assert primary_key[0].autoincrement is True

    @pytest.mark.parametrize("model", MAPPED_MODELS, ids=MODEL_IDS)
    def test_has_a_unique_indexed_uuid_column(self, model) -> None:  # noqa: ANN001
        """The public identifier. Unique because it is looked up by
        ``get_by_uuid``, indexed because that lookup is on the hot path of every
        route that takes a ``:uuid`` path param."""
        columns = model.__table__.columns

        assert "uuid" in columns, f"{model.__name__} has no public uuid column"
        column = columns["uuid"]
        assert column.unique is True
        assert column.index is True
        assert column.nullable is False

    @pytest.mark.parametrize("model", MAPPED_MODELS, ids=MODEL_IDS)
    def test_uuid_defaults_are_generated_client_side(self, model) -> None:  # noqa: ANN001
        """
        A caller never supplies the uuid, so the model must default it —
        otherwise every insert would fail the NOT NULL constraint.

        The default is asserted by calling it rather than by identity against
        ``uuid.uuid4``: the ``uuid`` module ends up loaded twice in this
        process (the app package and the test import resolve to distinct module
        objects), so an identity check fails despite both being the real
        ``uuid4``. Behaviour is the property that actually matters here.
        """
        default = model.__table__.columns["uuid"].default

        assert default is not None, f"{model.__name__}.uuid has no default"
        assert default.is_callable, f"{model.__name__}.uuid default is a constant"
        assert default.arg.__name__ == "uuid4"

        produced = {default.arg({}) for _ in range(5)}
        assert all(isinstance(v, uuid_pkg.UUID) for v in produced)
        assert len(produced) == 5, "uuid default is not generating unique values"

    @pytest.mark.parametrize("model", MAPPED_MODELS, ids=MODEL_IDS)
    def test_every_foreign_key_targets_a_bigint_id_not_a_uuid(self, model) -> None:  # noqa: ANN001
        """Joins go through the internal bigint. A FK pointing at ``uuid``
        would silently work but cost a wider index on every join."""
        for column in model.__table__.columns:
            for fk in column.foreign_keys:
                assert fk.column.name == "id", (
                    f"{model.__name__}.{column.name} references "
                    f"{fk.column.table.name}.{fk.column.name}, not id"
                )


class TestSchemaCreates:
    async def test_every_table_is_created_by_the_test_fixture(self, db_engine) -> None:  # noqa: ANN001
        """
        Proves the four SQLite @compiles shims in conftest still cover every
        column type in use. A model introducing a fifth Postgres-only type
        would fail create_all, and this is where that shows up rather than as a
        confusing error in an unrelated test.
        """
        async with db_engine.connect() as conn:
            names = await conn.run_sync(lambda c: inspect(c).get_table_names())

        expected = set(Base.metadata.tables)
        assert expected.issubset(set(names))

    @pytest.mark.parametrize("model", MAPPED_MODELS, ids=MODEL_IDS)
    def test_tablename_is_set_explicitly(self, model) -> None:  # noqa: ANN001
        assert model.__tablename__
        assert model.__tablename__ == model.__tablename__.lower()

    def test_no_two_models_map_the_same_table(self) -> None:
        """The subscriptions duplicate above is exactly this failure; asserting
        it over the loaded registry stops a second one appearing silently."""
        names = [m.__tablename__ for m in MAPPED_MODELS]
        assert len(names) == len(set(names)), f"duplicate __tablename__: {names}"

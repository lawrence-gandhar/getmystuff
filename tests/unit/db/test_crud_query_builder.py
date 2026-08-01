"""
Tests for CRUDQueryBuilder (app/db/db_utils.py).

The highest-leverage target in the repo: 27 modules instantiate one of these,
and every record-level read and write in the application goes through it. A bug
here is a bug everywhere.

``Workspace`` is used as the subject model because it is small and has the
standard id/uuid/user_id/name shape that CLAUDE.md mandates for every model.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from sqlalchemy import func
from sqlalchemy.exc import DBAPIError, IntegrityError, MultipleResultsFound

from app.db.db_utils import CRUDQueryBuilder
from app.models.user.user import User
from app.models.workspaces.workspaces import Workspace


@pytest.fixture
def crud() -> CRUDQueryBuilder:
    return CRUDQueryBuilder(Workspace)


@pytest.fixture
def make_workspace(db, crud, user: User):
    async def _make(name: str, *, owner: User | None = None, **extra) -> Workspace:
        return await crud.create(
            db,
            {
                "uuid": uuid_pkg.uuid4(),
                "user_id": (owner or user).id,
                "name": name,
                **extra,
            },
        )

    return _make


class TestCreate:
    async def test_persists_and_populates_the_primary_key(self, db, crud, user) -> None:
        record = await crud.create(
            db, {"uuid": uuid_pkg.uuid4(), "user_id": user.id, "name": "Finance"}
        )
        assert record.id is not None
        assert record.name == "Finance"

    async def test_the_record_is_readable_afterwards(self, db, crud, make_workspace) -> None:
        created = await make_workspace("Finance")
        assert await crud.get_one(db, {"id": created.id}) is not None

    async def test_an_unknown_column_raises(self, db, crud, user) -> None:
        with pytest.raises(TypeError):
            await crud.create(db, {"user_id": user.id, "nonexistent_column": "x"})

    async def test_one_user_cannot_own_two_workspaces_of_the_same_name(
        self, db, crud, make_workspace
    ) -> None:
        """Enforced by the uq_workspace_user_name_lower index, not by the service."""
        await make_workspace("Finance")
        with pytest.raises(IntegrityError):
            await make_workspace("Finance")

    async def test_two_users_may_each_own_the_same_name(
        self, db, crud, make_workspace, make_user
    ) -> None:
        await make_workspace("Finance")
        other = await make_user("other@example.com")
        assert (await make_workspace("Finance", owner=other)).id is not None


class TestGetOne:
    async def test_returns_none_when_nothing_matches(self, db, crud) -> None:
        assert await crud.get_one(db, {"name": "missing"}) is None

    async def test_finds_by_a_single_filter(self, db, crud, make_workspace) -> None:
        await make_workspace("Finance")
        found = await crud.get_one(db, {"name": "Finance"})
        assert found is not None and found.name == "Finance"

    async def test_filters_are_combined_with_and(self, db, crud, make_workspace, user) -> None:
        await make_workspace("Finance")
        assert await crud.get_one(db, {"name": "Finance", "user_id": user.id}) is not None
        assert await crud.get_one(db, {"name": "Finance", "user_id": user.id + 999}) is None

    async def test_no_filters_returns_the_only_row(self, db, crud, make_workspace) -> None:
        await make_workspace("Finance")
        assert (await crud.get_one(db)).name == "Finance"

    async def test_multiple_matches_raise_rather_than_pick_one(
        self, db, crud, make_workspace, make_user
    ) -> None:
        """
        scalar_one_or_none surfaces an ambiguous filter instead of quietly
        returning the first row. Two users may each own a workspace of the same
        name, so filtering on name alone is genuinely ambiguous.
        """
        await make_workspace("Dup")
        other = await make_user("other@example.com")
        await make_workspace("Dup", owner=other)

        with pytest.raises(MultipleResultsFound):
            await crud.get_one(db, {"name": "Dup"})


class TestGetByUuid:
    async def test_resolves_by_the_public_identifier(self, db, crud, make_workspace) -> None:
        created = await make_workspace("Finance")
        found = await crud.get_by_uuid(db, created.uuid)
        assert found is not None and found.id == created.id

    async def test_unknown_uuid_returns_none(self, db, crud) -> None:
        assert await crud.get_by_uuid(db, uuid_pkg.uuid4()) is None

    async def test_extra_filters_scope_the_lookup_to_the_owner(
        self, db, crud, make_workspace, make_user
    ) -> None:
        """
        The ownership check that stops one user reading another's row by guessing
        a uuid. Every service relies on this.
        """
        created = await make_workspace("Finance")
        intruder = await make_user("intruder@example.com")

        assert await crud.get_by_uuid(db, created.uuid, {"user_id": intruder.id}) is None
        assert await crud.get_by_uuid(db, created.uuid, {"user_id": created.user_id}) is not None


class TestGetMany:
    async def test_empty_table_yields_empty_list(self, db, crud) -> None:
        assert await crud.get_many(db) == []

    async def test_returns_every_matching_row(self, db, crud, make_workspace) -> None:
        for name in ("A", "B", "C"):
            await make_workspace(name)
        assert len(await crud.get_many(db)) == 3

    async def test_filters_narrow_the_result(self, db, crud, make_workspace, make_user) -> None:
        await make_workspace("Mine")
        other = await make_user("other@example.com")
        await make_workspace("Theirs", owner=other)

        rows = await crud.get_many(db, {"user_id": other.id})
        assert [row.name for row in rows] == ["Theirs"]

    async def test_order_by_ascending(self, db, crud, make_workspace) -> None:
        for name in ("C", "A", "B"):
            await make_workspace(name)
        rows = await crud.get_many(db, order_by="name")
        assert [row.name for row in rows] == ["A", "B", "C"]

    async def test_order_by_descending(self, db, crud, make_workspace) -> None:
        for name in ("C", "A", "B"):
            await make_workspace(name)
        rows = await crud.get_many(db, order_by="name", desc=True)
        assert [row.name for row in rows] == ["C", "B", "A"]

    async def test_limit_caps_the_result(self, db, crud, make_workspace) -> None:
        for name in ("A", "B", "C"):
            await make_workspace(name)
        assert len(await crud.get_many(db, order_by="name", limit=2)) == 2

    async def test_skip_offsets_the_result(self, db, crud, make_workspace) -> None:
        for name in ("A", "B", "C"):
            await make_workspace(name)
        rows = await crud.get_many(db, order_by="name", skip=1)
        assert [row.name for row in rows] == ["B", "C"]

    async def test_skip_and_limit_paginate(self, db, crud, make_workspace) -> None:
        for name in ("A", "B", "C", "D"):
            await make_workspace(name)
        rows = await crud.get_many(db, order_by="name", skip=1, limit=2)
        assert [row.name for row in rows] == ["B", "C"]

    async def test_limit_zero_is_treated_as_no_limit(self, db, crud, make_workspace) -> None:
        """`if limit:` means 0 is falsy — documents the real behaviour, not a wish."""
        for name in ("A", "B", "C"):
            await make_workspace(name)
        assert len(await crud.get_many(db, limit=0)) == 3


class TestUpdate:
    async def test_updates_the_named_columns(self, db, crud, make_workspace) -> None:
        created = await make_workspace("Old")
        updated = await crud.update(db, created.id, {"name": "New"})
        assert updated is not None and updated.name == "New"

    async def test_the_change_is_persisted(self, db, crud, make_workspace) -> None:
        created = await make_workspace("Old")
        await crud.update(db, created.id, {"name": "New"})
        assert (await crud.get_one(db, {"id": created.id})).name == "New"

    async def test_unknown_id_returns_none(self, db, crud) -> None:
        assert await crud.update(db, 999_999, {"name": "New"}) is None

    async def test_update_takes_the_bigint_pk_not_the_uuid(
        self, db, crud, make_workspace
    ) -> None:
        """
        Documents the asymmetry services must respect: get_by_uuid() takes the
        public uuid, update()/delete() take the internal bigint id.

        Passing a uuid raises from the driver rather than returning None — the
        mistake is loud, which is the good outcome. It is asserted here so the
        contract is pinned rather than discovered in production.
        """
        created = await make_workspace("Old")
        with pytest.raises(DBAPIError):
            await crud.update(db, created.uuid, {"name": "New"})

    async def test_empty_data_is_a_no_op(self, db, crud, make_workspace) -> None:
        created = await make_workspace("Same")
        updated = await crud.update(db, created.id, {})
        assert updated is not None and updated.name == "Same"


class TestDelete:
    async def test_returns_true_and_removes_the_row(self, db, crud, make_workspace) -> None:
        created = await make_workspace("Doomed")
        assert await crud.delete(db, created.id) is True
        assert await crud.get_one(db, {"id": created.id}) is None

    async def test_unknown_id_returns_false(self, db, crud) -> None:
        assert await crud.delete(db, 999_999) is False

    async def test_deleting_twice_returns_false_the_second_time(
        self, db, crud, make_workspace
    ) -> None:
        created = await make_workspace("Doomed")
        assert await crud.delete(db, created.id) is True
        assert await crud.delete(db, created.id) is False


class TestCount:
    async def test_empty_table_counts_zero(self, db, crud) -> None:
        assert await crud.count(db) == 0

    async def test_counts_every_row(self, db, crud, make_workspace) -> None:
        for name in ("A", "B", "C"):
            await make_workspace(name)
        assert await crud.count(db) == 3

    async def test_counts_only_matching_rows(self, db, crud, make_workspace, make_user) -> None:
        await make_workspace("Mine")
        other = await make_user("other@example.com")
        await make_workspace("Theirs", owner=other)
        assert await crud.count(db, {"user_id": other.id}) == 1

    async def test_non_matching_filter_counts_zero(self, db, crud, make_workspace) -> None:
        await make_workspace("A")
        assert await crud.count(db, {"name": "nope"}) == 0


class TestAggregate:
    async def test_counts_rows(self, db, crud, make_workspace) -> None:
        for name in ("A", "B", "C"):
            await make_workspace(name)
        result = await crud.aggregate(db, {"total": func.count(Workspace.id)})
        assert result == {"total": 3}

    async def test_applies_filters(self, db, crud, make_workspace, make_user) -> None:
        await make_workspace("Mine")
        other = await make_user("other@example.com")
        await make_workspace("Theirs", owner=other)
        result = await crud.aggregate(
            db, {"total": func.count(Workspace.id)}, {"user_id": other.id}
        )
        assert result == {"total": 1}

    async def test_multiple_aggregations_keep_their_keys(self, db, crud, make_workspace) -> None:
        for name in ("A", "B"):
            await make_workspace(name)
        result = await crud.aggregate(
            db,
            {"total": func.count(Workspace.id), "largest": func.max(Workspace.id)},
        )
        assert result["total"] == 2
        assert result["largest"] is not None

    async def test_aggregating_an_empty_table_yields_null_not_an_error(
        self, db, crud
    ) -> None:
        result = await crud.aggregate(db, {"largest": func.max(Workspace.id)})
        assert result == {"largest": None}

"""
Tests for app/services/datasource/datasource_service.py.

Every function here either reaches a user-supplied external database or reads
one's metadata, so the three ``metadata_service`` entry points
(``get_rdbms_tables``, ``get_mongo_collections``, ``get_table_schema``) and the
two connection testers are stubbed at the seam the service owns. The service's
own logic — validation, ownership, configuration_data assembly, the
active/inactive cascade — runs for real against the SQLite test database.

Two defects this module found are now fixed, and the tests that pinned them have
been inverted into regression tests:

* ``create_datasource`` ended in ``except Exception: print(...); return False``,
  which swallowed the HTTPExceptions raised inside its own ``try`` block — the
  carefully-worded "we connected but could not read any tables" message never
  reached the caller.
* ``uq_datasource_name_lower`` was a **global** unique index, so one tenant's
  datasource name blocked every other tenant's. It is now
  ``uq_datasource_user_name_lower`` and includes ``user_id``.
"""

from __future__ import annotations

import uuid as uuid_pkg
from pathlib import Path

import pytest
from litestar.exceptions import HTTPException

from app.models.datasource import DataSource, DatasourceFile
from app.services.datasource import datasource_service as svc
from app.utils.crypto import decrypt_password


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def make_datasource(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str, **kwargs):  # noqa: ANN001
        row = DataSource(
            user_id=owner.id,
            datasource_name=name,
            db_type=kwargs.pop("db_type", "postgres"),
            password_encrypted=kwargs.pop("password_encrypted", "enc"),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_file(db):  # noqa: ANN001, ANN201
    async def _make(datasource, path: Path, **kwargs):  # noqa: ANN001
        row = DatasourceFile(
            datasource_id=datasource.id,
            original_filename=path.name,
            stored_filename=path.name,
            normalized_base_name=path.stem,
            file_path=str(path),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
async def other_user(make_user):  # noqa: ANN001, ANN201
    return await make_user("intruder@example.com")


@pytest.fixture
def stub_metadata(monkeypatch: pytest.MonkeyPatch) -> dict:
    """
    Stub the three metadata_service calls the service imports by name.

    They are patched on ``datasource_service`` rather than on
    ``metadata_service``, because the service does ``from ... import
    get_rdbms_tables`` — patching the source module would not rebind the name
    the service actually calls.
    """
    state: dict = {
        "rdbms_tables": ["orders", "customers"],
        "mongo_collections": ["events"],
        "schema": [{"column": "id", "type": "integer"}, {"column": "name", "type": "text"}],
        "raise_on": None,
        "calls": [],
    }

    async def fake_rdbms_tables(datasource):  # noqa: ANN001
        state["calls"].append("rdbms_tables")
        if state["raise_on"] == "tables":
            raise RuntimeError("catalog unreadable")
        return list(state["rdbms_tables"])

    async def fake_mongo_collections(datasource):  # noqa: ANN001
        state["calls"].append("mongo_collections")
        if state["raise_on"] == "tables":
            raise RuntimeError("catalog unreadable")
        return list(state["mongo_collections"])

    async def fake_schema(datasource, table_name):  # noqa: ANN001
        state["calls"].append(("schema", table_name))
        if state["raise_on"] == "schema":
            raise RuntimeError("no permission")
        return list(state["schema"])

    monkeypatch.setattr(svc, "get_rdbms_tables", fake_rdbms_tables)
    monkeypatch.setattr(svc, "get_mongo_collections", fake_mongo_collections)
    monkeypatch.setattr(svc, "get_table_schema", fake_schema)
    return state


async def accept(*args, **kwargs) -> bool:  # noqa: ANN002, ANN003
    return True


async def reject(*args, **kwargs) -> bool:  # noqa: ANN002, ANN003
    return False


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------
class TestTestConnection:
    async def test_mongodb_goes_through_the_mongo_tester(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict = {}

        async def fake_mongo(uri: str, database: str) -> bool:
            seen.update({"uri": uri, "database": database})
            return True

        monkeypatch.setattr(svc, "test_mongo_connection", fake_mongo)

        assert await svc.test_connection("mongodb", "h", "27017", "shop", "u", "p") is True
        assert seen == {"uri": "mongodb://u:p@h:27017", "database": "shop"}

    @pytest.mark.parametrize("db_type", ["postgres", "mysql"])
    async def test_rdbms_goes_through_the_rdbms_tester(
        self, monkeypatch: pytest.MonkeyPatch, db_type: str
    ) -> None:
        seen: dict = {}

        async def fake_rdbms(url: str) -> bool:
            seen["url"] = url
            return True

        monkeypatch.setattr(svc, "test_rdbms_connection", fake_rdbms)

        assert await svc.test_connection(db_type, "h", "5432", "shop", "u", "p") is True
        assert "shop" in seen["url"]

    async def test_an_unsupported_dialect_becomes_a_readable_400(self) -> None:
        """``build_rdbms_url`` raises ValueError for Oracle; the service turns
        that into a message the UI can show rather than a 500."""
        with pytest.raises(HTTPException) as excinfo:
            await svc.test_connection("oracle", "h", "1521", "shop", "u", "p")

        assert excinfo.value.status_code == 400
        assert "Oracle is not yet supported" in excinfo.value.detail

    async def test_any_other_failure_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(url: str) -> bool:
            raise RuntimeError("network down")

        monkeypatch.setattr(svc, "test_rdbms_connection", boom)

        assert await svc.test_connection("postgres", "h", "5432", "s", "u", "p") is False


# ---------------------------------------------------------------------------
# create_datasource
# ---------------------------------------------------------------------------
class TestCreateDatasource:
    async def test_creates_a_connection_datasource_with_metadata(
        self, db, user, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await svc.create_datasource(
            db, user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "pw", accept
        )

        assert datasource.datasource_name == "sales_data"
        assert datasource.user_id == user.id
        assert set(datasource.configuration_data) == {"orders", "customers"}

    async def test_the_password_is_stored_encrypted(
        self, db, user, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await svc.create_datasource(
            db, user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "s3cret", accept
        )

        assert datasource.password_encrypted != "s3cret"
        assert decrypt_password(datasource.password_encrypted) == "s3cret"

    async def test_an_empty_password_is_stored_as_an_empty_string(
        self, db, user, stub_metadata  # noqa: ANN001
    ) -> None:
        """Not encrypted — ``encrypt_password("")`` is skipped entirely, so the
        column holds "" rather than the ciphertext of an empty string."""
        datasource = await svc.create_datasource(
            db, user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "", accept
        )

        assert datasource.password_encrypted == ""

    async def test_the_name_is_normalised_by_the_schema(
        self, db, user, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await svc.create_datasource(
            db, user.id, "  Sales_Data  ", "postgres", "h", "5432", "shop", "u", "pw", accept
        )

        assert datasource.datasource_name == "sales_data"

    @pytest.mark.parametrize("bad", ["", "   ", "sales-data", "sales data", "a" * 256])
    async def test_an_invalid_name_is_a_422(
        self, db, user, stub_metadata, bad: str  # noqa: ANN001
    ) -> None:
        with pytest.raises(HTTPException) as excinfo:
            await svc.create_datasource(
                db, user.id, bad, "postgres", "h", "5432", "shop", "u", "pw", accept
            )

        assert excinfo.value.status_code == 422

    async def test_a_failed_connection_test_is_a_readable_400(
        self, db, user, stub_metadata  # noqa: ANN001
    ) -> None:
        with pytest.raises(HTTPException) as excinfo:
            await svc.create_datasource(
                db, user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "pw", reject
            )

        assert excinfo.value.status_code == 400
        assert "double-check the host, port" in excinfo.value.detail

    @pytest.mark.parametrize(("db_type", "database"), [("", "shop"), ("postgres", "")])
    async def test_missing_required_fields_is_a_400(
        self, db, user, stub_metadata, db_type: str, database: str  # noqa: ANN001
    ) -> None:
        with pytest.raises(HTTPException) as excinfo:
            await svc.create_datasource(
                db, user.id, "sales_data", db_type, "h", "5432", database, "u", "pw", accept
            )

        assert excinfo.value.status_code == 400
        assert excinfo.value.detail == "Missing required fields"

    async def test_a_file_datasource_skips_the_connection_test_and_metadata(
        self, db, user, stub_metadata  # noqa: ANN001
    ) -> None:
        """A CSV has no host to reach, so neither the tester nor the metadata
        collector should run — the file's schema is read on upload instead."""
        datasource = await svc.create_datasource(
            db, user.id, "uploaded_csv", "csv", "", "", "", "", "", reject
        )

        assert datasource.db_type == "csv"
        assert stub_metadata["calls"] == []

    async def test_a_duplicate_name_is_a_409(
        self, db, user, stub_metadata  # noqa: ANN001
    ) -> None:
        await svc.create_datasource(
            db, user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "pw", accept
        )

        with pytest.raises(HTTPException) as excinfo:
            await svc.create_datasource(
                db, user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "pw", accept
            )

        assert excinfo.value.status_code == 409
        assert excinfo.value.detail == "A datasource with this name already exists"

    async def test_duplicate_detection_is_case_insensitive(
        self, db, user, stub_metadata  # noqa: ANN001
    ) -> None:
        """The name is lowercased by the schema before it reaches the index, and
        the index is on ``lower(datasource_name)`` as well."""
        await svc.create_datasource(
            db, user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "pw", accept
        )

        with pytest.raises(HTTPException) as excinfo:
            await svc.create_datasource(
                db, user.id, "SALES_DATA", "postgres", "h", "5432", "shop", "u", "pw", accept
            )

        assert excinfo.value.status_code == 409


class TestDatasourceNamesAreScopedPerUser:
    """
    Regression tests for a fixed multi-tenancy defect.

    ``uq_datasource_name_lower`` indexed ``lower(datasource_name)`` alone, with no
    ``user_id``, so names were unique across every tenant: once one customer
    created "sales_data", no other customer could ever use it, and the 409 they
    got named a row they could not see. The index is now
    ``uq_datasource_user_name_lower`` and includes the owner, matching
    ``uq_workspace_user_name_lower`` and the other per-owner indexes.
    """

    async def test_two_users_may_each_own_the_same_name(
        self, db, user, other_user, stub_metadata  # noqa: ANN001
    ) -> None:
        mine = await svc.create_datasource(
            db, user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "pw", accept
        )
        theirs = await svc.create_datasource(
            db, other_user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "pw", accept
        )

        assert mine.user_id == user.id
        assert theirs.user_id == other_user.id
        assert mine.uuid != theirs.uuid

    async def test_case_insensitivity_still_applies_across_users(
        self, db, user, other_user, stub_metadata  # noqa: ANN001
    ) -> None:
        """The other tenant's differently-cased name is also fine — the index is
        scoped by owner first, so casing only matters within one account."""
        await svc.create_datasource(
            db, user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "pw", accept
        )

        theirs = await svc.create_datasource(
            db, other_user.id, "SALES_DATA", "postgres", "h", "5432", "shop", "u", "pw", accept
        )

        assert theirs.datasource_name == "sales_data"

    async def test_a_user_still_cannot_reuse_their_own_name(
        self, db, user, other_user, stub_metadata  # noqa: ANN001
    ) -> None:
        """The constraint is loosened, not removed."""
        await svc.create_datasource(
            db, other_user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "pw", accept
        )
        await svc.create_datasource(
            db, user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "pw", accept
        )

        with pytest.raises(HTTPException) as excinfo:
            await svc.create_datasource(
                db, user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "pw", accept
            )

        assert excinfo.value.status_code == 409

    async def test_renaming_onto_another_users_name_is_allowed(
        self, db, user, other_user, make_datasource  # noqa: ANN001
    ) -> None:
        await make_datasource(other_user, "sales_data")
        mine = await make_datasource(user, "draft")

        updated = await svc.update_datasource_name(db, mine.uuid, user.id, "sales_data")

        assert updated.datasource_name == "sales_data"


class TestCreateDatasourceReportsItsOwnErrors:
    """
    Regression tests for a fixed silent failure — CLAUDE.md's first rule.

    ``create_datasource`` raises a carefully-worded HTTPException(400) when it
    connects but can read no tables. That raise sat *inside* the same ``try``
    whose last clause was ``except Exception: print(...); return False``, and
    ``HTTPException`` is an ``Exception`` — so the 400 was caught, printed to
    stdout, and the caller got ``False``. An ``except HTTPException: raise`` arm
    now lets it through, and the catch-all logs and raises a 500 instead of
    returning a falsy value.
    """

    async def test_an_empty_database_raises_its_400(
        self, db, user, stub_metadata  # noqa: ANN001
    ) -> None:
        stub_metadata["rdbms_tables"] = []

        with pytest.raises(HTTPException) as excinfo:
            await svc.create_datasource(
                db, user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "pw", accept
            )

        assert excinfo.value.status_code == 400
        assert "could not read" in excinfo.value.detail

    async def test_the_orphaned_row_is_cleaned_up(
        self, db, user, stub_metadata  # noqa: ANN001
    ) -> None:
        """The row is deleted before the raise, so no half-configured datasource
        is left behind."""
        stub_metadata["rdbms_tables"] = []

        with pytest.raises(HTTPException):
            await svc.create_datasource(
                db, user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "pw", accept
            )

        assert await svc.get_user_datasources(db, user.id) == []

    async def test_an_unexpected_error_becomes_a_500_not_a_false(
        self, db, user, stub_metadata, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
    ) -> None:
        """The catch-all no longer swallows. It logs the real cause and raises a
        generic 500 — the caller gets an exception, and the stack trace goes to
        the log rather than to stdout via ``print``."""

        async def boom(datasource):  # noqa: ANN001
            raise RuntimeError("something unexpected")

        monkeypatch.setattr(svc, "collect_datasource_metadata", boom)

        with pytest.raises(HTTPException) as excinfo:
            await svc.create_datasource(
                db, user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "pw", accept
            )

        assert excinfo.value.status_code == 500
        assert "could not be saved" in excinfo.value.detail

    async def test_the_unexpected_error_is_logged(
        self, db, user, stub_metadata, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,  # noqa: ANN001
    ) -> None:
        async def boom(datasource):  # noqa: ANN001
            raise RuntimeError("something unexpected")

        monkeypatch.setattr(svc, "collect_datasource_metadata", boom)

        with caplog.at_level("ERROR"):
            with pytest.raises(HTTPException):
                await svc.create_datasource(
                    db, user.id, "sales_data", "postgres", "h", "5432", "shop", "u", "pw", accept
                )

        assert "something unexpected" in caplog.text


# ---------------------------------------------------------------------------
# collect_datasource_metadata / get_tables_columns
# ---------------------------------------------------------------------------
class TestCollectDatasourceMetadata:
    async def test_builds_one_entry_per_table(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "sales_data")

        configuration = await svc.collect_datasource_metadata(datasource)

        assert set(configuration) == {"orders", "customers"}
        assert configuration["orders"]["table_name"] == "orders"
        assert configuration["orders"]["status"] == "active"
        assert configuration["orders"]["column_count"] == 2

    async def test_columns_default_to_active(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "sales_data")

        configuration = await svc.collect_datasource_metadata(datasource)

        assert configuration["orders"]["column_data"]["id"] == {
            "column_name": "id",
            "status": "active",
        }

    async def test_mongo_uses_the_collection_lister(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "events_db", db_type="mongodb")

        configuration = await svc.collect_datasource_metadata(datasource)

        assert set(configuration) == {"events"}
        assert "mongo_collections" in stub_metadata["calls"]

    async def test_an_unreadable_catalog_yields_an_empty_configuration(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "sales_data")
        stub_metadata["raise_on"] = "tables"

        assert await svc.collect_datasource_metadata(datasource) == {}

    async def test_a_table_whose_schema_fails_is_skipped_not_fatal(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        """One table the user cannot read must not lose them the whole
        datasource — the loop continues past it."""
        datasource = await make_datasource(user, "sales_data")
        stub_metadata["raise_on"] = "schema"

        assert await svc.collect_datasource_metadata(datasource) == {}

    async def test_get_tables_columns_accepts_either_column_key(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        """RDBMS metadata returns ``column``; some paths return ``column_name``.
        Both are accepted so the two producers can coexist."""
        datasource = await make_datasource(user, "sales_data")
        stub_metadata["schema"] = [{"column_name": "id"}, {"column": "total"}]

        configuration = await svc.get_tables_columns(datasource, "orders")

        assert set(configuration["column_data"]) == {"id", "total"}


# ---------------------------------------------------------------------------
# update_datasource_name
# ---------------------------------------------------------------------------
class TestUpdateDatasourceName:
    async def test_renames(self, db, user, make_datasource) -> None:  # noqa: ANN001
        datasource = await make_datasource(user, "old_name")

        updated = await svc.update_datasource_name(db, datasource.uuid, user.id, "new_name")

        assert updated.datasource_name == "new_name"

    async def test_normalises_the_new_name(self, db, user, make_datasource) -> None:  # noqa: ANN001
        datasource = await make_datasource(user, "old_name")

        updated = await svc.update_datasource_name(db, datasource.uuid, user.id, " NEW_Name ")

        assert updated.datasource_name == "new_name"

    async def test_an_unknown_uuid_is_404(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.update_datasource_name(db, uuid_pkg.uuid4(), user.id, "new_name")

        assert excinfo.value.status_code == 404

    async def test_another_users_datasource_is_404(
        self, db, user, other_user, make_datasource  # noqa: ANN001
    ) -> None:
        theirs = await make_datasource(other_user, "theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.update_datasource_name(db, theirs.uuid, user.id, "hijacked")

        assert excinfo.value.status_code == 404

    @pytest.mark.parametrize("bad", ["", "  ", "bad-name", "a" * 256])
    async def test_an_invalid_new_name_is_a_422(
        self, db, user, make_datasource, bad: str  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "old_name")

        with pytest.raises(HTTPException) as excinfo:
            await svc.update_datasource_name(db, datasource.uuid, user.id, bad)

        assert excinfo.value.status_code == 422

    async def test_a_taken_name_is_a_409(self, db, user, make_datasource) -> None:  # noqa: ANN001
        await make_datasource(user, "taken")
        datasource = await make_datasource(user, "mine")

        with pytest.raises(HTTPException) as excinfo:
            await svc.update_datasource_name(db, datasource.uuid, user.id, "taken")

        assert excinfo.value.status_code == 409


# ---------------------------------------------------------------------------
# get_datasource_objects
# ---------------------------------------------------------------------------
class TestGetDatasourceObjects:
    async def test_returns_rdbms_tables_and_the_public_uuid(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "sales_data", database_name="shop")

        result = await svc.get_datasource_objects(db, datasource.uuid, user.id)

        assert result["objects"] == ["orders", "customers"]
        assert result["datasource_id"] == str(datasource.uuid)
        assert result["database"] == "shop"

    async def test_never_exposes_the_internal_id(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "sales_data")

        result = await svc.get_datasource_objects(db, datasource.uuid, user.id)

        assert result["datasource_id"] == str(datasource.uuid)
        assert str(datasource.id) != result["datasource_id"]

    async def test_mongo_returns_collections(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "events_db", db_type="mongodb")

        result = await svc.get_datasource_objects(db, datasource.uuid, user.id)

        assert result["objects"] == ["events"]

    async def test_a_file_datasource_lists_each_active_files_tables(
        self, db, user, make_datasource, make_file, tmp_path: Path  # noqa: ANN001
    ) -> None:
        """For a file datasource the "tables" come from the uploaded files
        themselves, each tagged with the file's public uuid so the UI can tell
        two uploads apart."""
        datasource = await make_datasource(user, "uploaded", db_type="csv")
        path = tmp_path / "products.csv"
        path.write_text("id,name\n1,Widget\n")
        file = await make_file(datasource, path)

        result = await svc.get_datasource_objects(db, datasource.uuid, user.id)

        assert result["objects"] == [{"name": "products.csv", "file_id": str(file.uuid)}]

    async def test_inactive_files_are_excluded(
        self, db, user, make_datasource, make_file, tmp_path: Path  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "uploaded", db_type="csv")
        path = tmp_path / "products.csv"
        path.write_text("id\n1\n")
        await make_file(datasource, path, is_active=False)

        result = await svc.get_datasource_objects(db, datasource.uuid, user.id)

        assert result["objects"] == []

    async def test_an_unknown_uuid_is_404(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.get_datasource_objects(db, uuid_pkg.uuid4(), user.id)

        assert excinfo.value.status_code == 404

    async def test_another_users_datasource_is_404(
        self, db, user, other_user, make_datasource  # noqa: ANN001
    ) -> None:
        theirs = await make_datasource(other_user, "theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.get_datasource_objects(db, theirs.uuid, user.id)

        assert excinfo.value.status_code == 404

    async def test_a_metadata_failure_becomes_a_400_naming_the_cause(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "sales_data")
        stub_metadata["raise_on"] = "tables"

        with pytest.raises(HTTPException) as excinfo:
            await svc.get_datasource_objects(db, datasource.uuid, user.id)

        assert excinfo.value.status_code == 400
        assert "catalog unreadable" in excinfo.value.detail


# ---------------------------------------------------------------------------
# get_datasource_table_schema
# ---------------------------------------------------------------------------
class TestGetDatasourceTableSchema:
    async def test_returns_the_columns(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "sales_data")

        result = await svc.get_datasource_table_schema(
            db, datasource.uuid, user.id, "orders"
        )

        assert result["table"] == "orders"
        assert result["schema"] == stub_metadata["schema"]
        assert result["datasource_id"] == str(datasource.uuid)

    async def test_an_unknown_uuid_is_404(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.get_datasource_table_schema(db, uuid_pkg.uuid4(), user.id, "orders")

        assert excinfo.value.status_code == 404

    async def test_a_schema_failure_becomes_a_400_naming_the_cause(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "sales_data")
        stub_metadata["raise_on"] = "schema"

        with pytest.raises(HTTPException) as excinfo:
            await svc.get_datasource_table_schema(db, datasource.uuid, user.id, "orders")

        assert excinfo.value.status_code == 400
        assert "no permission" in excinfo.value.detail


# ---------------------------------------------------------------------------
# Listing, deletion, toggling
# ---------------------------------------------------------------------------
class TestGetUserDatasources:
    async def test_returns_only_this_users_rows(
        self, db, user, other_user, make_datasource  # noqa: ANN001
    ) -> None:
        await make_datasource(user, "mine")
        await make_datasource(other_user, "theirs")

        rows = await svc.get_user_datasources(db, user.id)

        assert [d.datasource_name for d in rows] == ["mine"]

    async def test_a_new_user_has_none(self, db, user) -> None:  # noqa: ANN001
        assert await svc.get_user_datasources(db, user.id) == []


class TestDeleteDatasource:
    async def test_deletes(self, db, user, make_datasource) -> None:  # noqa: ANN001
        datasource = await make_datasource(user, "sales_data")

        await svc.delete_datasource(db, datasource.uuid, user.id)

        assert await svc.get_user_datasources(db, user.id) == []

    async def test_an_unknown_uuid_is_404(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.delete_datasource(db, uuid_pkg.uuid4(), user.id)

        assert excinfo.value.status_code == 404

    async def test_another_users_datasource_is_404_and_survives(
        self, db, user, other_user, make_datasource  # noqa: ANN001
    ) -> None:
        theirs = await make_datasource(other_user, "theirs")

        with pytest.raises(HTTPException):
            await svc.delete_datasource(db, theirs.uuid, user.id)

        assert len(await svc.get_user_datasources(db, other_user.id)) == 1


class TestDeleteDatasourceFile:
    async def test_removes_the_row_and_the_file_on_disk(
        self, db, user, make_datasource, make_file, tmp_path: Path  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "uploaded", db_type="csv")
        path = tmp_path / "products.csv"
        path.write_text("id\n1\n")
        file = await make_file(datasource, path)

        await svc.delete_datasource_file(db, datasource.uuid, file.uuid, user.id)

        assert not path.exists()

    async def test_a_missing_file_on_disk_is_not_an_error(
        self, db, user, make_datasource, make_file, tmp_path: Path  # noqa: ANN001
    ) -> None:
        """The row is the source of truth; an already-absent file should not
        block deleting it."""
        datasource = await make_datasource(user, "uploaded", db_type="csv")
        file = await make_file(datasource, tmp_path / "never_written.csv")

        await svc.delete_datasource_file(db, datasource.uuid, file.uuid, user.id)

    async def test_an_unknown_file_is_404(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "uploaded", db_type="csv")

        with pytest.raises(HTTPException) as excinfo:
            await svc.delete_datasource_file(
                db, datasource.uuid, uuid_pkg.uuid4(), user.id
            )

        assert excinfo.value.status_code == 404
        assert excinfo.value.detail == "File not found"

    async def test_a_file_belonging_to_another_datasource_is_404(
        self, db, user, make_datasource, make_file, tmp_path: Path  # noqa: ANN001
    ) -> None:
        """The file is looked up by uuid *and* datasource_id, so a valid file
        uuid from a different datasource must not delete."""
        first = await make_datasource(user, "first", db_type="csv")
        second = await make_datasource(user, "second", db_type="csv")
        path = tmp_path / "products.csv"
        path.write_text("id\n1\n")
        file = await make_file(second, path)

        with pytest.raises(HTTPException) as excinfo:
            await svc.delete_datasource_file(db, first.uuid, file.uuid, user.id)

        assert excinfo.value.status_code == 404

    async def test_an_unknown_datasource_is_404(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.delete_datasource_file(
                db, uuid_pkg.uuid4(), uuid_pkg.uuid4(), user.id
            )

        assert excinfo.value.status_code == 404
        assert excinfo.value.detail == "Datasource not found"


class TestToggleDatasourceActive:
    async def test_flips_the_flag(self, db, user, make_datasource) -> None:  # noqa: ANN001
        datasource = await make_datasource(user, "sales_data")
        assert datasource.is_active is True

        toggled = await svc.toggle_datasource_active(db, datasource.uuid, user.id)

        assert toggled.is_active is False

    async def test_toggling_twice_restores(self, db, user, make_datasource) -> None:  # noqa: ANN001
        datasource = await make_datasource(user, "sales_data")

        await svc.toggle_datasource_active(db, datasource.uuid, user.id)
        restored = await svc.toggle_datasource_active(db, datasource.uuid, user.id)

        assert restored.is_active is True

    async def test_another_users_datasource_is_404(
        self, db, user, other_user, make_datasource  # noqa: ANN001
    ) -> None:
        theirs = await make_datasource(other_user, "theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.toggle_datasource_active(db, theirs.uuid, user.id)

        assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# configuration_data toggling
# ---------------------------------------------------------------------------
class TestToggleColumnStatus:
    async def test_flips_a_configured_column(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(
            user,
            "sales_data",
            configuration_data={
                "orders": {
                    "status": "active",
                    "column_data": {"id": {"column_name": "id", "status": "active"}},
                }
            },
        )

        result = await svc.toggle_column_status_service(
            db, datasource.uuid, user.id, "orders", "id", "inactive"
        )

        assert result == {"column_name": "id", "status": "inactive"}

    async def test_creates_the_entry_for_an_unconfigured_column(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        """Upsert — a table discovered after the datasource was created has no
        stored config yet, and toggling it must not fail."""
        datasource = await make_datasource(user, "sales_data", configuration_data={})

        result = await svc.toggle_column_status_service(
            db, datasource.uuid, user.id, "orders", "total", "inactive"
        )

        assert result["status"] == "inactive"

    async def test_activating_a_column_under_an_inactive_table_is_refused(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        """
        The table switch dominates: a column cannot be switched on while its
        table is off, or the two would disagree.

        A 400 with a message, not the 404 used for ownership — this is a rule the
        user can act on, and returning "not found" for both would tell them
        nothing about which it was. It used to return ``None`` for both.
        """
        datasource = await make_datasource(
            user,
            "sales_data",
            configuration_data={"orders": {"status": "inactive", "column_data": {}}},
        )

        with pytest.raises(HTTPException) as excinfo:
            await svc.toggle_column_status_service(
                db, datasource.uuid, user.id, "orders", "id", "active"
            )

        assert excinfo.value.status_code == 400
        assert "Activate the table" in excinfo.value.detail

    async def test_deactivating_under_an_inactive_table_is_allowed(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(
            user,
            "sales_data",
            configuration_data={"orders": {"status": "inactive", "column_data": {}}},
        )

        result = await svc.toggle_column_status_service(
            db, datasource.uuid, user.id, "orders", "id", "inactive"
        )

        assert result["status"] == "inactive"

    async def test_the_change_is_persisted(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        """``flag_modified`` is what makes this stick — SQLAlchemy does not
        detect in-place mutation of a JSON column, so without it the commit
        would write nothing."""
        datasource = await make_datasource(user, "sales_data", configuration_data={})

        await svc.toggle_column_status_service(
            db, datasource.uuid, user.id, "orders", "id", "inactive"
        )
        await db.refresh(datasource)

        assert (
            datasource.configuration_data["orders"]["column_data"]["id"]["status"]
            == "inactive"
        )

    async def test_another_users_datasource_is_404(
        self, db, user, other_user, make_datasource  # noqa: ANN001
    ) -> None:
        """
        Regression test for a fixed defect: this used to return ``None`` for a
        datasource the caller did not own — the same value it returns for the
        inactive-table rule — so a route could not tell an authorization failure
        from a business rule. It raises 404 now, like every other function here.
        """
        theirs = await make_datasource(other_user, "theirs", configuration_data={})

        with pytest.raises(HTTPException) as excinfo:
            await svc.toggle_column_status_service(
                db, theirs.uuid, user.id, "orders", "id", "inactive"
            )

        assert excinfo.value.status_code == 404

    async def test_an_unknown_uuid_is_404(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await svc.toggle_column_status_service(
                db, uuid_pkg.uuid4(), user.id, "orders", "id", "inactive"
            )

        assert excinfo.value.status_code == 404


class TestToggleTableStatus:
    async def test_flips_the_table(self, db, user, make_datasource) -> None:  # noqa: ANN001
        datasource = await make_datasource(
            user, "sales_data", configuration_data={"orders": {"status": "active"}}
        )

        result = await svc.toggle_table_status_service(
            db, datasource.uuid, user.id, "orders", "inactive"
        )

        assert result["status"] == "inactive"

    async def test_deactivating_cascades_to_every_column(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        """Switching a table off must switch its columns off too — a column left
        active under an inactive table disagrees with what the preview shows."""
        datasource = await make_datasource(
            user,
            "sales_data",
            configuration_data={
                "orders": {
                    "status": "active",
                    "column_data": {
                        "id": {"column_name": "id", "status": "active"},
                        "total": {"column_name": "total", "status": "active"},
                    },
                }
            },
        )

        result = await svc.toggle_table_status_service(
            db, datasource.uuid, user.id, "orders", "inactive"
        )

        assert [c["status"] for c in result["column_data"].values()] == [
            "inactive",
            "inactive",
        ]

    async def test_activating_cascades_to_every_column(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        """Switching a table on must switch its columns on too.

        The cascade is symmetric with deactivation: an active table whose columns
        are all inactive exposes no data, so leaving the columns untouched here
        would read as the activation having silently done nothing.
        """
        datasource = await make_datasource(
            user,
            "sales_data",
            configuration_data={
                "orders": {
                    "status": "inactive",
                    "column_data": {
                        "id": {"column_name": "id", "status": "inactive"},
                        "total": {"column_name": "total", "status": "inactive"},
                    },
                }
            },
        )

        result = await svc.toggle_table_status_service(
            db, datasource.uuid, user.id, "orders", "active"
        )

        assert result["status"] == "active"
        assert [c["status"] for c in result["column_data"].values()] == [
            "active",
            "active",
        ]

    async def test_cascade_survives_a_table_with_no_stored_columns(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        """A table discovered after the datasource was created has no
        ``column_data`` yet — the cascade must be a no-op rather than a KeyError,
        and must leave the key present so the next toggle has something to walk."""
        datasource = await make_datasource(
            user, "sales_data", configuration_data={"orders": {"status": "inactive"}}
        )

        result = await svc.toggle_table_status_service(
            db, datasource.uuid, user.id, "orders", "active"
        )

        assert result["status"] == "active"
        assert result["column_data"] == {}

    async def test_creates_the_entry_for_an_unconfigured_table(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "sales_data", configuration_data={})

        result = await svc.toggle_table_status_service(
            db, datasource.uuid, user.id, "new_table", "inactive"
        )

        assert result["status"] == "inactive"

    async def test_another_users_datasource_is_404(
        self, db, user, other_user, make_datasource  # noqa: ANN001
    ) -> None:
        theirs = await make_datasource(other_user, "theirs", configuration_data={})

        with pytest.raises(HTTPException) as excinfo:
            await svc.toggle_table_status_service(
                db, theirs.uuid, user.id, "orders", "inactive"
            )

        assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# search_sort_tables
# ---------------------------------------------------------------------------
class TestSearchSortTables:
    async def test_lists_live_tables_with_their_stored_status(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(
            user, "sales_data", configuration_data={"orders": {"status": "inactive"}}
        )

        result = await svc.search_sort_tables(db, datasource.uuid, user.id)

        assert result == [
            {"table_name": "customers", "status": "active"},
            {"table_name": "orders", "status": "inactive"},
        ]

    async def test_unconfigured_tables_default_to_active(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "sales_data", configuration_data={})

        result = await svc.search_sort_tables(db, datasource.uuid, user.id)

        assert all(entry["status"] == "active" for entry in result)

    async def test_search_matches_a_substring(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "sales_data")

        result = await svc.search_sort_tables(db, datasource.uuid, user.id, search="cust")

        assert [entry["table_name"] for entry in result] == ["customers"]

    @pytest.mark.parametrize("query", ["cust", "CUST", "Cust", "  CuSt  "])
    async def test_search_is_genuinely_case_insensitive(
        self, db, user, make_datasource, stub_metadata, query: str  # noqa: ANN001
    ) -> None:
        """
        Regression test for a fixed defect. The code was ``search in t.lower()``
        — it lowercased the *table* and not the *search term*, so any query
        containing an uppercase letter matched nothing, despite the comment
        claiming case-insensitivity. Both sides are lowercased now.
        """
        datasource = await make_datasource(user, "sales_data")

        result = await svc.search_sort_tables(db, datasource.uuid, user.id, search=query)

        assert [entry["table_name"] for entry in result] == ["customers"]

    async def test_the_status_filter_narrows_the_list(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(
            user, "sales_data", configuration_data={"orders": {"status": "inactive"}}
        )

        result = await svc.search_sort_tables(
            db, datasource.uuid, user.id, status_filter="inactive"
        )

        assert [entry["table_name"] for entry in result] == ["orders"]

    async def test_the_all_filter_keeps_everything(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(
            user, "sales_data", configuration_data={"orders": {"status": "inactive"}}
        )

        result = await svc.search_sort_tables(
            db, datasource.uuid, user.id, status_filter="all"
        )

        assert len(result) == 2

    @pytest.mark.parametrize(
        ("sort_by", "expected"),
        [(None, ["customers", "orders"]), ("az", ["customers", "orders"]), ("za", ["orders", "customers"])],
    )
    async def test_sorting(
        self, db, user, make_datasource, stub_metadata, sort_by, expected  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "sales_data")

        result = await svc.search_sort_tables(
            db, datasource.uuid, user.id, sort_by=sort_by
        )

        assert [entry["table_name"] for entry in result] == expected

    async def test_a_file_datasource_lists_nothing(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "uploaded", db_type="csv")

        assert await svc.search_sort_tables(db, datasource.uuid, user.id) == []

    async def test_an_unreadable_catalog_lists_nothing_rather_than_raising(
        self, db, user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "sales_data")
        stub_metadata["raise_on"] = "tables"

        assert await svc.search_sort_tables(db, datasource.uuid, user.id) == []

    async def test_another_users_datasource_is_404(
        self, db, user, other_user, make_datasource, stub_metadata  # noqa: ANN001
    ) -> None:
        """Used to return ``[]``, indistinguishable from a datasource that
        genuinely has no tables."""
        theirs = await make_datasource(other_user, "theirs")

        with pytest.raises(HTTPException) as excinfo:
            await svc.search_sort_tables(db, theirs.uuid, user.id)

        assert excinfo.value.status_code == 404

"""
Shared scaffolding for the Ask AI service tests.

Every test in this package needs the same two things: a datasource row owned by the
test user, and the reflection stubbed at the seam the service imports it through
(``metadata_service``). What is under test here is the pruning, the prompt and what
is done with the model's answer — never SQLAlchemy's Inspector — so the reflection is
answered from a dict the test can rewrite.
"""

from __future__ import annotations

import pytest

from app.models.datasource import DataSource
from app.services.sql_assist import sql_assist_service as svc


def reflected(table: str, *columns: str, **extras) -> dict:
    """One table as ``fetch_rdbms_metadata`` returns it."""
    entry = {
        "table": table,
        "kind": "table",
        "columns": [
            {"name": name, "type": "INTEGER", "nullable": True} for name in columns
        ],
        "primary_key": list(extras.pop("primary_key", [])),
        "foreign_keys": list(extras.pop("foreign_keys", [])),
    }
    entry.update(extras)
    return entry


ORDERS = reflected(
    "orders",
    "id",
    "total",
    "customer_id",
    primary_key=["id"],
    foreign_keys=[
        {
            "columns": ["customer_id"],
            "references_table": "customers",
            "references_columns": ["id"],
        }
    ],
)
CUSTOMERS = reflected("customers", "id", "name", primary_key=["id"])


def configuration(**tables) -> dict:
    """
    ``configuration_data`` from ``table="col,col"`` pairs naming the columns to switch
    off. A table mapped to ``None`` is switched off entirely.
    """
    built = {}

    for table_name, columns in tables.items():
        if columns is None:
            built[table_name] = {"status": "inactive"}
            continue

        built[table_name] = {
            "status": "active",
            "column_data": {
                name: {"column_name": name, "status": "inactive"}
                for name in columns.split(",")
                if name
            },
        }

    return built


@pytest.fixture
def make_datasource(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str = "warehouse", **kwargs):  # noqa: ANN001
        row = DataSource(
            user_id=owner.id,
            datasource_name=name,
            db_type=kwargs.pop("db_type", "postgres"),
            password_encrypted="enc",
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def stub_reflection(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub the two reflection helpers the panel reads structure through."""
    state: dict = {"tables": ["orders", "customers"], "metadata": [ORDERS, CUSTOMERS]}

    async def fake_tables(datasource):  # noqa: ANN001
        return list(state["tables"])

    async def fake_metadata(datasource, table_names):  # noqa: ANN001
        return [
            entry for entry in state["metadata"] if entry["table"] in table_names
        ]

    monkeypatch.setattr(
        svc.metadata_service, "get_rdbms_reflected_tables", fake_tables
    )
    monkeypatch.setattr(
        svc.metadata_service, "get_rdbms_reflected_metadata", fake_metadata
    )
    return state

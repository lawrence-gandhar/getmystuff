import uuid
from typing import Any, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from litestar.exceptions import HTTPException

from app.models.datasource import DatasourceToolBaseConfig, DataSource, DataSourceAgentConfig
from app.db.db_utils import CRUDQueryBuilder
from app.utils.query_joins import (
    query_tables,
    validated_column_reference,
    validated_joins,
)


config_crud = CRUDQueryBuilder(DatasourceToolBaseConfig)
datasource_crud = CRUDQueryBuilder(DataSource)


# -----------------------------------
# CREATE CONFIG
# -----------------------------------
async def create_config(
    db: AsyncSession,
    user_id: int,
    datasource_id: uuid.UUID,
    tool_name: str,
    table_name: str,
    base_config: dict,
) -> DatasourceToolBaseConfig:

    if not tool_name:
        raise HTTPException(status_code=400, detail="tool_name is required")

    if not table_name:
        raise HTTPException(status_code=400, detail="table_name is required")

    datasource = await datasource_crud.get_by_uuid(
        db, datasource_id, extra_filters={"user_id": user_id},
    )

    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    # Outside the try below: that catch-all turns anything it sees into a 500, which
    # would bury a perfectly readable validation message.
    base_config = validated_base_config(base_config, table_name, datasource.db_type)

    try:
        config = await config_crud.create(db, {
            "datasource_id": datasource.id,
            "tool_name": tool_name,
            "table_name": table_name,
            "base_config": base_config,
        })
        return config
    except IntegrityError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A configuration with tool name '{tool_name}' already exists "
                f"for table '{table_name}' in this datasource."
            ),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Failed to create configuration. Please try again.",
        ) from exc


# -----------------------------------
# CREATE CONFIG WITH SUBQUERIES (transactional)
# -----------------------------------
async def create_config_with_subqueries(
    db: AsyncSession,
    user_id: int,
    datasource_id: uuid.UUID,
    tool_name: str,
    table_name: str,
    base_config: dict,
    subquery_configs: List[dict],
) -> DatasourceToolBaseConfig:

    if not tool_name:
        raise HTTPException(status_code=400, detail="tool_name is required")

    if not table_name:
        raise HTTPException(status_code=400, detail="table_name is required")

    datasource = await datasource_crud.get_by_uuid(
        db, datasource_id, extra_filters={"user_id": user_id},
    )

    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    base_config = validated_base_config(base_config, table_name, datasource.db_type)

    try:
        # Create base config — flush to get its ID without committing yet
        base = DatasourceToolBaseConfig(
            datasource_id=datasource.id,
            tool_name=tool_name,
            table_name=table_name,
            base_config=base_config,
        )
        db.add(base)
        await db.flush()  # assigns base.id

        # Create a tool config entry for each subquery
        for sq in subquery_configs:
            agg_index = sq.get("agg_index", 0)
            agent_name = sq.get("alias") or f"subquery_agg_{agg_index}"
            table_column = sq.get("column") or ""
            tool_config = DataSourceAgentConfig(
                datasource_base_config_id=base.id,
                agent_name=agent_name,
                table_column=table_column,
                config={
                    "table": sq.get("table", ""),
                    "columns": sq.get("columns", []),
                    "aggregations": sq.get("aggregations", []),
                    "groupBy": sq.get("groupBy", []),
                    "filters": sq.get("filters", []),
                    "agg_index": agg_index,
                },
                policy_json={},
            )
            db.add(tool_config)

        await db.commit()
        await db.refresh(base)
        return base

    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail=(
                f"A configuration with tool name '{tool_name}' already exists "
                f"for table '{table_name}' in this datasource."
            ),
        ) from exc
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Failed to create configuration. Please try again.",
        ) from exc


# -----------------------------------
# VALIDATE BASE CONFIG
# -----------------------------------
def validated_base_config(
    base_config: Optional[dict],
    table_name: str,
    db_type: Optional[str],
) -> dict:
    """
    Check the parts of a base config this service is responsible for and return it
    ready to store.

    A base config is otherwise carried through as the builder produced it, but joins
    cannot be: they name tables, they only work on a relational datasource, and each
    one has to match against a table already in the query. app.utils.query_joins owns
    those rules, shared with the Tool Configs library so a join means the same thing
    wherever it was authored.

    With joins present every column reference is qualified as ``table.column``, so
    those are checked against the tables the query actually reads — a reference to a
    table the user never joined is rejected here rather than stored as a query that
    could not run.
    """
    config = dict(base_config or {})
    joins = validated_joins(config.get("joins"), table_name, db_type)

    if not joins:
        # Keep the stored shape exactly as it was before joins existed rather than
        # writing an empty key into every config.
        config.pop("joins", None)
        return config

    tables = query_tables(joins, table_name)

    config["joins"] = joins
    config["columns"] = _checked_references(
        config.get("columns"), "column", "Column", tables,
    )
    config["aggregations"] = _checked_references(
        config.get("aggregations"), "column", "Aggregation column", tables,
    )
    config["filters"] = _checked_references(
        config.get("filters"), "column", "Filter column", tables,
    )
    config["group_by"] = [
        validated_column_reference(entry, "Group by column", tables)
        for entry in (config.get("group_by") or [])
        if entry
    ]

    return config


def _checked_references(
    entries: Any,
    key: str,
    field_label: str,
    tables: List[str],
) -> list:
    """
    Validate one field of every entry in a query section, leaving the rest of the
    entry untouched — the aliases, functions and operators the Configurations builder
    writes are its own business.
    """
    if not entries:
        return []

    if not isinstance(entries, list):
        raise HTTPException(
            status_code=400,
            detail=f"{field_label}s are not in the expected format",
        )

    checked = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=400,
                detail=f"{field_label}s are not in the expected format",
            )
        checked.append({
            **entry,
            key: validated_column_reference(entry.get(key), field_label, tables),
        })

    return checked


# -----------------------------------
# CHECK TOOL NAME EXISTS
# -----------------------------------
async def check_tool_name_exists(
    db: AsyncSession,
    user_id: int,
    datasource_id: uuid.UUID,
    tool_name: str,
) -> bool:
    """Return True if a config with the given tool_name already exists for this datasource."""
    datasource = await datasource_crud.get_by_uuid(
        db, datasource_id, extra_filters={"user_id": user_id},
    )
    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    existing = await config_crud.get_one(
        db,
        filters={"datasource_id": datasource.id, "tool_name": tool_name},
    )
    return existing is not None


# -----------------------------------
# DELETE CONFIG
# -----------------------------------
async def delete_config(
    db: AsyncSession,
    user_id: int,
    config_id: uuid.UUID,
) -> bool:

    config = await config_crud.get_by_uuid(db, config_id)

    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")

    datasource = await datasource_crud.get_one(
        db,
        filters={"id": config.datasource_id, "user_id": user_id},
    )

    if not datasource:
        raise HTTPException(status_code=403, detail="Not authorized")

    return await config_crud.delete(db, config.id)

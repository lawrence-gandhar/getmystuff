import uuid
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from litestar.exceptions import HTTPException

from app.models.datasource import DatasourceToolBaseConfig, DataSource, DataSourceAgentConfig
from app.db.db_utils import CRUDQueryBuilder


config_crud = CRUDQueryBuilder(DatasourceToolBaseConfig)
datasource_crud = CRUDQueryBuilder(DataSource)


# -----------------------------------
# CREATE CONFIG
# -----------------------------------
async def create_config(
    db: AsyncSession,
    user_id: uuid.UUID,
    datasource_id: uuid.UUID,
    tool_name: str,
    table_name: str,
    base_config: dict,
) -> DatasourceToolBaseConfig:

    if not tool_name:
        raise HTTPException(status_code=400, detail="tool_name is required")

    if not table_name:
        raise HTTPException(status_code=400, detail="table_name is required")

    datasource = await datasource_crud.get_one(
        db,
        filters={"id": datasource_id, "user_id": user_id},
    )

    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    try:
        config = await config_crud.create(db, {
            "datasource_id": datasource_id,
            "tool_name": tool_name,
            "table_name": table_name,
            "base_config": base_config or {},
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
    user_id: uuid.UUID,
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

    datasource = await datasource_crud.get_one(
        db,
        filters={"id": datasource_id, "user_id": user_id},
    )

    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    try:
        # Create base config — flush to get its ID without committing yet
        base = DatasourceToolBaseConfig(
            datasource_id=datasource_id,
            tool_name=tool_name,
            table_name=table_name,
            base_config=base_config or {},
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
# CHECK TOOL NAME EXISTS
# -----------------------------------
async def check_tool_name_exists(
    db: AsyncSession,
    user_id: uuid.UUID,
    datasource_id: uuid.UUID,
    tool_name: str,
) -> bool:
    """Return True if a config with the given tool_name already exists for this datasource."""
    datasource = await datasource_crud.get_one(
        db,
        filters={"id": datasource_id, "user_id": user_id},
    )
    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    existing = await config_crud.get_one(
        db,
        filters={"datasource_id": datasource_id, "tool_name": tool_name},
    )
    return existing is not None


# -----------------------------------
# DELETE CONFIG
# -----------------------------------
async def delete_config(
    db: AsyncSession,
    user_id: uuid.UUID,
    config_id: uuid.UUID,
) -> bool:

    config = await config_crud.get_one(db, filters={"id": config_id})

    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")

    datasource = await datasource_crud.get_one(
        db,
        filters={"id": config.datasource_id, "user_id": user_id},
    )

    if not datasource:
        raise HTTPException(status_code=403, detail="Not authorized")

    return await config_crud.delete(db, config_id)

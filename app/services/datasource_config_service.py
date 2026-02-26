import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from litestar.exceptions import HTTPException

from app.models.datasource import DatasourceToolBaseConfig, DataSource
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
    base_config: dict,
) -> DatasourceToolBaseConfig:

    if not tool_name:
        raise HTTPException(status_code=400, detail="tool_name is required")

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
            "base_config": base_config or {},
        })
        return config
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


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

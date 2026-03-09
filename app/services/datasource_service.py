import uuid
import os
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from litestar.exceptions import HTTPException
from sqlalchemy.orm.attributes import flag_modified
from pydantic import ValidationError

from app.models.datasource import DataSource, DatasourceFile
from app.schemas.datasource import DatasourceCreateSchema, DatasourceUpdateSchema
from app.services.metadata_service import (
    get_rdbms_tables,
    get_mongo_collections,
    get_table_schema,
)
from app.utils.crypto import encrypt_password
from app.utils.file_utils import FILE_BASED_TYPES
from app.db.db_utils import (
    CRUDQueryBuilder,
    build_rdbms_url,
    build_mongo_uri,
    test_rdbms_connection,
    test_mongo_connection,
    fetch_file_listing,
)

from typing import Optional


datasource_crud = CRUDQueryBuilder(DataSource)


# -----------------------------------
# TEST DATASOURCE
# -----------------------------------
async def test_connection(db_type, host, port, database, username, password):
    """Test database connection using db_utils connection testers."""

    try:
        if db_type == "mongodb":
            uri = build_mongo_uri(host, port, username, password)
            return await test_mongo_connection(uri, database)
        else:
            url = build_rdbms_url(db_type, host, port, database, username, password)
            return await test_rdbms_connection(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        return False

# -----------------------------------
# CREATE DATASOURCE
# -----------------------------------
async def create_datasource(
    db: AsyncSession,
    user_id: uuid.UUID,
    datasource_name: str,
    db_type: str,
    host: str,
    port: str,
    database_name: str,
    username: str,
    password: str,
    connection_tester,
):
    # Validate and normalize datasource_name via Pydantic schema.
    # Raises HTTPException(422) on validation failure so the caller
    # (route handler) can surface a clean error message to the UI.
    try:
        validated = DatasourceCreateSchema(datasource_name=datasource_name)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors()[0]["msg"],
        )

    is_file_type = db_type in FILE_BASED_TYPES

    if not is_file_type:
        if not db_type or not database_name:
            raise HTTPException(status_code=400, detail="Missing required fields")

        is_valid = await connection_tester(
            db_type, host, port, database_name, username, password
        )

        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Could not connect to the database. "
                    "Please double-check the host, port, database name, username, and password, "
                    "and make sure the database server is running and reachable."
                ),
            )

    encrypted_password = encrypt_password(password) if password else ""

    try:
        datasource = await datasource_crud.create(db, {
            "user_id": user_id,
            "datasource_name": validated.datasource_name,
            "db_type": db_type,
            "host": host,
            "port": port,
            "database_name": database_name,
            "username": username,
            "password_encrypted": encrypted_password,
        })

        if not is_file_type:
            configuration_data = await collect_datasource_metadata(datasource)

            if not configuration_data:
                await db.delete(datasource)
                await db.commit()
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "We connected to the database successfully, but could not read "
                        "any tables or collections from it. Please make sure the database "
                        "is not empty and your user account has permission to list tables."
                    ),
                )

            datasource = await datasource_crud.update(
                db, datasource.id, {"configuration_data": configuration_data}
            )

        return datasource

    except IntegrityError:
        # The functional unique index uq_datasource_name_lower fired —
        # a datasource with the same name (case-insensitively) already exists.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A datasource with this name already exists",
        )
    except Exception as e:
        print(str(e))
        return False


# -----------------------------------
# UPDATE DATASOURCE NAME
# -----------------------------------
async def update_datasource_name(
    db: AsyncSession,
    datasource_id: uuid.UUID,
    user_id: uuid.UUID,
    datasource_name: str,
) -> DataSource:
    """
    Rename an existing datasource.

    Validates and normalizes the new name through DatasourceUpdateSchema,
    then persists it.  Returns the updated DataSource instance.

    Raises:
        HTTPException(404) – datasource not found or not owned by user.
        HTTPException(422) – validation / normalization failure.
        HTTPException(409) – name already taken (case-insensitive).
    """
    datasource = await datasource_crud.get_one(
        db,
        filters={"id": datasource_id, "user_id": user_id},
    )

    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    try:
        validated = DatasourceUpdateSchema(datasource_name=datasource_name)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors()[0]["msg"],
        )

    try:
        updated = await datasource_crud.update(
            db,
            datasource_id,
            {"datasource_name": validated.datasource_name},
        )
        return updated
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A datasource with this name already exists",
        )


# -----------------------------------
# GET DATASOURCE OBJECTS
# -----------------------------------
async def get_datasource_objects(
    db: AsyncSession,
    datasource_id: uuid.UUID,
    user_id: uuid.UUID,
):

    datasource = await datasource_crud.get_one(
        db,
        filters={
            "id": datasource_id,
            "user_id": user_id,
        }
    )

    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    try:
        if datasource.db_type == "mongodb":
            objects = await get_mongo_collections(datasource)
        elif datasource.db_type in FILE_BASED_TYPES:
            result = await db.execute(
                select(DatasourceFile)
                .where(DatasourceFile.datasource_id == datasource_id)
                .where(DatasourceFile.is_active == True)
            )
            files = result.scalars().all()
            objects = []
            for f in files:
                listing = await fetch_file_listing(path=f.file_path, file_type=datasource.db_type)
                for name in listing:
                    objects.append({"name": name, "file_id": str(f.id)})
        else:
            objects = await get_rdbms_tables(datasource)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Metadata fetch failed: {str(e)}"
        )

    return {
        "datasource_id": str(datasource.id),
        "datasource_name": datasource.datasource_name,
        "database": datasource.database_name,
        "host": datasource.host,
        "port": datasource.port,
        "user": datasource.username,
        "type": datasource.db_type,
        "objects": objects,
        "configuration_data": datasource.configuration_data
    }


# -----------------------------------
# GET TABLE SCHEMA
# -----------------------------------
async def get_datasource_table_schema(
    db: AsyncSession,
    datasource_id: uuid.UUID,
    user_id: uuid.UUID,
    table_name: str,
):

    datasource = await datasource_crud.get_one(
        db,
        filters={
            "id": datasource_id,
            "user_id": user_id,
        }
    )

    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    try:
        table_columns = await get_table_schema(datasource, table_name)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Schema fetch failed: {str(e)}"
        )

    return {
        "datasource_id": str(datasource.id),
        "database": datasource.database_name,
        "type": datasource.db_type,
        "table": table_name,
        "schema": table_columns,
    }

async def get_user_datasources(
    db: AsyncSession,
    user_id: uuid.UUID
):
    return await datasource_crud.get_many(
        db,
        filters={"user_id": user_id},
    )


async def delete_datasource(
    db: AsyncSession,
    datasource_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    datasource = await datasource_crud.get_one(
        db,
        filters={"id": datasource_id, "user_id": user_id},
    )
    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")
    await db.delete(datasource)
    await db.commit()


async def delete_datasource_file(
    db: AsyncSession,
    datasource_id: uuid.UUID,
    file_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    datasource = await datasource_crud.get_one(
        db,
        filters={"id": datasource_id, "user_id": user_id},
    )
    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    result = await db.execute(
        select(DatasourceFile)
        .where(DatasourceFile.id == file_id)
        .where(DatasourceFile.datasource_id == datasource_id)
    )
    file = result.scalar_one_or_none()
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    file_path = file.file_path
    await db.delete(file)
    await db.commit()

    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"File record deleted but could not remove file from disk: {e}",
        )


async def toggle_datasource_active(
    db: AsyncSession,
    datasource_id: uuid.UUID,
    user_id: uuid.UUID,
) -> DataSource:
    datasource = await datasource_crud.get_one(
        db,
        filters={"id": datasource_id, "user_id": user_id},
    )
    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")
    datasource.is_active = not datasource.is_active
    await db.commit()
    await db.refresh(datasource)
    return datasource


async def get_tables_columns(datasource, table_name):
    schema = await get_table_schema(datasource, table_name)

    column_data = {}
    configuration_data = {}

    for column in schema:
        column_name = column.get("column") or column.get("column_name")

        column_data[column_name] = {
            "column_name": column_name,
            "status": "active",      # default
        }

    configuration_data = {
        "table_name": table_name,
        "column_count": len(column_data),
        "column_data": column_data,
        "status": "active"
    }

    return configuration_data


async def collect_datasource_metadata(datasource):

    """
    Collects all tables/collections and builds configuration_data
    with column_data dictionary format.
    """

    configuration_data = {}

    try:
        if datasource.db_type == "mongodb":
            tables = await get_mongo_collections(datasource)
        else:
            tables = await get_rdbms_tables(datasource)
    except Exception:
        return configuration_data

    for table_name in tables:
        try:
            configuration_data[table_name] = await get_tables_columns(datasource, table_name)
        except Exception:
            continue

    return configuration_data

async def toggle_column_status_service(
    db: AsyncSession,
    datasource_id: uuid.UUID,
    user_id: uuid.UUID,
    table_name: str,
    column_name: str,
    new_status: str,
):

    datasource = await db.get(DataSource, datasource_id)

    if not datasource or datasource.user_id != user_id:
        return None

    configuration = datasource.configuration_data or {}

    # Upsert table entry
    if table_name not in configuration:
        configuration[table_name] = {}

    table_config = configuration[table_name]

    # Block activating a column when its table is inactive
    if table_config.get("status", "active") == "inactive" and new_status == "active":
        return None

    # Upsert column entry
    column_data = table_config.get("column_data", {})
    if column_name not in column_data:
        column_data[column_name] = {"column_name": column_name, "status": "active"}
    column_data[column_name]["status"] = new_status
    table_config["column_data"] = column_data
    configuration[table_name] = table_config

    datasource.configuration_data = configuration
    flag_modified(datasource, "configuration_data")

    await db.commit()
    await db.refresh(datasource)

    # Return updated column as dict
    return configuration[table_name]["column_data"][column_name]


async def toggle_table_status_service(
    db: AsyncSession,
    datasource_id: uuid.UUID,
    user_id: uuid.UUID,
    table_name: str,
    new_status: str,
):

    datasource = await db.get(DataSource, datasource_id)

    if not datasource or datasource.user_id != user_id:
        return None

    configuration = datasource.configuration_data or {}

    # Upsert — create entry if this table has never been configured yet
    if table_name not in configuration:
        configuration[table_name] = {}

    configuration[table_name]["status"] = new_status

    # Cascade: deactivate all columns when the table is deactivated
    if new_status == "inactive":
        column_data = configuration[table_name].get("column_data", {})
        for col_name in column_data:
            column_data[col_name]["status"] = "inactive"
        configuration[table_name]["column_data"] = column_data

    datasource.configuration_data = configuration
    flag_modified(datasource, "configuration_data")

    await db.commit()
    await db.refresh(datasource)

    # Return updated column as dict
    return configuration[table_name]

async def search_sort_tables(
    db: AsyncSession,
    datasource_id: uuid.UUID,
    user_id: uuid.UUID,
    search: Optional[str]=None,
    status_filter: Optional[str]=None,
    sort_by: Optional[str]=None
):
    datasource = await db.get(DataSource, datasource_id)

    if not datasource or datasource.user_id != user_id:
        return []

    # Fetch live objects so search works against the actual DB, not just saved config
    try:
        if datasource.db_type == "mongodb":
            live_tables = await get_mongo_collections(datasource)
        elif datasource.db_type in FILE_BASED_TYPES:
            live_tables = []
        else:
            live_tables = await get_rdbms_tables(datasource)
    except Exception:
        live_tables = []

    configuration = datasource.configuration_data or {}

    # SEARCH (substring, case-insensitive)
    if search:
        live_tables = [t for t in live_tables if search in t.lower()]

    # STATUS FILTER — use configuration_data status, default "active" for unconfigured tables
    if status_filter and status_filter != "all":
        live_tables = [
            t for t in live_tables
            if configuration.get(t, {}).get("status", "active") == status_filter
        ]

    # SORT
    live_tables.sort(reverse=(sort_by == "za"))

    # Build result — overlay status from configuration_data
    result = []
    for table_name in live_tables:
        cfg = configuration.get(table_name, {})
        result.append({
            "table_name": table_name,
            "status": cfg.get("status", "active"),
        })

    return result

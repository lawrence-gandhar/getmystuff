import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from litestar.exceptions import HTTPException
from sqlalchemy.orm.attributes import flag_modified

from app.models.datasource import DataSource
from app.services.metadata_service import (
    get_rdbms_tables,
    get_mongo_collections,
    get_table_schema,
)
from app.utils.crypto import encrypt_password
from app.db.db_utils import (
    CRUDQueryBuilder,
    build_rdbms_url,
    build_mongo_uri,
    test_rdbms_connection,
    test_mongo_connection
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
    except Exception:
        return False

# -----------------------------------
# CREATE DATASOURCE
# -----------------------------------
async def create_datasource(
    db: AsyncSession,
    user_id: uuid.UUID,
    db_type: str,
    host: str,
    port: str,
    database_name: str,
    username: str,
    password: str,
    connection_tester,
):

    if not db_type or not database_name:
        raise HTTPException(status_code=400, detail="Missing required fields")

    is_valid = await connection_tester(
        db_type, host, port, database_name, username, password
    )

    if not is_valid:
        raise HTTPException(status_code=400, detail="Connection Failed")

    encrypted_password = encrypt_password(password)

    try:
        datasource = await datasource_crud.create(db, {
            "user_id": user_id,
            "db_type": db_type,
            "host": host,
            "port": port,
            "database_name": database_name,
            "username": username,
            "password_encrypted": encrypted_password,
        })

        configuration_data = await collect_datasource_metadata(datasource)

        datasource = await datasource_crud.update(
            db, datasource.id, {"configuration_data":configuration_data}
        )

        return datasource
    
    except Exception as e:
        print(str(e))
        return False


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
        else:
            objects = await get_rdbms_tables(datasource)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Metadata fetch failed: {str(e)}"
        )

    return {
        "datasource_id": str(datasource.id),
        "database": datasource.database_name,
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

    try:
        configuration_data = {}

        # Get tables / collections
        if datasource.db_type == "mongodb":
            tables = await get_mongo_collections(datasource)
        else:
            tables = await get_rdbms_tables(datasource)

        # Loop tables
        for table_name in tables:
            configuration_data[table_name] = await get_tables_columns(datasource, table_name)
        
    except Exception as e:
        return False
    
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

    if table_name not in configuration:
        return None

    if column_name not in configuration[table_name]["column_data"]:
        return None

    configuration[table_name]["column_data"][column_name]["status"] = new_status

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

    if table_name not in configuration:
        return None

    configuration[table_name]["status"] = new_status

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
        return None

    configuration = datasource.configuration_data or {}

    tables = list(configuration.keys())

    # print("Tables", tables)

    # SEARCH
    if search:
        tables = [t for t in tables if search in t.lower()]

    # FILTER
    if status_filter != "all":
        tables = [
            t for t in tables
            if configuration[t]["status"] == status_filter
        ]

    # SORT
    reverse = True if sort_by == "za" else False
    tables.sort(reverse=reverse)

    # Return list of table configuration objects with table_name included
    result = []
    for table_name in tables:
        table_config = configuration[table_name].copy()
        table_config["table_name"] = table_name
        table_config.pop("column_data")
        table_config.pop("column_count")
        result.append(table_config)

    return result
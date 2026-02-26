from app.utils.crypto import decrypt_password
from app.db.db_utils import (
    build_rdbms_url,
    build_mongo_uri,
    fetch_rdbms_tables,
    fetch_mongo_collections,
    fetch_rdbms_schema,
    fetch_mongo_schema,
)


async def get_rdbms_tables(datasource):
    """Fetch RDBMS tables using db_utils connection management."""
    password = decrypt_password(datasource.password_encrypted)

    url = build_rdbms_url(
        db_type=datasource.db_type,
        host=datasource.host,
        port=datasource.port,
        database=datasource.database_name,
        username=datasource.username,
        password=password,
    )

    return await fetch_rdbms_tables(url, datasource.db_type)


async def get_mongo_collections(datasource):
    """Fetch MongoDB collections using db_utils connection management."""
    password = decrypt_password(datasource.password_encrypted)

    uri = build_mongo_uri(
        host=datasource.host,
        port=datasource.port,
        username=datasource.username,
        password=password,
    )

    return await fetch_mongo_collections(uri, datasource.database_name)


async def get_table_schema(datasource, table_name: str):
    """Fetch table/collection schema (columns and types) using db_utils."""
    password = decrypt_password(datasource.password_encrypted)

    if datasource.db_type == "mongodb":
        uri = build_mongo_uri(
            host=datasource.host,
            port=datasource.port,
            username=datasource.username,
            password=password,
        )
        return await fetch_mongo_schema(uri, datasource.database_name, table_name)
    else:
        url = build_rdbms_url(
            db_type=datasource.db_type,
            host=datasource.host,
            port=datasource.port,
            database=datasource.database_name,
            username=datasource.username,
            password=password,
        )
        return await fetch_rdbms_schema(url, datasource.db_type, table_name)
from typing import Any, Dict, List

from app.utils.crypto import decrypt_password
from app.db.db_utils import (
    build_rdbms_url,
    build_mongo_uri,
    fetch_rdbms_metadata,
    fetch_rdbms_table_names,
    fetch_rdbms_tables,
    fetch_mongo_collections,
    fetch_rdbms_schema,
    fetch_mongo_schema,
)


def rdbms_url(datasource) -> str:
    """
    The connection URL for a datasource, with its password decrypted.

    Public because the Deep Agents query executor needs the same URL to run a
    tool config's query (app.services.deep_agents.query_executor). Duplicating
    the decrypt-and-build there would mean two places deciding how a datasource
    becomes a connection.
    """
    password = decrypt_password(datasource.password_encrypted) if datasource.password_encrypted else ""

    return build_rdbms_url(
        db_type=datasource.db_type,
        host=datasource.host,
        port=datasource.port,
        database=datasource.database_name,
        username=datasource.username,
        password=password,
    )


async def get_rdbms_tables(datasource):
    """Fetch RDBMS tables using db_utils connection management."""
    return await fetch_rdbms_tables(rdbms_url(datasource), datasource.db_type)


async def get_rdbms_reflected_tables(datasource) -> List[str]:
    """
    Table and view names read by reflection rather than by a hand-written catalog
    query — the listing counterpart of :func:`get_rdbms_reflected_metadata`, used
    where the caller is only allowed to see structure.
    """
    return await fetch_rdbms_table_names(rdbms_url(datasource))


async def get_rdbms_reflected_metadata(
    datasource,
    table_names: List[str],
) -> List[Dict[str, Any]]:
    """
    The structure of the named tables — columns, types, primary and foreign keys —
    read with SQLAlchemy's Inspector and containing no row data at all.

    This is what the AI SQL assistant is given (see
    app.services.sql_assist.sql_assist_service); nothing else in the app needs a
    reflected view yet.
    """
    return await fetch_rdbms_metadata(rdbms_url(datasource), table_names)


async def get_mongo_collections(datasource):
    """Fetch MongoDB collections using db_utils connection management."""
    password = decrypt_password(datasource.password_encrypted) if datasource.password_encrypted else ""

    uri = build_mongo_uri(
        host=datasource.host,
        port=datasource.port,
        username=datasource.username,
        password=password,
    )

    return await fetch_mongo_collections(uri, datasource.database_name)


async def get_table_schema(datasource, table_name: str):
    """Fetch table/collection schema (columns and types) using db_utils."""
    password = decrypt_password(datasource.password_encrypted) if datasource.password_encrypted else ""

    if datasource.db_type == "mongodb":
        uri = build_mongo_uri(
            host=datasource.host,
            port=datasource.port,
            username=datasource.username,
            password=password,
        )
        return await fetch_mongo_schema(uri, datasource.database_name, table_name)
    else:
        return await fetch_rdbms_schema(
            rdbms_url(datasource), datasource.db_type, table_name,
        )
import os
import logging
from typing import List
from io import StringIO

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# --------------------------------------------------
# Logging Configuration
# --------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

# --------------------------------------------------
# Database Configuration
# --------------------------------------------------

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "flight_delays_cancelation",
    "user": "postgres",
    "password": "123"
}

CSV_FOLDER = r"C:\Users\user\Downloads\2015_flight_delays_cancelation"
CHUNK_SIZE = 50000


# --------------------------------------------------
# Engine Factory
# --------------------------------------------------

def get_engine() -> Engine:
    """
    Create SQLAlchemy engine with connection pooling.
    """

    db_url = (
        f"postgresql+psycopg2://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
        f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
    )

    engine = create_engine(
        db_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True
    )

    return engine


# --------------------------------------------------
# Column Cleaning
# --------------------------------------------------

def clean_column(name: str) -> str:
    """
    Normalize column names to database-friendly format.
    """

    return (
        name.strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
    )


# --------------------------------------------------
# Table Creation
# --------------------------------------------------

def create_table(engine: Engine, table_name: str, columns: List[str]) -> None:
    """
    Create table dynamically based on CSV columns.
    """

    columns_sql = ", ".join([f'"{col}" TEXT' for col in columns])

    query = f"""
    CREATE TABLE IF NOT EXISTS "{table_name}" (
        {columns_sql}
    )
    """

    with engine.begin() as conn:
        conn.execute(text(query))

    logger.info("Table ensured: %s", table_name)


# --------------------------------------------------
# Bulk COPY Insert
# --------------------------------------------------

def copy_chunk(engine: Engine, table_name: str, df: pd.DataFrame) -> None:
    """
    Bulk load dataframe chunk using PostgreSQL COPY.
    """

    buffer = StringIO()
    df.to_csv(buffer, index=False, header=False)
    buffer.seek(0)

    raw_conn = engine.raw_connection()
    try:
        with raw_conn.cursor() as cur:
            copy_sql = f"COPY \"{table_name}\" FROM STDIN WITH CSV"
            cur.copy_expert(copy_sql, buffer)

        raw_conn.commit()

    except Exception as e:
        raw_conn.rollback()
        logger.exception("COPY failed for table %s", table_name)
        raise

    finally:
        raw_conn.close()


# --------------------------------------------------
# CSV Processing
# --------------------------------------------------

def process_csv_file(engine: Engine, file_path: str) -> None:
    """
    Process a single CSV file and load into PostgreSQL.
    """

    table_name = os.path.splitext(os.path.basename(file_path))[0].lower()

    logger.info("Processing file: %s → table: %s", file_path, table_name)

    total_rows = 0
    first_chunk = True

    try:
        chunk_iter = pd.read_csv(file_path, chunksize=CHUNK_SIZE)

        for chunk in chunk_iter:

            chunk.columns = [clean_column(c) for c in chunk.columns]

            if first_chunk:
                create_table(engine, table_name, chunk.columns.tolist())
                first_chunk = False

            copy_chunk(engine, table_name, chunk)

            total_rows += len(chunk)

            logger.info("Inserted rows so far: %s", total_rows)

        logger.info("Finished %s → %s rows inserted", table_name, total_rows)

    except Exception:
        logger.exception("Failed processing file %s", file_path)
        raise


# --------------------------------------------------
# Folder Seeder
# --------------------------------------------------

def seed_folder(folder: str) -> None:
    """
    Load all CSV files from a folder into PostgreSQL.
    """

    engine = get_engine()

    logger.info("Starting CSV ingestion from folder: %s", folder)

    try:
        for file in os.listdir(folder):

            if file.endswith(".csv"):
                file_path = os.path.join(folder, file)
                process_csv_file(engine, file_path)

    except Exception:
        logger.exception("Folder processing failed")
        raise

    finally:
        engine.dispose()

    logger.info("All files processed successfully")


# --------------------------------------------------
# Entry Point
# --------------------------------------------------

if __name__ == "__main__":
    seed_folder(CSV_FOLDER)
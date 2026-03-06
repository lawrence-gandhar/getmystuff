import os
import pandas as pd
import psycopg2
from psycopg2 import sql
from io import StringIO

# PostgreSQL config
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "flight_delays_cancelation",
    "user": "postgres",
    "password": "123"
}

CSV_FOLDER = "C:\\Users\\user\\Downloads\\2015_flight_delays_cancelation"
CHUNK_SIZE = 50


def clean_column(name: str) -> str:
    return (
        name.strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
    )


def create_table(conn, table_name, columns):
    cur = conn.cursor()

    cols = [sql.SQL("{} TEXT").format(sql.Identifier(col)) for col in columns]

    query = sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
        sql.Identifier(table_name),
        sql.SQL(", ").join(cols)
    )

    cur.execute(query)
    conn.commit()
    cur.close()


def copy_chunk(conn, table_name, df):
    buffer = StringIO()
    df.to_csv(buffer, index=False, header=False)
    buffer.seek(0)

    cur = conn.cursor()

    copy_sql = sql.SQL(
        "COPY {} FROM STDIN WITH CSV"
    ).format(sql.Identifier(table_name))

    cur.copy_expert(copy_sql, buffer)
    conn.commit()
    cur.close()


def process_csv_file(conn, file_path):
    table_name = os.path.splitext(os.path.basename(file_path))[0].lower()

    print(f"\nProcessing {file_path} → table `{table_name}`")

    chunk_iter = pd.read_csv(file_path, chunksize=CHUNK_SIZE)

    first_chunk = True
    total_rows = 0

    for chunk in chunk_iter:

        chunk.columns = [clean_column(c) for c in chunk.columns]

        if first_chunk:
            create_table(conn, table_name, chunk.columns)
            first_chunk = False

        copy_chunk(conn, table_name, chunk)

        total_rows += len(chunk)
        print(f"Inserted {total_rows} rows...", end="\r")

    print(f"\nFinished {table_name} → {total_rows} rows inserted")


def seed_folder(folder):
    conn = psycopg2.connect(**DB_CONFIG)

    for file in os.listdir(folder):
        if file.endswith(".csv"):
            process_csv_file(conn, os.path.join(folder, file))

    conn.close()


if __name__ == "__main__":
    seed_folder(CSV_FOLDER)
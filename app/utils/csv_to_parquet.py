import os
import logging
from typing import Union

import pyarrow as pa
import pyarrow.csv as pv
import pyarrow.parquet as pq


logger = logging.getLogger(__name__)


def csv_to_parquet_stream(csv_path: Union[str, os.PathLike],
                          parquet_path: Union[str, os.PathLike]) -> None:
    """
    Convert a CSV file to a Parquet file using streaming batches.

    This function reads a CSV file incrementally using PyArrow's streaming
    CSV reader and writes it to a Parquet file batch-by-batch. The streaming
    approach ensures constant memory usage, making it suitable for very
    large CSV files used in enterprise ETL and data pipeline workloads.

    Parameters
    ----------
    csv_path : str or PathLike
        Path to the input CSV file.

    parquet_path : str or PathLike
        Destination path where the Parquet file will be written.

    Returns
    -------
    None
        The function writes the converted Parquet file to disk.

    Raises
    ------
    FileNotFoundError
        If the input CSV file does not exist.

    ValueError
        If the provided file paths are invalid.

    RuntimeError
        If an unexpected failure occurs during CSV reading
        or Parquet writing.

    Notes
    -----
    - Designed for large-scale CSV files that may exceed system memory.
    - Uses PyArrow's high-performance columnar processing.
    - Recommended for data engineering pipelines, analytics workloads,
      and data lake ingestion processes.

    Example
    -------
    >>> csv_to_parquet_stream("data/products.csv", "data/products.parquet")
    """

    try:
        # Validate input path
        if not csv_path:
            raise ValueError("csv_path cannot be empty.")

        if not parquet_path:
            raise ValueError("parquet_path cannot be empty.")

        if not os.path.exists(csv_path):
            raise FileNotFoundError(f"CSV file not found: {csv_path}")

        logger.info("Starting CSV to Parquet conversion")
        logger.info("Input CSV: %s", csv_path)
        logger.info("Output Parquet: %s", parquet_path)

        # Open CSV reader
        try:
            reader = pv.open_csv(csv_path)
        except Exception as exc:
            logger.exception("Failed to open CSV file.")
            raise RuntimeError(f"Unable to read CSV file: {csv_path}") from exc

        # Write Parquet in batches
        try:
            with pq.ParquetWriter(parquet_path, reader.schema) as writer:
                for batch in reader:
                    table = pa.Table.from_batches([batch])
                    writer.write_table(table)

        except Exception as exc:
            logger.exception("Failed during Parquet writing process.")
            raise RuntimeError(
                f"Error occurred while writing Parquet file: {parquet_path}"
            ) from exc

        logger.info("CSV successfully converted to Parquet.")

    except (FileNotFoundError, ValueError):
        raise

    except Exception as exc:
        logger.exception("Unexpected error during CSV to Parquet conversion.")
        raise RuntimeError(
            "Unexpected failure occurred during CSV to Parquet conversion."
        ) from exc
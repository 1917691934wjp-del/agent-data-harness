"""Load bundled JSON datasets into isolated, read-only SQLite databases."""

import json
from pathlib import Path

from langchain_community.utilities.sql_database import SQLDatabase
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool


def create_sample_database(source: str) -> SQLDatabase:
    """Import one dataset, preserving nested values as JSON and generated IDs."""
    data = json.loads(Path(source).read_text(encoding="utf-8"))
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    def quote(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    generated_ids = {
        "warehouses": "warehouse_id",
        "inventory": "inventory_id",
        "shipments": "shipment_id",
    }
    with engine.begin() as conn:
        for table, rows in data.items():
            if not rows:
                raise ValueError(f"Sample table {table} has no rows to infer a schema")
            columns = list(dict.fromkeys(key for row in rows for key in row))
            definitions = []
            id_column = generated_ids.get(table)
            if id_column and id_column not in columns:
                definitions.append(f"{quote(id_column)} INTEGER PRIMARY KEY")
            for column in columns:
                values = [row[column] for row in rows if row.get(column) is not None]
                if values and all(isinstance(v, (int, bool)) for v in values):
                    kind = "INTEGER"
                elif values and all(isinstance(v, (int, float)) for v in values):
                    kind = "REAL"
                else:
                    kind = "TEXT"
                definitions.append(f"{quote(column)} {kind}")
            conn.exec_driver_sql(
                f"CREATE TABLE {quote(table)} ({', '.join(definitions)})"
            )
            parameters = []
            for row in rows:
                values = []
                for column in columns:
                    value = row.get(column)
                    if isinstance(value, (dict, list)):
                        value = json.dumps(value, ensure_ascii=False)
                    values.append(value)
                parameters.append(tuple(values))
            conn.exec_driver_sql(
                f"INSERT INTO {quote(table)} ({', '.join(map(quote, columns))}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                parameters,
            )
        conn.exec_driver_sql("PRAGMA query_only = ON")
    return SQLDatabase(engine)

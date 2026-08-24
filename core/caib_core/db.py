import os

import psycopg


def connect(url: str | None = None, **kwargs) -> psycopg.Connection:
    return psycopg.connect(url or os.environ["CAIB_PG_URL"], **kwargs)

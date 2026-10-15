"""Check HTTP liveness and database connectivity without importing the app."""
import os
import sys
import urllib.request
import psycopg
from start import configure_database

try:
    configure_database()
    with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3) as response:
        if response.status != 200:
            raise RuntimeError("HTTP check failed")
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=3) as connection:
        connection.execute("SELECT 1")
except Exception:
    sys.exit(1)

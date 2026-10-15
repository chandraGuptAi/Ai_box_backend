"""Configure container networking without changing host development settings."""
import os
import sys
from urllib.parse import quote


def configure_database():
    if not os.environ.get("DATABASE_URL", "").strip():
        user, password, database = (
            quote(os.environ[key], safe="")
            for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
        )
        os.environ["DATABASE_URL"] = f"postgresql://{user}:{password}@postgres:5432/{database}"


if __name__ == "__main__":
    configure_database()
    os.execvp(sys.argv[1], sys.argv[1:])

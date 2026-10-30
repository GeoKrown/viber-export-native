"""Initialize the collector's local database."""
from contextlib import closing
from pathlib import Path
import sqlite3

DEFAULT_PATH = Path.cwd() / 'data' / 'viber.sqlite'


def initialize(path=DEFAULT_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as con, con:
        version = con.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            raise ValueError('unsupported_storage_version')
        con.execute('CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, name TEXT, kind TEXT, status TEXT, notes TEXT)')
        con.execute('PRAGMA user_version=1')

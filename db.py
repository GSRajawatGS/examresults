"""
db.py - the connection to Supabase (used by db_sync.py and app.py).

Put two lines in a file called .env next to these scripts:
    SUPABASE_URL=https://xxxxxxxx.supabase.co
    SUPABASE_KEY=<the service_role key: Supabase -> Project Settings -> API>
Keep that file private (do not share it, do not upload it): the service_role key can read and change
every table. The scripts and the app run on your PC, so the key never goes to a web page.

    pip install supabase python-dotenv
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

TABLES = ["attestation_students", "students", "marksheets", "marksheet_papers", "sitting_plans"]


def configured():
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"))


def connect():
    if not configured():
        raise RuntimeError("SUPABASE_URL / SUPABASE_KEY are not set (see the top of db.py).")
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def upsert(client, table, rows, on_conflict, chunk=400, log=None):
    """Insert-or-update rows (a row with the same on_conflict columns is replaced). Returns the row count."""
    n = 0
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        client.table(table).upsert(part, on_conflict=on_conflict).execute()
        n += len(part)
        if log:
            log(f"      {table}: {n}/{len(rows)}")
    return n


def fetch_all(client, table, columns="*", eq=None, page=1000):
    """Every row of a table / view (Supabase returns at most 1000 rows per request, so this pages).
    eq = {"session": "JUN-2026"} keeps only matching rows."""
    out, start = [], 0
    while True:
        q = client.table(table).select(columns)
        for k, v in (eq or {}).items():
            q = q.eq(k, v)
        data = q.range(start, start + page - 1).execute().data or []
        out.extend(data)
        if len(data) < page:
            return out
        start += page


def count(client, table):
    res = client.table(table).select("id", count="exact").limit(1).execute()
    return res.count or 0

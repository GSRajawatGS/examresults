"""
run_adhoc.py - run ONE mponline job that is not in jobs.py, then (optionally) save the data in Supabase.
The app's Download page uses this.

    python run_adhoc.py job.json

job.json holds the same fields as an entry of JOBS in jobs.py, for example
    {"name": "adhoc", "task": "attestation", "href": "rptATTEST_SSRS_ExmCnt.aspx",
     "session": "JUN-2026", "course": ["LLB(L.L.B.)", "MA(M.A.)"], "tag": "", "sync": true}
"sync": true  -> after the download, the PDFs of that session are read into the Supabase tables
                 (attestation_students for attestation sheets, sitting_plans for sitting plans).
Keep next to mponline.py.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

import mponline as mp


def save_to_supabase(job, sessions):
    import db
    import db_sync
    import result_report as rr
    if not db.configured():
        print("\nSupabase is not set up (SUPABASE_URL / SUPABASE_KEY in .env): the data stays in the PDFs.")
        return
    docs = Path(rr.DOCS_ROOT) if rr.DOCS_ROOT else rr.documents_dir()
    client = db.connect()
    print("\nSaving the data in Supabase ...")
    for session in sessions:
        if job.get("task") == "attestation":
            db_sync.sync_attestation(client, docs, session)
        else:
            db_sync.sync_sitting(client, docs, session)
    print("Saved to Supabase.")


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python run_adhoc.py job.json")
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    job.setdefault("name", "adhoc")
    job.setdefault("tag", "")
    sessions = job["session"] if isinstance(job["session"], list) else [job["session"]]
    with sync_playwright() as p:
        for j in mp.expand([{**job, "run": True}], [job["name"]]):      # a list of sessions / courses is fine
            mp.run_job(p, j)
    if job.get("sync"):
        try:
            save_to_supabase(job, sessions)
        except Exception as e:
            print(f"\nSaving to Supabase failed ({type(e).__name__}: {str(e)[:150]}). "
                  f"The PDFs are safe; use 'Sync to Supabase' on the app's Run tasks page later.")


if __name__ == "__main__":
    main()

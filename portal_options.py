"""
portal_options.py - asks the portal which sessions (and, for one session, which courses) a report page offers,
and saves the answer in portal_options.json. The app's Download page uses it to fill its drop-downs.

    python portal_options.py attestation|normal              the sessions of that page
    python portal_options.py attestation|normal JUN-2026     ... and the courses offered for JUN-2026

Keys: attestation|normal  attestation|nep  sitting|normal  sitting|nep  sitting|nep_rollwise
Keep next to mponline.py.
"""
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import mponline as mp

BASE = Path(__file__).resolve().parent
CACHE = BASE / "portal_options.json"
HREFS = {
    "attestation|normal": "rptATTEST_SSRS_ExmCnt.aspx",
    "attestation|nep": "rptATTEST_SSRS_examcenter_NEP.aspx",
    "sitting|normal": "DocketReport_ExamCenter.aspx",
    "sitting|nep": "DocketReport_ExamCenter_NEP.aspx",
    "sitting|nep_rollwise": "DocketReport_ExamCenter_NEP_RollNo_Wise.aspx",
}


def load():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(data):
    CACHE.write_text(json.dumps(data, indent=1), encoding="utf-8")


def session_key(s):
    m = re.fullmatch(r"([A-Za-z]{3})-(\d{4})", s.strip())
    return (int(m.group(2)), mp.MONTHS.index(m.group(1).upper()) if m.group(1).upper() in mp.MONTHS else 0) if m else (0, 0)


def real(texts):
    """Option texts without the placeholders ('-- Select --', '--ALL--' ...)."""
    return [t for t in texts if t and not t.startswith("-") and not t.lower().startswith("select")]


def read(key, session=None):
    """(sessions, courses of `session`) as the portal shows them now."""
    href = HREFS[key]
    with sync_playwright() as p:
        portal = mp.Portal(p)
        try:
            page = mp.open_report_link(portal.fresh_page(), {"href": href})
            page.wait_for_timeout(1200)
            try:
                mp.dismiss_message(page)                # the sitting-plan pages open with a message box
            except Exception:
                pass
            sessions = real([t for _, t in mp.real_options(page, "Select Session")])
            courses = None
            if session:
                mp.select_row(page, "Select Session", session, quiet=True)
                sel = mp.get_dropdown(page, "Select Course")
                for _ in range(20):                      # the course list fills after the session is chosen
                    if sel.locator("option").count() > 1:
                        break
                    page.wait_for_timeout(500)
                courses = real([t.strip() for t in sel.locator("option").all_inner_texts()])
            return sessions, courses
        finally:
            portal.close()


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in HREFS:
        sys.exit(f"Usage: python portal_options.py {'|'.join(HREFS)} [SESSION]")
    key = sys.argv[1]
    session = sys.argv[2] if len(sys.argv) > 2 else None
    print(f"Reading {key}" + (f" for {session}" if session else "") + " from the portal ...")
    sessions, courses = read(key, session)
    sessions = sorted(sessions, key=session_key, reverse=True)
    data = load()
    entry = data.setdefault(key, {"sessions": [], "courses": {}})
    entry["sessions"], entry["sessions_at"] = sessions, time.time()
    print(f"   {len(sessions)} session(s): {', '.join(sessions[:12])}{' ...' if len(sessions) > 12 else ''}")
    if session:
        entry["courses"][session] = courses or []
        print(f"   {len(courses or [])} course(s) for {session}: {courses}")
    save(data)
    print("Saved to", CACHE)


if __name__ == "__main__":
    main()

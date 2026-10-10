import os
from dotenv import load_dotenv
load_dotenv()
USER_ID  = os.environ["PORTAL_USER_ID"]
PASSWORD = os.environ["PORTAL_PASSWORD"]
# jobs.py - the only file you need to edit. Lines starting with # are comments.
# Keep it in the same folder as mponline.py.

# ---- Login --------------------------------------------------------------
LOGIN_URL = "https://jiwaji.mponline.gov.in/Portal/ServicesIndex/frmLogin.aspx"
CLUSTER   = "G2G"


# ---- Options ------------------------------------------------------------
TEST_ONE = False      # True = attestation saves ONE file, then pauses (use for a first test)
MIN_PDF_KB = 5        # attestation PDFs smaller than this are treated as "no data"
INCLUDE_ALL = True   # True = also loop the "--ALL--" option of each dropdown
DEBUG = False         # True = print browser / tab events

# ---- Backfill: attestation sheets for every session / course not downloaded yet ----------
#   python mponline.py backfill            do it (finished work is remembered, so reruns skip it)
#   python mponline.py backfill --plan     show what is still to do, download nothing
#   python mponline.py backfill --again    check everything again (existing PDFs are not downloaded twice)
#
#  name     any label (python mponline.py backfill NAME runs only this group)
#  href     part of the report link's URL (same as in JOBS)
#  months   which sessions: the three-letter month of the session, e.g. ["JUN", "DEC"]
#  courses  short names are enough ("LLB", "BA"); each is matched to the page's exact spelling
#           ("LLB(L.L.B.)", "BA - B.A.") separately for every session. A course the session does not
#           offer is noted and skipped.
#  tag      "NEP" for the NEP page (files go to the NEP folders), "" for the normal page
#  levels   optional, as in JOBS ("auto" or a list of dropdown labels)
#  min_year / max_year   optional limits, e.g. "min_year": 2018
#
# KEEP THIS LIST ABOVE "JOBS": agent_runner.py adds its jobs at the end of the last list in this file.
BACKFILL = [
    {
        "name": "backfill-nonnep",
        "task": "attestation",
        "href": "rptATTEST_SSRS_ExmCnt.aspx",
        "months": ["JUN", "DEC"],
        "courses": ["LLB", "MA", "MSC", "MCOM", "MBA", "BED"],
        "tag": "",
    },
    {
        "name": "backfill-nep",
        "task": "attestation",
        "href": "rptATTEST_SSRS_examcenter_NEP.aspx",
        "months": ["MAR"],
        "courses": ["BA", "BSC", "BCOM"],
        "tag": "NEP",
        "levels": "auto",
    },
]
BACKFILL_RECHECK_DAYS = 7     # a session that had no data / did not offer the course is looked at again after this

# ---- Jobs ---------------------------------------------------------------
#  name     any label. Run just this job with:  python mponline.py NAME   (even if run is False)
#  task     "sitting" or "attestation"
#  href     part of the link's URL (find them with:  python mponline.py links)
#  text     alternative to href: the link's exact visible text
#  session  exactly as listed in the dropdown, e.g. "JUN-2026"   (or a list of sessions)
#  course   exactly as listed in the dropdown (or a list of courses). The sitting page and the
#           attestation page spell them differently:  "LLB - L.L.B."  vs  "LLB(L.L.B.)"
#  tag      optional folder suffix so NEP and normal files never mix, e.g. "NEP"
#  levels   optional: dropdown labels to loop over, in the order they appear on the page
#  run      True / False

JOBS = [
    {
        "name": "sitting-LLB",
        "task": "sitting",
        "href": "DocketReport_ExamCenter.aspx",
        "session": "JUN-2026",
        "course": "LLB - L.L.B.",
        "tag": "",
        "run": False,
    },
    {
        "name": "attestation-LLB",
        "task": "attestation",
        "href": "rptATTEST_SSRS_ExmCnt.aspx",
        "session": "JUN-2026",
        "course": "LLB(L.L.B.)",
        "tag": "",
        "run": False,
    },

    # --- NEP: fill in the course names exactly as listed, then set run to True ---
    {
        "name": "sitting-NEP",
        "task": "sitting",
        "href": "DocketReport_ExamCenter_NEP.aspx",
        "session": "MAR-2026",
        "course": ["BA(B.A.)", "BSC(B.Sc.)", "BCOM(B.Com.)"],     # or a list: ["BA - B.A.", "BSC - B.Sc."]
        "tag": "NEP",
        "run": False,
    },
    {
        "name": "sitting-NEP-rollwise",
        "task": "sitting",
        "href": "DocketReport_ExamCenter_NEP_RollNo_Wise.aspx",
        "session": "MAR-2026",
        "course": ["BA(B.A.)", "BSC(B.Sc.)", "BCOM(B.Com.)"],
        "tag": "NEP_ROLLWISE",
        "run": False,
    },
    {
        "name": "attestation-NEP",
        "task": "attestation",
        "href": "rptATTEST_SSRS_examcenter_NEP.aspx",
        "session": "MAR-2026",
        "course": ['BA - B.A.', 'BSC - B.Sc.', 'BCOM - BCOM'],
        "tag": "NEP",
        "levels": "auto",
        "run": True,
    },
    {
        "name": "agent-job",
        "task": "attestation",
        "href": "rptATTEST_SSRS_ExmCnt.aspx",
        "session": "JUN-2018",
        "course": ['LLB(L.L.B.)'],
        "tag": "",
        "run": True,
    },
]
"""
app.py - one screen for all the work: download attestation / sitting PDFs, fetch marksheets, look at the
results and statistics, check what is on disk, keep Supabase up to date.

    pip install streamlit pandas supabase python-dotenv pdfplumber openpyxl playwright
    streamlit run app.py

Keep it in the same folder as mponline.py, result_scraper.py, result_report.py, db.py, db_sync.py ...
It runs on THIS PC (the scripts open a browser here). Open it at http://localhost:8501.
To protect the student data put  APP_PASSWORD=something  in the .env file: the app then asks for it.
"""
import json
import os
import runpy
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import analytics as an          # noqa: E402
import db                       # noqa: E402
import result_report as rr      # noqa: E402
from runner import Runner, python_cmd   # noqa: E402

KNOWN_LINKS = {
    "Attestation sheet": ("attestation", "rptATTEST_SSRS_ExmCnt.aspx"),
    "Attestation sheet - NEP": ("attestation", "rptATTEST_SSRS_examcenter_NEP.aspx"),
    "Sitting plan": ("sitting", "DocketReport_ExamCenter.aspx"),
    "Sitting plan - NEP": ("sitting", "DocketReport_ExamCenter_NEP.aspx"),
    "Sitting plan - NEP roll-wise": ("sitting", "DocketReport_ExamCenter_NEP_RollNo_Wise.aspx"),
}
REQUIRED_FILES = ["mponline.py", "jobs.py", "result_scraper.py", "result_jobs.py", "result_report.py", "analytics.py",
                  "db.py", "db_sync.py", "runner.py", "run_adhoc.py", "portal_options.py", "supabase_schema.sql"]
PAGES = ["Download", "Run tasks", "Results", "Student search", "Statistics", "Attestation data", "Sitting plans", "Files & backfill",
         "Database"]

# (what, normal / NEP) -> (task, key in portal_options.py, link of the portal page, folder tag, levels)
DL_TYPES = {
    ("Attestation sheet", "Normal (non-NEP)"): ("attestation", "attestation|normal", "rptATTEST_SSRS_ExmCnt.aspx", "", None),
    ("Attestation sheet", "NEP"): ("attestation", "attestation|nep", "rptATTEST_SSRS_examcenter_NEP.aspx", "NEP", "auto"),
    ("Sitting plan", "Normal (non-NEP)"): ("sitting", "sitting|normal", "DocketReport_ExamCenter.aspx", "", None),
    ("Sitting plan", "NEP"): ("sitting", "sitting|nep", "DocketReport_ExamCenter_NEP.aspx", "NEP", "auto"),
    ("Sitting plan", "NEP - roll number wise"): ("sitting", "sitting|nep_rollwise",
                                                  "DocketReport_ExamCenter_NEP_RollNo_Wise.aspx", "NEP_ROLLWISE", "auto"),
}


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
@st.cache_resource
def get_runner():
    return Runner(BASE / "app_logs")


def docs_folder():
    custom = st.session_state.get("docs_override") or ""
    if custom.strip():
        return Path(custom.strip())
    return Path(rr.DOCS_ROOT) if rr.DOCS_ROOT else rr.documents_dir()


def read_jobs():
    """JOBS and BACKFILL from jobs.py (its .env is loaded first)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE / ".env")
    except Exception:
        pass
    try:
        ns = runpy.run_path(str(BASE / "jobs.py"))
        return list(ns.get("JOBS", [])), list(ns.get("BACKFILL", []))
    except Exception as e:
        st.warning(f"jobs.py could not be read ({type(e).__name__}: {str(e)[:120]}).")
        return [], []


@st.cache_data(ttl=600, show_spinner="Loading the data ...")
def load_data(source, docs):
    frames = an.db_frames(db.connect()) if source == "Supabase" else an.local_frames(docs)
    return frames, an.prepare(an.merge_results(frames))


@st.cache_data(ttl=600, show_spinner="Reading the attestation PDFs (the first time this takes a while) ...")
def load_attestation(source, docs):
    return an.db_attestation(db.connect()) if source == "Supabase" else an.local_attestation(docs)


@st.cache_data(ttl=600, show_spinner="Reading the sitting-plan PDFs (the first time this takes a while) ...")
def load_sitting(source, docs):
    return an.db_sitting(db.connect()) if source == "Supabase" else an.local_sitting(docs)


def need_data(source):
    """(frames, df) or (None, None) after showing why there is nothing to show."""
    try:
        frames, df = load_data(source, str(docs_folder()))
    except Exception as e:
        st.error(f"Could not load the data from {source}: {type(e).__name__}: {str(e)[:200]}")
        return None, None
    if df.empty:
        st.info("No student data yet. Run 'Marksheets' on the Run tasks page"
                + (", then 'Sync to Supabase' on the Database page." if source == "Supabase" else "."))
        return None, None
    return frames, df


def sessions_of(df):
    return sorted(df["session"].dropna().unique(), key=an.session_sort_key, reverse=True)


def pick_class(df, key=""):
    """Session + class selectors; returns (session, code, rows of that class)."""
    c1, c2 = st.columns(2)
    session = c1.selectbox("Session", sessions_of(df), key=f"{key}_session")
    codes = sorted(df.loc[df["session"] == session, "exam_code"].dropna().unique())
    code = c2.selectbox("Class (exam code)", codes, key=f"{key}_code")
    return session, code, df[(df["session"] == session) & (df["exam_code"] == code)]


def missing_files():
    return [f for f in REQUIRED_FILES if not (BASE / f).exists()]


def start_task(runner, cmd, title, stdin_text=None):
    """Start a script in the background - or say clearly which file is missing."""
    script = Path(cmd[1])
    if not script.exists():
        st.error(f"The file {script.name} is not in {BASE}. Copy it next to app.py and try again.")
        return False
    runner.start(cmd, BASE, title=title, stdin_text=stdin_text)
    st.rerun()
    return True


def csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8-sig")        # opens correctly in Excel


# ----------------------------------------------------------------------------
# Page: Download (type -> session -> courses -> download -> Supabase)
# ----------------------------------------------------------------------------
def load_portal_options():
    try:
        return json.loads((BASE / "portal_options.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def page_download():
    st.header("Download attestation sheets / sitting plans")
    runner = get_runner()
    busy = runner.running()
    if busy:
        st.warning("A task is running - the buttons come back when it has finished (or press Stop).")

    c1, c2 = st.columns(2)
    what = c1.radio("1. What do you want?", ["Attestation sheet", "Sitting plan"], horizontal=True)
    variants = ["Normal (non-NEP)", "NEP"] + (["NEP - roll number wise"] if what == "Sitting plan" else [])
    variant = c2.radio("2. Normal or NEP?", variants, horizontal=True)
    task, key, href, tag, levels = DL_TYPES[(what, variant)]
    st.caption(f"Portal page: {href}")
    opts = load_portal_options().get(key, {})

    def ask_portal(*args, title):
        start_task(runner, python_cmd(BASE / "portal_options.py", key, *args), title)

    st.markdown("**3. Session**")
    sessions = opts.get("sessions", [])
    c1, c2 = st.columns([3, 1])
    session = None
    if sessions:
        session = c1.selectbox("Session-year", sessions, key=f"dl_session_{key}")
    else:
        c1.info("The sessions of this page have not been read from the portal yet.")
    if c2.button("Read the sessions from the portal" if not sessions else "Refresh the sessions", disabled=busy):
        ask_portal(title=f"read sessions: {what} {variant}")

    if session:
        st.markdown(f"**4. Courses offered for {session}**")
        offered = opts.get("courses", {}).get(session)
        if offered is None:
            st.info(f"The courses of {session} have not been read from the portal yet.")
            if st.button(f"Read the courses of {session}", disabled=busy):
                ask_portal(session, title=f"read courses: {what} {variant} {session}")
        else:
            chosen = st.multiselect("Courses (one or more)", offered, default=offered, key=f"dl_courses_{key}_{session}")
            can_sync = db.configured()
            save_db = st.checkbox("Also save the data in Supabase when the download is finished",
                                  value=can_sync, disabled=not can_sync, key=f"dl_db_{key}")
            if not can_sync:
                st.caption("Supabase is not connected yet (see the Database page): the data stays in the PDFs.")
            c1, c2 = st.columns([3, 1])
            if c1.button(f"Download {len(chosen)} course(s) of {session}", disabled=busy or not chosen):
                job = {"name": "download", "task": task, "href": href, "tag": tag, "session": [session],
                       "course": list(chosen), "sync": bool(save_db and can_sync)}
                if levels:
                    job["levels"] = levels
                f = BASE / "app_logs" / f"download_{datetime.now():%Y%m%d_%H%M%S}.json"
                f.parent.mkdir(exist_ok=True)
                f.write_text(json.dumps(job, indent=1), encoding="utf-8")
                start_task(runner, python_cmd(BASE / "run_adhoc.py", str(f)),
                           f"download {what} {variant} {session}: {', '.join(chosen)}")
            if c2.button("Read the courses again", disabled=busy):
                ask_portal(session, title=f"read courses: {what} {variant} {session}")
            st.caption("PDFs that are already on this PC are skipped, so a repeated run only fetches what is missing.")
    live_log(runner)


# ----------------------------------------------------------------------------
# Page: Attestation data
# ----------------------------------------------------------------------------
def page_attestation(source):
    st.header("Attestation data")
    try:
        att = load_attestation(source, str(docs_folder()))
    except Exception as e:
        st.error(f"Could not load the attestation data: {type(e).__name__}: {str(e)[:200]}")
        return
    if att.empty:
        st.info("No attestation data yet" + (" in Supabase. Download some on the Download page with 'save in Supabase' ticked, "
                                              "or press 'Sync to Supabase'." if source == "Supabase" else "."))
        return
    c = st.columns(4)
    session = c[0].selectbox("Session", ["(all)"] + sessions_of(att))
    kind = c[1].selectbox("Normal / NEP", ["(all)", "Normal", "NEP"])
    d = att if session == "(all)" else att[att["session"] == session]
    if kind != "(all)":
        d = d[d["tag"].fillna("").eq("") if kind == "Normal" else d["tag"].fillna("").ne("")]
    courses = sorted(d["course"].dropna().unique())
    course = c[2].selectbox("Course", ["(all)"] + courses)
    if course != "(all)":
        d = d[d["course"] == course]
    status = c[3].selectbox("Status", ["(all)"] + sorted(d["status"].dropna().unique()))
    if status != "(all)":
        d = d[d["status"] == status]

    st.subheader(f"{len(d)} students")
    summary = (d.groupby(["session", "tag", "course", "sem", "regular_private", "status"], dropna=False)
               .size().reset_index(name="students").sort_values(["session", "course", "sem"], ascending=[False, True, True]))
    st.dataframe(summary, hide_index=True)

    text = st.text_input("Search roll number / name / father's name / enrollment", "")
    v = d
    if text.strip():
        t = text.strip().lower()
        v = d[d[["roll_no", "name", "father_name", "enrollment_no"]].fillna("").astype(str).apply(
            lambda col: col.str.lower().str.contains(t, regex=False)).any(axis=1)]
    cols = ["session", "tag", "roll_no", "name", "father_name", "mother_name", "gender", "category", "course", "branch",
            "sem", "status", "enrollment_no", "exam_centre", "address"]
    table = v[[x for x in cols if x in v]].sort_values(["session", "roll_no"])
    st.dataframe(table, hide_index=True)
    st.download_button("Download this table (CSV)", csv_bytes(table), file_name="attestation_students.csv")


# ----------------------------------------------------------------------------
# Page: Run tasks
# ----------------------------------------------------------------------------
def live_log(runner):
    st.divider()
    c1, c2 = st.columns([4, 1])
    c1.markdown(f"**Task:** {runner.title or '-'}   |   **Status:** {runner.status()}"
                + (f"   |   {runner.elapsed()} s" if runner.running() else ""))
    if runner.running() and c2.button("Stop the task"):
        runner.stop()
        st.rerun()
    if runner.log:
        st.code(runner.tail(60) or "(no output yet)", language=None)
    hist = runner.history(8)
    if hist:
        with st.expander("Earlier runs (log files are in the app_logs folder)"):
            st.table(pd.DataFrame(hist, columns=["log file", "time"]))
    if runner.running():                                    # keep the log fresh while it runs
        time.sleep(2)
        st.rerun()


def page_run():
    st.header("Run tasks")
    runner = get_runner()
    busy = runner.running()
    if busy:
        st.warning("A task is running. The buttons come back when it has finished (or press Stop). "
                   "Only one task can run at a time: they share one portal login and one browser.")

    def go(cmd, title, stdin_text=None):
        start_task(runner, cmd, title, stdin_text)

    jobs, backfill = read_jobs()
    t1, t2, t3 = st.tabs(["1. Attestation / sitting download", "2. Marksheets", "3. Reports & database"])

    with t1:
        st.subheader("A job from jobs.py")
        names = [j.get("name", "?") for j in jobs]
        if names:
            name = st.selectbox("Job", names)
            job = next(j for j in jobs if j.get("name") == name)
            st.caption(f"{job.get('task')} | {job.get('session')} | {job.get('course')} | "
                       f"{'NEP' if job.get('tag') else 'normal'}")
            if st.button("Run this job", disabled=busy):
                go(python_cmd(BASE / "mponline.py", name), f"mponline: {name}")
        else:
            st.info("No jobs found in jobs.py.")

        st.subheader("A one-off job")
        st.caption("Course names exactly as the portal lists them, e.g. LLB(L.L.B.) on the attestation page, "
                   "'LLB - L.L.B.' on the sitting-plan page, 'BA - B.A.' for NEP.")
        with st.form("adhoc"):
            link = st.selectbox("Report", list(KNOWN_LINKS))
            c1, c2, c3 = st.columns(3)
            session = c1.text_input("Session(s), comma separated", "JUN-2026")
            courses = c2.text_input("Course(s), comma separated", "LLB(L.L.B.)")
            tag = c3.text_input("Folder tag (NEP for NEP pages)", "")
            submitted = st.form_submit_button("Run once", disabled=busy)
        if submitted:
            task, href = KNOWN_LINKS[link]
            job = {"name": "adhoc", "task": task, "href": href, "tag": tag.strip(),
                   "session": [s.strip() for s in session.split(",") if s.strip()],
                   "course": [c.strip() for c in courses.split(",") if c.strip()]}
            if task == "attestation" and "NEP" in link:
                job["levels"] = "auto"
            f = BASE / "app_logs" / f"adhoc_{datetime.now():%Y%m%d_%H%M%S}.json"
            f.parent.mkdir(exist_ok=True)
            f.write_text(json.dumps(job, indent=1), encoding="utf-8")
            go(python_cmd(BASE / "run_adhoc.py", str(f)), f"mponline one-off: {link} {session} {courses}")

        st.subheader("Download everything not downloaded yet (backfill)")
        if backfill:
            st.caption("Groups from jobs.py: " + "; ".join(
                f"{g.get('name')} ({'/'.join(g.get('months', []))}: {', '.join(g.get('courses', []))})" for g in backfill))
        c1, c2, c3 = st.columns(3)
        if c1.button("Plan only (downloads nothing)", disabled=busy):
            go(python_cmd(BASE / "mponline.py", "backfill", "--plan"), "backfill plan")
        if c2.button("Run backfill", disabled=busy):
            go(python_cmd(BASE / "mponline.py", "backfill"), "backfill")
        if c3.button("Check everything again", disabled=busy):
            go(python_cmd(BASE / "mponline.py", "backfill", "--again"), "backfill again")

    with t2:
        st.subheader("New attestation PDFs -> marksheets (automatic)")
        st.caption("Looks in the Documents folder for session folders, reads new attestation PDFs into Excel, "
                   "then fetches each roll number's marksheet from the result site "
                   "(ATKT: also the II-chance exams).")
        c1, c2 = st.columns(2)
        if c1.button("Fetch marksheets (auto)", disabled=busy):
            go(python_cmd(BASE / "result_scraper.py", "auto"), "results: auto")
        if c2.button("Auto, and look again at 'not found' roll numbers", disabled=busy):
            go(python_cmd(BASE / "result_scraper.py", "auto", "retry"), "results: auto retry")
        st.subheader("One class")
        cls = st.text_input("Class, e.g. llb 3rd sem atkt  /  llb 5 sem  /  ALL323", "")
        c1, c2 = st.columns(2)
        if c1.button("Run this class", disabled=busy or not cls.strip()):
            go(python_cmd(BASE / "result_scraper.py"), f"results: {cls}", stdin_text=cls.strip())
        if c2.button("Test: 3 roll numbers only", disabled=busy or not cls.strip()):
            go(python_cmd(BASE / "result_scraper.py", "test"), f"results test: {cls}", stdin_text=cls.strip())
        st.caption("These use the session and folders in result_jobs.py. The class is typed into the script for you.")

    with t3:
        st.subheader("Excel reports and statistics")
        if st.button("Make the reports", disabled=busy):
            go(python_cmd(BASE / "result_report.py"), "reports")
        st.subheader("Supabase")
        if not db.configured():
            st.info("Supabase is not set up yet - see the Database page.")
        c1, c2 = st.columns(2)
        if c1.button("Sync to Supabase (results + sitting plans)", disabled=busy or not db.configured()):
            go(python_cmd(BASE / "db_sync.py"), "sync to Supabase")
        if c2.button("Sync results only", disabled=busy or not db.configured()):
            go(python_cmd(BASE / "db_sync.py", "results"), "sync results")

    live_log(runner)


# ----------------------------------------------------------------------------
# Page: Results
# ----------------------------------------------------------------------------
def page_results(source):
    st.header("Results")
    frames, df = need_data(source)
    if df is None:
        return
    session, code, d = pick_class(df, "res")
    o = an.overview(d)
    c = st.columns(5)
    c[0].metric("In attestation list", o["listed"])
    c[1].metric("Result found", o["appeared"])
    c[2].metric("Passed", o["passed"])
    c[3].metric("Pass %", f"{o['pass_pct']}%")
    c[4].metric("No result yet", o["not_found"] + o["pending"])

    c1, c2 = st.columns([2, 3])
    show = c1.multiselect("Show", ["Passed", "Not passed", "No result"], default=["Passed", "Not passed", "No result"])
    text = c2.text_input("Search roll number / name / father's name", "")
    mask = pd.Series(False, index=d.index)
    if "Passed" in show:
        mask |= d["passed"]
    if "Not passed" in show:
        mask |= d["appeared"] & ~d["passed"]
    if "No result" in show:
        mask |= ~d["appeared"]
    v = d[mask]
    if text.strip():
        t = text.strip().lower()
        v = v[v[["roll_no", "name", "father_name"]].fillna("").astype(str).apply(
            lambda col: col.str.lower().str.contains(t, regex=False)).any(axis=1)]
    cols = ["roll_no", "name", "father_name", "gender", "category", "att_status", "final_result", "total_obtained",
            "percentage", "division", "result_exam_code", "result_status"]
    table = v[[c_ for c_ in cols if c_ in v]].sort_values("roll_no")
    st.dataframe(table, hide_index=True)
    st.download_button("Download this table (CSV)", csv_bytes(table), file_name=f"{session}_{code}_results.csv")

    st.subheader("One student")
    rolls = list(table["roll_no"])
    roll = st.selectbox("Roll number", rolls) if rolls else None
    if roll:
        att, ms, papers = an.student_card(frames, session, code, roll)
        a, b = st.columns(2)
        a.markdown("**Attestation sheet**")
        a.table(pd.DataFrame({"field": list(att), "value": [str(x) for x in att.values()]}))
        b.markdown("**Marksheet**")
        if ms:
            b.table(pd.DataFrame({"field": list(ms), "value": [str(x) for x in ms.values()]}))
        else:
            b.info("No marksheet found for this student yet.")
        if len(papers):
            st.markdown("**Papers**")
            p = papers[["paper", "components", "total"]].copy()
            p["components"] = p["components"].map(lambda x: ", ".join(f"{k}: {v}" for k, v in (x or {}).items()))
            st.dataframe(p, hide_index=True)
        hist = df[df["roll_no"] == str(roll)][["session", "exam_code", "class_label", "final_result", "percentage"]]
        if len(hist) > 1:
            st.markdown("**Every class this roll number appears in**")
            st.dataframe(hist.sort_values("session"), hide_index=True)


# ----------------------------------------------------------------------------
# Page: Student search (one roll number -> every exam, in detail)
# ----------------------------------------------------------------------------
def page_student(source):
    st.header("Student search")
    c1, c2 = st.columns([2, 3])
    q = c1.text_input("Roll number (or enrollment number)", key="student_query").strip()
    link = c2.checkbox("Also include this student's exams under other roll numbers (same enrollment number)", value=True)
    extras_on = st.checkbox("Also look in the attestation PDFs and the sitting plans", value=True)
    if not q:
        st.info("Type a roll number (or an enrollment number) to see every exam of that student: result, marks of "
                "every paper, attestation sheet and sitting plan.")
        return
    frames, df = need_data(source)
    if frames is None:
        return

    att = sit = None
    if extras_on:
        try:
            att = load_attestation(source, str(docs_folder()))
            sit = load_sitting(source, str(docs_folder()))
        except Exception as e:
            st.caption(f"(attestation PDFs / sitting plans not available: {type(e).__name__})")

    res = an.find_student(frames, q, link)
    if not res["found"]:
        st.warning(f"No exam with the roll number / enrollment number {q} was found in the results data.")
        if att is not None and not att.empty:
            hit = att[att["roll_no"].astype(str) == q]
            if len(hit):
                st.info("It is in the attestation PDFs, but no results have been fetched for it yet:")
                st.dataframe(hit[["session", "tag", "roll_no", "name", "course", "branch", "sem", "status"]],
                             hide_index=True)
        return

    extras = an.student_extras(res, att, sit)
    ex = res["exams"]
    p = res["profile"]
    st.subheader(p.get("name", q))
    a, b = st.columns([3, 2])
    a.table(pd.DataFrame({"field": list(p), "value": [str(v) for v in p.values()]}))
    passed = int(ex["division"].ne("").sum()) if "division" in ex else 0
    found = int(ex["result_status"].eq("OK").sum()) if "result_status" in ex else 0
    b.metric("Exams", len(ex))
    b.metric("Results found", found)
    b.metric("Passed (with percentage)", passed)
    b.markdown("**Roll numbers:** " + ", ".join(res["rolls"]))
    if res["linked"]:
        st.info("This student has more than one roll number (same enrollment number); all their exams are shown.")
    if len(res["names"]) > 1:
        st.warning("The name is spelled differently in different exams: " + "; ".join(res["names"]))

    st.markdown("**All exams**")
    st.dataframe(ex, hide_index=True)

    st.markdown("**Every exam in detail**")
    for i, d in enumerate(res["details"]):
        k = d["key"]
        title = f"{k['session']}  |  {d.get('class_label') or k['exam_code']}  |  roll {k['roll_no']}  |  " \
                f"{d['result'] if isinstance(d['result'], str) else (d['status'] or 'no result yet')}"
        with st.expander(title, expanded=(i == 0)):
            if d["marksheet"]:
                st.table(pd.DataFrame({"field": list(d["marksheet"]), "value": [str(v) for v in d["marksheet"].values()]}))
            else:
                st.caption("No marksheet fetched for this exam yet.")
            if len(d["papers"]):
                st.dataframe(d["papers"], hide_index=True)

    if len(extras["attestation"]):
        st.markdown("**Attestation sheets (PDFs)**")
        st.dataframe(extras["attestation"], hide_index=True)
    if len(extras["sitting"]):
        st.markdown("**Sitting plans**")
        st.dataframe(extras["sitting"], hide_index=True)
    st.download_button("Download everything about this student (Excel)", an.student_workbook(res, extras),
                       file_name=f"student_{q}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ----------------------------------------------------------------------------
# Page: Statistics
# ----------------------------------------------------------------------------
def page_stats(source):
    st.header("Statistics")
    frames, df = need_data(source)
    if df is None:
        return
    summ = an.class_summary(df)
    tab1, tab2, tab3 = st.tabs(["One class", "All classes", "Trend over sessions"])

    with tab1:
        session, code, d = pick_class(df, "stat")
        o = an.overview(d)
        c = st.columns(6)
        c[0].metric("Appeared", o["appeared"])
        c[1].metric("Passed", o["passed"])
        c[2].metric("Not passed", o["not_passed"])
        c[3].metric("Pass %", f"{o['pass_pct']}%")
        c[4].metric("Average %", o["avg_pct"] if o["avg_pct"] is not None else "-")
        c[5].metric("Highest marks", o["highest"] if o["highest"] is not None else "-")

        a, b = st.columns(2)
        dv = an.division_counts(d)
        a.markdown("**Division-wise (passed students, worked out from the marks)**")
        a.bar_chart(dv.set_index("division")["students"])
        bc = an.band_counts(d)
        b.markdown("**Marks distribution**")
        b.bar_chart(bc.set_index("marks band")["students"])

        for title, col in (("Gender-wise", "gender"), ("Category-wise", "category"), ("Status", "att_status"),
                           ("Regular / Private", "att_regular_private")):
            g = an.group_stats(d, col)
            if len(g) >= 2 or col == "gender":
                x, y = st.columns([2, 3])
                x.markdown(f"**{title}**")
                x.dataframe(g, hide_index=True)
                y.bar_chart(g.set_index(col)["pass_pct"])
        st.markdown("**Result as printed on the marksheet**")
        st.dataframe(an.result_counts(d), hide_index=True)
        st.markdown("**Toppers**")
        st.dataframe(an.toppers(d, 10), hide_index=True)
        ps = an.paper_stats(frames["papers"], d)
        if len(ps):
            st.markdown("**Paper-wise**")
            st.dataframe(ps, hide_index=True)

    with tab2:
        st.dataframe(summ, hide_index=True)
        st.download_button("Download (CSV)", csv_bytes(summ), file_name="all_classes.csv")

    with tab3:
        tr = an.trend(summ)
        if tr.empty:
            st.info("Not enough sessions yet.")
        else:
            st.caption("Pass % of the same class in every session.")
            st.dataframe(tr.round(1))
            fams = st.multiselect("Classes in the chart", list(tr.index), default=list(tr.index)[:5])
            if fams:
                st.line_chart(tr.loc[fams].T)


# ----------------------------------------------------------------------------
# Page: Sitting plans
# ----------------------------------------------------------------------------
def page_sitting(source):
    st.header("Sitting plans")
    try:
        sit = load_sitting(source, str(docs_folder()))
    except Exception as e:
        st.error(f"Could not load the sitting plans: {type(e).__name__}: {str(e)[:200]}")
        return
    if sit.empty:
        st.info("No sitting-plan PDFs found yet" + (" in Supabase. Run 'Sync to Supabase'." if source == "Supabase" else "."))
        return
    sess = st.selectbox("Session", ["(all)"] + sessions_of(sit))
    d = sit if sess == "(all)" else sit[sit["session"] == sess]
    text = st.text_input("Search paper code / name", "")
    if text.strip():
        t = text.strip().lower()
        d = d[(d["paper_code"].fillna("").str.lower().str.contains(t, regex=False))
              | (d["paper_name"].fillna("").str.lower().str.contains(t, regex=False))]
    view = d.drop(columns=["rolls"]).copy()
    view["check"] = ["OK" if (a == b) else "count differs" for a, b in zip(view["total_students"], view["roll_count"])]
    st.dataframe(view, hide_index=True)
    st.caption("check = the 'Total' printed on the sheet against the roll numbers read from it.")
    st.download_button("Download (CSV)", csv_bytes(view), file_name="sitting_plans.csv")

    st.subheader("Which papers does a roll number sit?")
    roll = st.text_input("Roll number", "").strip()
    if roll:
        hit = sit[sit["rolls"].map(lambda r: roll in (r or []))]
        if hit.empty:
            st.info("This roll number is not in any sitting plan.")
        else:
            st.dataframe(hit[["session", "tag", "paper_code", "paper_name", "sem", "status", "exam_centre"]],
                         hide_index=True)


# ----------------------------------------------------------------------------
# Page: Files & backfill
# ----------------------------------------------------------------------------
def disk_summary(docs):
    rows = []
    if not docs.is_dir():
        return pd.DataFrame(columns=["session", "attestation PDFs", "sitting PDFs", "result Excel files", "reports"])
    for d in sorted(docs.iterdir()):
        if not d.is_dir() or len(d.name) < 8 or d.name[3] != "_" or not d.name[4:8].isdigit():
            continue
        att = len(list((d / f"{d.name}_ATTESTATION").rglob("*.pdf"))) if (d / f"{d.name}_ATTESTATION").is_dir() else 0
        sit = sum(len(list(s.glob("*.pdf"))) for s in d.iterdir()
                  if s.is_dir() and not s.name.endswith(("_ATTESTATION", "_RESULTS")))
        res = d / f"{d.name}_RESULTS"
        xl = len([f for f in res.glob("*.xlsx") if not f.name.startswith("~$")]) if res.is_dir() else 0
        rep = len(list((res / "REPORTS").glob("*.xlsx"))) if (res / "REPORTS").is_dir() else 0
        rows.append({"session": d.name, "attestation PDFs": att, "sitting PDFs": sit, "result Excel files": xl,
                     "reports": rep})
    return pd.DataFrame(rows)


def page_files():
    st.header("Files & backfill")
    docs = docs_folder()
    st.caption(f"Documents folder: {docs}")
    summary = disk_summary(docs)
    st.subheader("What is on this PC")
    if summary.empty:
        st.info("No session folders (like JUN_2026) found yet.")
    else:
        st.dataframe(summary, hide_index=True)

    st.subheader("Backfill progress")
    ledger = docs / "attestation_backfill.json"
    if ledger.exists():
        try:
            data = json.loads(ledger.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        rows = [{"session | course | tag": k, "state": v.get("state"), "when": v.get("when"), "note": v.get("note")}
                for k, v in sorted(data.items())]
        led = pd.DataFrame(rows)
        if len(led):
            st.bar_chart(led["state"].value_counts())
            pick = st.multiselect("States", sorted(led["state"].dropna().unique()),
                                  default=sorted(led["state"].dropna().unique()))
            st.dataframe(led[led["state"].isin(pick)], hide_index=True)
    else:
        st.info("No backfill has been run yet.")

    st.subheader("Excel reports")
    stats = docs / "RESULT_STATISTICS.xlsx"
    if stats.exists():
        st.download_button("RESULT_STATISTICS.xlsx (all classes)", stats.read_bytes(), file_name=stats.name)
    reports = sorted(docs.glob("*/*_RESULTS/REPORTS/*_REPORT.xlsx"), reverse=True)
    if reports:
        pick = st.selectbox("Class report", [str(p.relative_to(docs)) for p in reports])
        p = docs / pick
        st.download_button("Download this report", p.read_bytes(), file_name=p.name)
    elif not stats.exists():
        st.info("No reports yet. Use 'Make the reports' on the Run tasks page.")


# ----------------------------------------------------------------------------
# Page: Database
# ----------------------------------------------------------------------------
def page_database():
    st.header("Database (Supabase)")
    if not db.configured():
        st.warning("Supabase is not connected yet.")
        st.markdown(
            "1. Create a free project at supabase.com.\n"
            "2. SQL Editor -> paste the contents of `supabase_schema.sql` (below) -> Run.\n"
            "3. Project Settings -> API: copy the **Project URL** and the **service_role** key.\n"
            "4. In the file `.env` next to the scripts add:\n"
            "```\nSUPABASE_URL=https://xxxx.supabase.co\nSUPABASE_KEY=<service_role key>\n```\n"
            "5. Restart the app, then press 'Sync to Supabase' on the Run tasks page.\n\n"
            "Keep the service_role key private: it can read and change every table.")
    else:
        st.success("Supabase is configured.")
        if st.button("Count the rows in every table"):
            try:
                client = db.connect()
                st.table(pd.DataFrame([(t, db.count(client, t)) for t in db.TABLES], columns=["table", "rows"]))
            except Exception as e:
                st.error(f"{type(e).__name__}: {str(e)[:200]}")
        st.caption("Use 'Sync to Supabase' on the Run tasks page after new results or sitting plans arrive. "
                   "It can be repeated safely: nothing is duplicated.")
    sql = BASE / "supabase_schema.sql"
    if sql.exists():
        with st.expander("supabase_schema.sql"):
            st.code(sql.read_text(encoding="utf-8"), language="sql")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def gate():
    """Optional password (APP_PASSWORD in .env)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE / ".env")
    except Exception:
        pass
    secret = os.environ.get("APP_PASSWORD")
    if not secret or st.session_state.get("authed"):
        return True
    pw = st.text_input("Password", type="password")
    if pw and pw == secret:
        st.session_state["authed"] = True
        st.rerun()
    elif pw:
        st.error("Wrong password.")
    return False


def main():
    st.set_page_config(page_title="College Results Manager", page_icon="📊", layout="wide")
    try:                                    # Streamlit Cloud style secrets, if any
        for k in ("SUPABASE_URL", "SUPABASE_KEY"):
            if k in st.secrets:
                os.environ.setdefault(k, st.secrets[k])
    except Exception:
        pass
    st.title("College Results Manager")
    if not gate():
        return

    with st.sidebar:
        page = st.radio("Page", PAGES)
        st.divider()
        options = ["Supabase", "Local Excel files"]
        source = st.radio("Data from", options, index=0 if db.configured() else 1)
        st.text_input("Documents folder (blank = My Documents)", key="docs_override")
        if st.button("Reload the data"):
            st.cache_data.clear()
            st.rerun()
        st.caption(f"Folder: {docs_folder()}")
        gone = missing_files()
        if gone:
            st.error("Missing next to app.py: " + ", ".join(gone))

    if page == "Download":
        page_download()
    elif page == "Run tasks":
        page_run()
    elif page == "Attestation data":
        page_attestation(source)
    elif page == "Results":
        page_results(source)
    elif page == "Student search":
        page_student(source)
    elif page == "Statistics":
        page_stats(source)
    elif page == "Sitting plans":
        page_sitting(source)
    elif page == "Files & backfill":
        page_files()
    else:
        page_database()


if __name__ == "__main__":
    main()

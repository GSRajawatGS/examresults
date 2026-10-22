"""
analytics.py - loads the data (from Supabase, or straight from the local Excel files / PDFs) into pandas
tables and works out the statistics the app shows. No Streamlit in here, so it can be tested on its own.

The tables have the same columns as the Supabase tables (see supabase_schema.sql):
    students, marksheets, papers (= marksheet_papers), sitting (= sitting_plans), results (= students + marksheets)
"""
import re
from pathlib import Path

import pandas as pd

import db
import db_sync
import result_report as rr

STUDENT_COLS = ["session", "exam_code", "roll_no", "name", "father_name", "mother_name", "enrollment_no",
                "regular_backlog", "exam_name", "exam_centre", "college_name", "gender", "category", "address",
                "papers", "class_label", "att_regular_private", "att_status", "att_branch", "source_file"]
MARK_COLS = ["session", "exam_code", "roll_no", "result_status", "result_note", "result_exam_code", "fetched_at",
             "title", "ms_roll_number", "ms_enrollment", "ms_name", "ms_father", "ms_mother", "abcid",
             "total_obtained", "total_max", "percentage", "final_result"]
PAPER_COLS = ["session", "exam_code", "roll_no", "paper", "components", "total", "total_num"]
SITTING_COLS = ["session", "tag", "file_key", "exam_centre", "class_line", "branch", "sem", "regular_private",
                "status", "paper_code", "paper_name", "total_students", "roll_count", "rolls"]


ATT_COLS = ["session", "tag", "file_key", "roll_no", "folder", "course", "branch", "sem", "regular_private", "status",
            "exam_code", "enrollment_no", "regular_backlog", "exam_name", "exam_centre", "college_name", "name",
            "father_name", "mother_name", "gender", "category", "address", "papers"]


def _frame(rows, cols):
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------
def local_frames(docs):
    """The same tables, read from the class Excel files in the Documents folder."""
    st_rows, ms_rows, pp_rows = [], [], []
    for _, _, st, ms, pp in db_sync.class_rows(Path(docs)):
        st_rows += st
        ms_rows += ms
        pp_rows += pp
    return {"students": _frame(st_rows, STUDENT_COLS), "marksheets": _frame(ms_rows, MARK_COLS),
            "papers": _frame(pp_rows, PAPER_COLS)}


def local_sitting(docs, session=None):
    rows = []
    for sess, tag, key, f in db_sync.sitting_files(Path(docs), session):
        try:
            rows.append(db_sync.sitting_row(sess, tag, key, f))
        except Exception:
            pass
    return _frame(rows, SITTING_COLS)


def db_frames(client):
    return {"students": _frame(db.fetch_all(client, "students"), STUDENT_COLS),
            "marksheets": _frame(db.fetch_all(client, "marksheets"), MARK_COLS),
            "papers": _frame(db.fetch_all(client, "marksheet_papers"), PAPER_COLS)}


def local_attestation(docs, session=None):
    """Every attestation PDF under <Documents>\\<SESSION>\\<SESSION>_ATTESTATION (normal and NEP), read now."""
    rows = []
    for sess, tag, key, f in db_sync.attestation_files(Path(docs), session):
        try:
            rows += db_sync.attestation_rows(sess, tag, key, f)
        except Exception:
            pass
    return _frame(rows, ATT_COLS)


def db_attestation(client):
    return _frame(db.fetch_all(client, "attestation_students"), ATT_COLS)


def db_sitting(client):
    return _frame(db.fetch_all(client, "sitting_plans"), SITTING_COLS)


def merge_results(frames):
    """One row per student: attestation data + the marksheet result (if any)."""
    keys = ["session", "exam_code", "roll_no"]
    st = frames["students"].drop(columns=[c for c in ("id", "updated_at") if c in frames["students"]])
    ms = frames["marksheets"].drop(columns=[c for c in ("id", "updated_at", "extra", "raw_text")
                                            if c in frames["marksheets"]])
    return st.merge(ms, on=keys, how="left")


# ----------------------------------------------------------------------------
# Per-student flags
# ----------------------------------------------------------------------------
def kind_of(row):
    label = str(row.get("class_label") or "").upper()
    if "ATKT" in label:
        return "ATKT"
    if label:
        return "Main"
    return "ATKT" if str(row.get("exam_code") or "").upper().startswith("A") else "Main"


def family_of(row):
    label = str(row.get("class_label") or "")
    if label:
        return re.sub(r"\s*\([^)]*\)\s*$", "", label).strip()
    return str(row.get("exam_code"))


def prepare(results):
    """Adds appeared / passed / division / kind / family columns."""
    df = results.copy()
    for c in ("result_status", "final_result", "percentage", "total_obtained", "total_max"):
        if c not in df:
            df[c] = None
    status = df["result_status"].fillna("").astype(str)
    df["appeared"] = status.eq("OK")
    df["not_found"] = status.eq("NOT FOUND")
    df["pending"] = ~df["appeared"] & ~df["not_found"]
    df["result_text"] = df["final_result"].map(lambda t: rr.clean(t, ""))
    df["passed"] = df["appeared"] & df["result_text"].map(lambda t: bool(t) and rr.is_pass(t))
    df["percentage"] = pd.to_numeric(df["percentage"], errors="coerce")
    df["total_obtained"] = pd.to_numeric(df["total_obtained"], errors="coerce")
    df["division"] = [rr.division_of(p) if ok and pd.notna(p) else ""
                      for ok, p in zip(df["passed"], df["percentage"])]
    df["kind"] = df.apply(kind_of, axis=1) if len(df) else []
    df["family"] = df.apply(family_of, axis=1) if len(df) else []
    return df


def session_sort_key(session):
    return rr.session_key(str(session).replace("-", "_"))


# ----------------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------------
def pct(a, b):
    return round(100.0 * a / b, 2) if b else 0.0


def overview(df):
    appeared = int(df["appeared"].sum())
    passed = int(df["passed"].sum())
    return {"listed": len(df), "appeared": appeared, "not_found": int(df["not_found"].sum()),
            "pending": int(df["pending"].sum()), "passed": passed, "not_passed": appeared - passed,
            "pass_pct": pct(passed, appeared),
            "avg_pct": round(float(df.loc[df["appeared"], "percentage"].mean()), 2)
            if df["appeared"].any() and df["percentage"].notna().any() else None,
            "highest": df["total_obtained"].max() if df["total_obtained"].notna().any() else None}


def class_summary(df):
    """One row per session + class."""
    rows = []
    for (sess, code), g in df.groupby(["session", "exam_code"], sort=False):
        o = overview(g)
        rows.append({"session": sess, "exam_code": code, "kind": g["kind"].iloc[0], "family": g["family"].iloc[0],
                     "in_list": o["listed"], "appeared": o["appeared"], "not_found": o["not_found"],
                     "pending": o["pending"], "passed": o["passed"], "pass_pct": o["pass_pct"],
                     "avg_pct": o["avg_pct"], "highest": o["highest"]})
    out = pd.DataFrame(rows, columns=["session", "exam_code", "kind", "family", "in_list", "appeared", "not_found",
                                      "pending", "passed", "pass_pct", "avg_pct", "highest"])
    if len(out):
        out = out.sort_values(["session", "exam_code"], key=lambda s: s.map(session_sort_key) if s.name == "session"
                              else s, ascending=[False, True]).reset_index(drop=True)
    return out


def group_stats(df, col):
    """Gender / category / status ... wise: in list, appeared, passed, pass %, average %."""
    if col not in df or df.empty:
        return pd.DataFrame(columns=[col, "in_list", "appeared", "passed", "pass_pct", "avg_pct"])
    key = df[col].fillna("(unknown)").astype(str).str.strip().str.upper().replace("", "(unknown)")
    rows = []
    for k, g in df.groupby(key):
        app = int(g["appeared"].sum())
        rows.append({col: k, "in_list": len(g), "appeared": app, "passed": int(g["passed"].sum()),
                     "pass_pct": pct(int(g["passed"].sum()), app),
                     "avg_pct": round(float(g.loc[g["appeared"], "percentage"].mean()), 2)
                     if app and g["percentage"].notna().any() else None})
    return pd.DataFrame(rows)


def division_counts(df):
    order = [name for _, name in rr.DIVISIONS]
    counts = df.loc[df["passed"], "division"].value_counts()
    return pd.DataFrame({"division": order, "students": [int(counts.get(n, 0)) for n in order]})


def band_counts(df):
    labels = rr.band_labels()
    counts = [0] * len(labels)
    for p in df.loc[df["appeared"], "percentage"].dropna():
        counts[rr.band_index(p)] += 1
    return pd.DataFrame({"marks band": labels, "students": counts})


def result_counts(df):
    s = df.loc[df["appeared"], "result_text"].replace("", "(blank)").value_counts()
    return pd.DataFrame({"result": s.index, "students": s.values})


def toppers(df, n=10):
    t = df[df["appeared"] & df["total_obtained"].notna()].copy()
    if t.empty:
        return t
    t["rank"] = t["total_obtained"].rank(method="min", ascending=False).astype(int)
    t = t.sort_values(["rank", "roll_no"])
    t = t[t["rank"] <= n]
    return t[["rank", "roll_no", "name", "total_obtained", "percentage", "division", "final_result"]]


def paper_stats(papers, df):
    """Paper-wise statistics of the students in df (one class or more)."""
    if papers.empty or df.empty:
        return pd.DataFrame(columns=["paper", "appeared", "absent", "highest", "lowest", "average"])
    keys = ["session", "exam_code", "roll_no"]
    p = papers.merge(df.loc[df["appeared"], keys], on=keys, how="inner")
    p["absent"] = p["total"].fillna("").astype(str).str.upper().isin(rr.ABSENT_WORDS)
    p["total_num"] = pd.to_numeric(p["total_num"], errors="coerce")
    rows = []
    for paper, g in p.groupby("paper", sort=False):
        v = g["total_num"].dropna()
        rows.append({"paper": paper, "appeared": int((~g["absent"]).sum()), "absent": int(g["absent"].sum()),
                     "highest": v.max() if len(v) else None, "lowest": v.min() if len(v) else None,
                     "average": round(float(v.mean()), 2) if len(v) else None})
    return pd.DataFrame(rows)


def trend(summary):
    """Pass % of the same class (family) in every session: rows = family, columns = sessions."""
    if summary.empty:
        return pd.DataFrame()
    g = summary.groupby(["family", "session"]).agg(appeared=("appeared", "sum"), passed=("passed", "sum")).reset_index()
    g["pass_pct"] = [pct(p, a) if a else None for p, a in zip(g["passed"], g["appeared"])]
    pv = g.pivot(index="family", columns="session", values="pass_pct")
    return pv[sorted(pv.columns, key=session_sort_key)]


def student_card(frames, session, code, roll):
    """Everything known about one student of one class: dicts for attestation, marksheet and papers."""
    keys = {"session": session, "exam_code": code, "roll_no": str(roll)}

    def pick(df):
        if df.empty:
            return df
        m = (df["session"] == keys["session"]) & (df["exam_code"] == keys["exam_code"]) & (df["roll_no"] == keys["roll_no"])
        return df[m]

    st, ms, pp = pick(frames["students"]), pick(frames["marksheets"]), pick(frames["papers"])
    return (st.iloc[0].dropna().to_dict() if len(st) else {}, ms.iloc[0].dropna().to_dict() if len(ms) else {}, pp)


# ----------------------------------------------------------------------------
# One student, every exam
# ----------------------------------------------------------------------------
def _clean_dict(d, drop=("id", "updated_at", "raw_text")):
    out = {}
    for k, v in d.items():
        if k in drop or v is None or (isinstance(v, float) and v != v):
            continue
        if isinstance(v, str) and not v.strip():
            continue
        out[k] = v
    return out


def find_student(frames, query, link_enrollment=True):
    """Everything known about a student, found by roll number (or enrollment number).

    link_enrollment: also take this student's exams under OTHER roll numbers (same enrollment number).
    Returns a dict: found, query, rolls, profile, names, exams (DataFrame), details (one entry per exam)."""
    q = str(query).strip()
    st, ms, pp = frames["students"], frames["marksheets"], frames["papers"]
    res = {"query": q, "found": False, "rolls": [], "profile": {}, "names": [], "exams": pd.DataFrame(),
           "details": [], "linked": False}
    if not q or st.empty:
        return res

    base = st[st["roll_no"].astype(str) == q]
    if base.empty:                                           # maybe an enrollment number was typed
        base = st[st["enrollment_no"].fillna("").astype(str).str.upper() == q.upper()]
    if base.empty:
        return res
    rolls = set(base["roll_no"].astype(str))
    enrolls = set(base["enrollment_no"].dropna().astype(str))
    if link_enrollment and enrolls:
        linked = st[st["enrollment_no"].isin(enrolls)]
        res["linked"] = set(linked["roll_no"].astype(str)) != rolls
        rolls |= set(linked["roll_no"].astype(str))

    return _build_student(frames, rolls, q, res["linked"])


def _build_student(frames, rolls, q, linked=False):
    """The full result (profile, exams, details) for a set of roll numbers."""
    st, ms, pp = frames["students"], frames["marksheets"], frames["papers"]
    res = {"query": q, "found": False, "rolls": [], "profile": {}, "names": [], "exams": pd.DataFrame(),
           "details": [], "linked": linked}
    students = st[st["roll_no"].astype(str).isin(rolls)].copy()
    keys = ["session", "exam_code", "roll_no"]
    marks = ms[ms["roll_no"].astype(str).isin(rolls)] if not ms.empty else ms
    papers = pp[pp["roll_no"].astype(str).isin(rolls)] if not pp.empty else pp

    merged = prepare(students.drop(columns=[c for c in ("id", "updated_at") if c in students]).merge(
        marks.drop(columns=[c for c in ("id", "updated_at", "extra", "raw_text") if c in marks]), on=keys, how="left"))
    merged["_order"] = merged["session"].map(session_sort_key)
    merged = merged.sort_values(["_order", "exam_code"], ascending=[False, True]).drop(columns="_order")
    cols = ["session", "exam_code", "class_label", "roll_no", "att_status", "att_regular_private", "result_status",
            "result_exam_code", "final_result", "total_obtained", "total_max", "percentage", "division"]
    res["exams"] = merged[[c for c in cols if c in merged]].reset_index(drop=True)

    details = []
    for _, row in merged.iterrows():
        k = {"session": row["session"], "exam_code": row["exam_code"], "roll_no": row["roll_no"]}
        m = marks[(marks["session"] == k["session"]) & (marks["exam_code"] == k["exam_code"])
                  & (marks["roll_no"] == k["roll_no"])] if not marks.empty else marks
        md = _clean_dict(m.iloc[0].to_dict()) if len(m) else {}
        extra = md.pop("extra", None)
        if isinstance(extra, dict):
            md.update(extra)
        for drop in ("session", "exam_code", "roll_no"):
            md.pop(drop, None)
        p = papers[(papers["session"] == k["session"]) & (papers["exam_code"] == k["exam_code"])
                   & (papers["roll_no"] == k["roll_no"])] if not papers.empty else papers
        rows = []
        for _, pr in p.iterrows():
            comps = pr.get("components") or {}
            rows.append({"paper": pr["paper"], **{str(a): b for a, b in comps.items()}})
        details.append({"key": k, "class_label": row.get("class_label"), "result": row.get("final_result"),
                        "status": row.get("result_status"), "marksheet": md, "papers": pd.DataFrame(rows)})
    res["details"] = details

    prof_rows = students.copy()
    prof_rows["_order"] = prof_rows["session"].map(session_sort_key)
    latest = prof_rows.sort_values("_order", ascending=False).iloc[0]
    res["profile"] = _clean_dict({k: latest.get(k) for k in (
        "name", "father_name", "mother_name", "enrollment_no", "gender", "category", "address", "college_name",
        "exam_centre") if k in latest})
    res["names"] = sorted({str(x).strip().upper() for x in students["name"].dropna()})
    res["rolls"] = sorted(rolls)
    res["found"] = True
    return res


def _person_key(row):
    enr = str(row.get("enrollment_no") or "").strip().upper()
    if enr and enr != "NAN":
        return "E:" + enr
    return "N:" + norm_name(row.get("name")) + "|" + norm_name(row.get("father_name"))


def norm_name(text):
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def search_students(frames, query, include_parents=False, limit=200):
    """Students whose name contains every word of `query` (any order, any case, part of a word is enough):
    'akash' finds Akash Sharma, Akash Verma, Ram Akash, Aakash ...
    One row per PERSON (same enrollment number, else same name + father), with all of their roll numbers."""
    words = [w for w in re.split(r"\s+", str(query).upper().strip()) if w]
    st = frames["students"]
    cols = ["name", "father_name", "enrollment_no", "rolls", "exams", "last_exam", "gender"]
    if not words or st.empty:
        return pd.DataFrame(columns=cols)
    text = st["name"].fillna("").astype(str).str.upper()
    if include_parents:
        text = text + " | " + st["father_name"].fillna("").astype(str).str.upper() + " | " + \
               st["mother_name"].fillna("").astype(str).str.upper()
    mask = pd.Series(True, index=st.index)
    for w in words:
        mask &= text.str.contains(w, regex=False)
    hit = st[mask].copy()
    if hit.empty:
        return pd.DataFrame(columns=cols)
    hit["_key"] = hit.apply(_person_key, axis=1)
    hit["_order"] = hit["session"].map(session_sort_key)

"""agent_runner.py - Groq agent for mponline.py.

GUIDED MODE (no arguments):   python agent_runner.py
  asks report type, session, course(s) -> lists the portal's report links -> Groq ranks them ->
  tries the best link, explores the form, fixes jobs.py (course spelling etc.) -> if the link is
  wrong it tries the next one -> runs mponline.py and downloads.

JOB MODE:   python agent_runner.py "task text" JOB_NAME   [--no-explore]

Setup:  pip install groq python-dotenv
"""
import os
import re
import sys
import ast
import json
import time
import shutil
import runpy
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env", override=True)

API_KEY = os.environ.get("GROQ_API_KEY")
if not API_KEY:
    sys.exit("GROQ_API_KEY not found. Add it to the .env file.")

client = Groq(api_key=API_KEY)
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
JOBS_FILE = BASE / "jobs.py"
MPONLINE = BASE / "mponline.py"
EXPLORER = BASE / "explorer.py"
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}
AGENT_JOB = "agent-job"

MAX_LOOPS = 5
MAX_EXPLORE_ROUNDS = 3
MAX_LINKS_TO_TRY = 4
ALLOWED_FIELDS = {"session", "course", "levels", "run", "href", "text"}
HUMAN_ONLY = ("captcha", "otp", "invalid password", "login failed",
              "invalid user", "incorrect password", "account locked")

PLAN_PROMPT = """You fix jobs.py for an MPOnline/eGranthalaya scraper.
You receive the job config and what the LIVE web page really offers (sessions, courses,
dropdown labels, report links). Reply with ONE JSON object only:
{"edits": [{"job": "<job name>", "field": "session|course|levels|run|href|text", "value": ...}],
 "needs_human": false, "wrong_link": false, "reason": "<one short sentence>"}
Rules:
- Copy option text EXACTLY as the page lists it (the sitting page and the attestation page
  spell courses differently, e.g. "LLB - L.L.B." vs "LLB(L.L.B.)").
- Map the user's short names to page options (e.g. "BA" -> "BA - B.A.", "BCOM" -> "BCOM - BCOM").
- session/course: a string or a list of strings. levels: a list of dropdown labels in page
  order, or "auto". href: a distinctive part of the right report link's URL.
- Change only what is missing or mismatched. Nothing to change -> "edits": [].
- wrong_link true: the requested session/course is not offered on this page at all (no similar
  spelling), so this is probably the wrong report link. Then return no edits.
- If several options could be what the user meant, set needs_human true.
- Never repeat an edit already in the history."""

FIX_PROMPT = """You are an automation agent for an MPOnline/eGranthalaya scraping script.
Reply with ONE JSON object only:
{"action": "edit_job" | "retry" | "ask_human" | "stop",
 "job": "<job name>", "field": "session|course|levels|run|href|text", "value": <new value>,
 "reason": "<one short sentence>"}
- edit_job: a wrong/missing option in jobs.py (use the exact spelling from the error). If the log
  shows every combination "no data" with a message like "Please Select ...", set levels to "auto".
- retry: temporary problem (timeout, browser crash, network).
- ask_human: login, password, captcha, or anything you cannot fix by editing those fields.
- stop: impossible task.
- Never repeat a fix already in the history."""

RANK_PROMPT = """You choose the right report link on an MPOnline university portal.
You get the user's request and the list of links ({"text","href"}). Reply with ONE JSON object:
{"ranked": [{"href": "<copied exactly from the list>", "why": "<short>"}]}  best first, max 5.
Rules:
- report type "attestation" -> links about attestation (href/text containing 'attest').
  "sitting" -> sitting plan / docket / exam centre links ('sitting', 'docket').
- NEP = yes -> prefer links containing 'NEP'; NEP = no -> prefer links WITHOUT 'NEP'.
- Roll-number-wise variants ('RollNo_Wise') rank below the normal link unless requested.
- Only use hrefs that exist in the list."""


# ---------------------------------------------------------------- jobs.py
def load_jobs():
    return runpy.run_path(str(JOBS_FILE))["JOBS"]


def safe_jobs(job_name):
    try:
        return [j for j in load_jobs() if not job_name or j["name"] == job_name]
    except Exception as e:
        return f"jobs.py failed to load: {e}"


def valid_value(field, value):
    if field == "run":
        return isinstance(value, bool)
    if field == "levels":
        return value == "auto" or (isinstance(value, list) and all(isinstance(v, str) for v in value))
    if field in ("session", "course"):
        return (isinstance(value, str) and bool(value)) or (
            isinstance(value, list) and bool(value) and all(isinstance(v, str) for v in value))
    if field in ("href", "text"):
        return isinstance(value, str) and bool(value)
    return False


def backup_jobs():
    b = JOBS_FILE.with_name(f"jobs.py.{int(time.time() * 1000)}.bak")
    shutil.copy(JOBS_FILE, b)
    return b


def edit_job(job_name, field, value):
    """Edit one whitelisted field of one job. Backs up jobs.py and restores it if the
    edited file does not load or does not hold the new value."""
    if field not in ALLOWED_FIELDS:
        return False, f"field '{field}' is not allowed"
    if not valid_value(field, value):
        return False, f"bad value for '{field}': {value!r}"

    src = JOBS_FILE.read_text(encoding="utf-8")
    m = re.search(r'"name"\s*:\s*"%s"' % re.escape(job_name), src)
    if not m:
        return False, f"job '{job_name}' not found"

    start = src.rfind("{", 0, m.start())
    end = src.find("}", m.end())
    block = src[start:end]

    pattern = re.compile(r'("%s"\s*:\s*)([^\n#]*?)(,?)([ \t]*#[^\n]*)?$' % field, re.M)
    new_block, n = pattern.subn(
        lambda x: x.group(1) + repr(value) + x.group(3) + (x.group(4) or ""),
        block, count=1)
    if n == 0:
        return False, f"field '{field}' not present in job '{job_name}'"

    backup = backup_jobs()
    JOBS_FILE.write_text(src[:start] + new_block + src[end:], encoding="utf-8")
    try:
        job = next(j for j in load_jobs() if j["name"] == job_name)
        if job.get(field) != value:
            raise ValueError("value mismatch after edit")
    except Exception as e:
        shutil.copy(backup, JOBS_FILE)
        return False, f"edit failed verification, restored backup ({e})"
    return True, f"{job_name}.{field} = {value!r} (backup: {backup.name})"


def _py(v):
    return repr(v) if isinstance(v, bool) else json.dumps(v)


def ensure_job(job):
    """Create (or replace) the job named job['name'] at the end of JOBS in jobs.py."""
    src = JOBS_FILE.read_text(encoding="utf-8")
    m = re.search(r'"name"\s*:\s*"%s"' % re.escape(job["name"]), src)
    if m:  # drop the old block
        start = src.rfind("{", 0, m.start())
        line_start = src.rfind("\n", 0, start) + 1
        end_line = src.find("\n", src.find("}", m.end())) + 1
        src = src[:line_start] + src[end_line:]
    block = "    {\n" + "".join(f"        {json.dumps(k)}: {_py(v)},\n" for k, v in job.items()) + "    },\n"
    idx = src.rfind("]")
    new_src = src[:idx] + block + src[idx:]

    backup = backup_jobs()
    JOBS_FILE.write_text(new_src, encoding="utf-8")
    try:
        got = next(j for j in load_jobs() if j["name"] == job["name"])
        if got.get("href") != job.get("href"):
            raise ValueError("job not found after write")
    except Exception as e:
        shutil.copy(backup, JOBS_FILE)
        raise RuntimeError(f"could not write job to jobs.py, restored backup ({e})")


# ---------------------------------------------------------------- Groq
def groq_json(system, user):
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    try:
        return json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        return {"action": "ask_human", "needs_human": True, "edits": [], "ranked": [],
                "reason": "Groq returned invalid JSON"}


def ask_groq_plan(task, job_name, explored, history, run_error=""):
    return groq_json(PLAN_PROMPT, (
        f"Task: {task}\n\nJob config(s): {json.dumps(safe_jobs(job_name))}\n\n"
        f"Live page exploration: {json.dumps(explored)[:6000]}\n\n"
        f"Last run error (may be empty): {run_error}\n\nHistory: {json.dumps(history)}"))


def ask_groq_fix(task, job_name, stdout, stderr, history):
    return groq_json(FIX_PROMPT, (
        f"Task: {task}\n\nJob config(s): {json.dumps(safe_jobs(job_name))}\n\n"
        f"STDOUT (tail):\n{stdout[-2000:]}\n\nSTDERR (tail):\n{stderr[-2000:]}\n\n"
        f"History: {json.dumps(history)}"))


# ---------------------------------------------------------------- subprocesses
def run_mponline(job_name=""):
    cmd = [sys.executable, str(MPONLINE)] + ([job_name] if job_name else [])
    print(f"\n[Agent] Executing: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=BASE, timeout=3600, env=ENV)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "TimeoutExpired: mponline.py ran longer than 1 hour"


def run_explorer(args, marker, timeout=900):
    try:
        r = subprocess.run([sys.executable, str(EXPLORER)] + args, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           cwd=BASE, timeout=timeout, env=ENV)
    except subprocess.TimeoutExpired:
        print("[Agent] Explorer timed out.")
        return None
    for line in r.stdout.splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    print("[Agent] Explorer failed:\n" + (r.stderr or r.stdout)[-600:])
    return None


def explore(job_name):
    print(f"\n[Agent] Exploring the portal page for job '{job_name}' ...")
    return run_explorer([job_name], "EXPLORE_JSON: ")


def error_signature(stdout, stderr):
    text = (stderr or stdout).strip().splitlines()
    return text[-1].strip() if text else "no output"


def human_pause(msg):
    print(f"[Agent] Human needed: {msg}")
    return input("Fix it, then press Enter to continue (or type 'exit'): ").strip().lower() != "exit"


# ---------------------------------------------------------------- explore -> edit loop
def preflight(task, job_name, history, run_error="", allow_switch=False):
    """Compare the live page with jobs.py and let Groq edit jobs.py until they match.
    Returns True/False; with allow_switch it returns "WRONG_LINK" when the link looks wrong."""
    for rnd in range(1, MAX_EXPLORE_ROUNDS + 1):
        explored = explore(job_name)
        if explored is None:
            return human_pause("could not open the page (login/captcha/network?)")
        if not explored.get("problem") and not run_error:
            print("[Agent] Page options match jobs.py.")
            return True
        if allow_switch and explored.get("problem") == "link_not_found":
            print("[Agent] Link not found on the portal.")
            return "WRONG_LINK"

        print(f"[Agent] Mismatch (round {rnd}): missing sessions={explored.get('missing_sessions')}, "
              f"missing courses={explored.get('missing_courses')}, "
              f"missing levels={explored.get('missing_levels')}, problem={explored.get('problem')}")
        plan = ask_groq_plan(task, job_name, explored, history, run_error)
        print(f"[Groq] {plan}")

        if plan.get("wrong_link"):
            if allow_switch:
                return "WRONG_LINK"
            return human_pause("this looks like the wrong report link; check href in jobs.py")
        if plan.get("needs_human"):
            return human_pause(plan.get("reason", "ambiguous choice"))

        applied = 0
        for e in plan.get("edits", []):
            ok, msg = edit_job(e.get("job", job_name), e.get("field"), e.get("value"))
            print(f"[Agent] {'Edited' if ok else 'Edit rejected'}: {msg}")
            history.append(f"edit_job -> {msg}")
            applied += ok
        if applied == 0:
            if explored.get("problem"):
                return "WRONG_LINK" if allow_switch else human_pause(
                    "Groq found nothing it can safely edit; check jobs.py by hand")
            return True
        run_error = ""   # verify the edits on the next round
    return "WRONG_LINK" if allow_switch else False


# ---------------------------------------------------------------- guided mode
def pick(prompt, options):
    while True:
        a = input(f"{prompt} ({'/'.join(options)}): ").strip().lower()
        for o in options:
            if a in (o, o[0]):
                return o
        print("  please type one of:", ", ".join(options))


def ask_request():
    print("\n=== What do you want to download? ===")
    kind = pick("Report type", ["attestation", "sitting"])
    session = input("Session exactly as on the portal (e.g. MAR-2026): ").strip().upper()
    courses = [c.strip() for c in input("Course(s), comma separated (e.g. BA, BSC, BCOM): ").split(",") if c.strip()]
    nep = pick("NEP scheme", ["yes", "no"])
    if not session or not courses:
        sys.exit("Session and course are required.")
    return {"kind": kind, "session": session, "course": courses, "nep": nep}


def discover_links():
    print("\n[Agent] Logging in to list the portal's report links ...")
    return run_explorer(["--links"], "LINKS_JSON: ", timeout=600) or []


def rank_links(req, links):
    plan = groq_json(RANK_PROMPT, f"Request: {json.dumps(req)}\n\nLinks: {json.dumps(links)}")
    by_href = {l["href"]: l for l in links}
    ranked = [by_href[r["href"]] for r in plan.get("ranked", [])
              if isinstance(r, dict) and r.get("href") in by_href]
    if not ranked:   # fallback: keyword filter
        key = "attest" if req["kind"] == "attestation" else "sitting|docket"
        ranked = [l for l in links if re.search(key, l["text"] + l["href"], re.I)]
    return ranked


def job_for(req, link):
    href = link["href"]
    low = href.lower()
    tag = "NEP_ROLLWISE" if "rollno_wise" in low else ("NEP" if "nep" in low else "")
    job = {"name": AGENT_JOB, "task": req["kind"], "href": href.split("/")[-1].split("?")[0],
           "session": req["session"], "course": req["course"], "tag": tag}
    if req["kind"] == "attestation" and tag:
        job["levels"] = "auto"
    job["run"] = True
    return job


def guided_mode():
    req = ask_request()
    task = (f"Download {req['kind']} report, session {req['session']}, "
            f"course(s) {', '.join(req['course'])}, NEP={req['nep']}")
    links = discover_links()
    if not links:
        print("[Agent] No report links found (login/captcha problem?). Stopping.")
        return False
    ranked = rank_links(req, links)[:MAX_LINKS_TO_TRY]
    if not ranked:
        print("[Agent] No matching link in the list:\n" + "\n".join(f"  {l['text']} | {l['href']}" for l in links))
        return False

    history = []
    for i, link in enumerate(ranked, 1):
        print(f"\n[Agent] Candidate {i}/{len(ranked)}: {link['text']}  ({link['href']})")
        ensure_job(job_for(req, link))
        res = preflight(task, AGENT_JOB, history, allow_switch=True)
        if res is True:
            return run_agent(task, AGENT_JOB, explore_first=False, history=history)
        if res is False:
            print("[Agent] Stopped.")
            return False
        history.append(f"link rejected: {link['href']}")
        print("[Agent] Wrong link - trying the next one ...")

    print("[Agent] None of the candidate links offered that session/course. "
          "Check the spelling of the session, or the report type.")
    return False


# ---------------------------------------------------------------- summary
def print_summary(success, attempts, outputs, history):
    text = "\n".join(outputs)
    done = [l for l in text.splitlines() if "DONE:" in l]
    print("\n" + "=" * 50)
    print(f"[Summary] {'SUCCESS' if success else 'NOT COMPLETED'} after {attempts} run attempt(s)")
    for d in done[-5:]:
        print(f"  {d.strip()}")
    if history:
        print("  Actions taken:")
        for h in history:
            print(f"   - {h}")
    print("=" * 50)


# ---------------------------------------------------------------- main loop
def run_agent(task, job_name="", explore_first=True, history=None):
    history = history if history is not None else []
    outputs = []
    last_sig, repeats, attempt = None, 0, 0

    if job_name and explore_first:
        if not preflight(task, job_name, history):
            print("[Agent] Stopped before running.")
            return False
    elif explore_first:
        print("[Agent] No job name given - skipping page exploration.")

    for attempt in range(1, MAX_LOOPS + 1):
        print(f"\n--- Agent Iteration {attempt} of {MAX_LOOPS} ---")
        code, out, err = run_mponline(job_name)
        outputs.append(out)
        print(out[-500:])

        done_m = re.search(r"DONE: (\{.*\})", out)
        try:
            saved = ast.literal_eval(done_m.group(1)).get("saved", 0) if done_m else 0
        except Exception:
            saved = 0
        if code == 0 and saved > 0 and "SETTINGS PROBLEM" not in out:
            print_summary(True, attempt, outputs, history)
            return True

        sig = error_signature(out, err)
        repeats = repeats + 1 if sig == last_sig else 0
        last_sig = sig
        print(f"[Agent] Failed or nothing saved (code {code}): {sig}")

        combined = (out + err).lower()
        if repeats >= 1 or any(k in combined for k in HUMAN_ONLY):
            why = "same error repeated" if repeats >= 1 else "login/captcha issue"
            history.append(f"asked human: {why}")
            if not human_pause(why):
                break
            continue

        if "SETTINGS PROBLEM" in out and job_name:
            history.append("settings problem -> re-explored page")
            if not preflight(task, job_name, history, run_error=out[-800:]):
                break
            continue

        decision = ask_groq_fix(task, job_name, out, err, history)
        action = decision.get("action")
        print(f"[Groq] {decision}")

        if action == "edit_job":
            ok, msg = edit_job(decision.get("job", job_name),
                               decision.get("field"), decision.get("value"))
            print(f"[Agent] {'Edited' if ok else 'Edit rejected'}: {msg}")
            history.append(f"edit_job -> {msg}")
            if not ok and not human_pause("edit rejected"):
                break
        elif action == "retry":
            history.append(f"retry: {decision.get('reason', '')}")
        elif action == "stop":
            history.append(f"stop: {decision.get('reason', '')}")
            break
        else:
            history.append(f"asked human: {decision.get('reason', '')}")
            if not human_pause(decision.get("reason", "")):
                break

    print_summary(False, attempt, outputs, history)
    return False


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        guided_mode()
    else:
        run_agent(args[0], args[1] if len(args) > 1 else "",
                  explore_first="--no-explore" not in sys.argv)
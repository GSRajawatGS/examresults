"""
mponline.py - Jiwaji mponline automation (sitting plans + attestation sheets).
All settings (login, jobs, session, course, links) live in jobs.py.

  python mponline.py                 run every job in jobs.py whose "run" is True
  python mponline.py JOBNAME ...     run only the named job(s), even if "run" is False
  python mponline.py links           list the sitting plan / attestation links and their URLs
  python mponline.py backfill        download every attestation not downloaded yet (BACKFILL in jobs.py)
        backfill NAME ...            only the named BACKFILL group(s)
        backfill --plan              show what would be done, download nothing
        backfill --again             ignore the "already done" memory and check everything again
"""
import base64
import ctypes
import difflib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import ddddocr
from playwright.sync_api import sync_playwright, Error as PWError, TimeoutError as PWTimeout

import jobs as cfg

# ----------------------------------------------------------------------------
# Settings (read from jobs.py)
# ----------------------------------------------------------------------------
LOGIN_URL = cfg.LOGIN_URL
USER_ID = cfg.USER_ID
PASSWORD = cfg.PASSWORD
USER_TYPE = getattr(cfg, "USER_TYPE", "G2G")
JOBS = cfg.JOBS
BACKFILL = getattr(cfg, "BACKFILL", [])
BACKFILL_RECHECK_DAYS = getattr(cfg, "BACKFILL_RECHECK_DAYS", 7)
TEST_ONE = getattr(cfg, "TEST_ONE", False)        # attestation: save one file, then stop
MIN_PDF_KB = getattr(cfg, "MIN_PDF_KB", 5)        # attestation: smaller PDFs are treated as empty
INCLUDE_ALL = getattr(cfg, "INCLUDE_ALL", False)  # True also loops the "--ALL--" options
DEBUG = getattr(cfg, "DEBUG", False)              # True prints browser/tab events

# Set for each job by apply_job()
SESSION = ""
COURSE = ""
TAG = ""
IGNORE = {}      # per job: {"Dropdown label": ["option text to skip", ...]}
DEFAULT_LEVELS = ["Select Branch", "Select Semester/Year", "Regular/Private", "Status", "Select Paper"]
DEFAULT_ATT_LEVELS = ["Select College", "Select Branch", "Select Semester/Year", "Regular/Private", "Status"]
LEVELS = DEFAULT_LEVELS
ATT_LEVELS = DEFAULT_ATT_LEVELS

HEADING = ":text('SITTING ARRANGEMENT DETAILS')"
EXPORT_PARTS = ["a[title*='Export' i]", "img[alt*='Export' i]", "img[title*='Export' i]"]
EXPORT_SEL = ", ".join(EXPORT_PARTS)
EXPORT_FRESH = ", ".join(s + ":not([data-old])" for s in EXPORT_PARTS)
EXPORT_URL_RE = re.compile(r"export|format=pdf", re.IGNORECASE)

ocr = ddddocr.DdddOcr(show_ad=False)
ocr.set_ranges("0123456789+")  # only digits and plus

CRASHES = {}   # attestation combination -> number of times the browser closed on it


class ConfigError(Exception):
    """Something in jobs.py is wrong (do not restart the browser)."""


class BrowserLost(Exception):
    pass


class StopRun(Exception):
    pass


# ----------------------------------------------------------------------------
# Login
# ----------------------------------------------------------------------------
def solve_math_captcha(page):
    img = page.locator("#NewCaptcha_imgCaptcha")
    img.wait_for(state="visible", timeout=15000)

    # The image is an inline base64 JPEG, so decode it directly
    src = img.get_attribute("src")
    img_bytes = base64.b64decode(src.split(",", 1)[1])

    text = ocr.classification(img_bytes)
    print(f"-> OCR read: '{text}'")

    # It is always "digit + digit", so take the digits and ignore the "+"
    digits = re.findall(r"\d", text)
    if len(digits) == 2:
        return str(int(digits[0]) + int(digits[1]))

    raise Exception(f"Unexpected OCR result: '{text}'")


def login(page):
    max_attempts = 5

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"\n--- Login Attempt {attempt} of {max_attempts} ---")
            page.goto(LOGIN_URL)
            page.wait_for_load_state("networkidle")

            # Leftover session from a previous run: clear it and reload the login page
            if "already" in page.url.lower() or page.locator("input[type='password']").count() == 0:
                print("-> Old session found, clearing cookies and retrying")
                page.context.clear_cookies()
                page.goto(LOGIN_URL)
                page.wait_for_load_state("networkidle")

            try:
                page.select_option("select", label=USER_TYPE)
            except Exception:
                pass

            page.get_by_placeholder("Enter User ID").fill(USER_ID)
            page.get_by_placeholder("Enter Password").fill(PASSWORD)

            answer = solve_math_captcha(page)
            page.get_by_placeholder("Answer").fill(answer)

            page.locator("button:has-text('सबमिट'), input[value*='सबमिट']").first.click()
            page.wait_for_timeout(3000)

            # Real success: password box gone AND not on the error/index page
            body = page.inner_text("body")
            if (page.locator("input[type='password']").count() == 0
                    and "already logged in" not in page.url.lower()
                    and "resource cannot be found" not in body.lower()):
                print("Logged in successfully!")
                return True
            print("-> Login failed, retrying...")

        except Exception as e:
            print(f"-> Attempt {attempt} error: {e}")
            time.sleep(2)

    print("\n>>> Automated login failed. Please complete manually.")
    page.get_by_placeholder("Answer").focus()
    page.wait_for_selector("input[type='password']", state="detached", timeout=300000)
    print("Logged in successfully (Manual).")
    return True


# ----------------------------------------------------------------------------
# Generic page helpers
# ----------------------------------------------------------------------------
def dismiss_message(page):
    # only a real OK button, so a menu link that contains "ok" is never clicked
    ok_btn = page.locator("input[value='ok' i], button:text-matches('^ok$','i')").first
    ok_btn.wait_for(state="visible", timeout=10000)
    print("-> Clicking OK on message popup")
    ok_btn.click()
    page.wait_for_timeout(500)


def get_dropdown(page, label):
    return page.locator(
        f"xpath=//td[not(.//td)][contains(normalize-space(.),'{label}')]/ancestor::tr[1]//select"
    ).first


def select_row(page, label_text, option=None, quiet=False):
    """Select an option in the dropdown on the row labelled label_text.
    If option is None, the first real option is chosen."""
    sel = get_dropdown(page, label_text)
    sel.wait_for(state="visible", timeout=15000)

    # Wait for the options to load (they fill in after the previous dropdown changes)
    for _ in range(30):
        if sel.locator("option").count() > 1:
            break
        page.wait_for_timeout(500)

    texts = [t.strip() for t in sel.locator("option").all_inner_texts()]
    if not quiet:
        print(f"-> {label_text}: options = {texts}")

    norm = lambda s: " ".join(s.split())
    if option:
        match = [i for i, t in enumerate(texts) if norm(t) == norm(option)]
        if not match:
            close = difflib.get_close_matches(option, texts, n=3, cutoff=0.4)
            raise ConfigError(f"'{option}' is not in the '{label_text}' list. Closest: {close}")
        idx = match[0]
    else:
        idx = next((i for i, t in enumerate(texts) if t and "select" not in t.lower()), None)
        if idx is None:
            print(f"   (no selectable option for {label_text}, skipping)")
            return

    sel.select_option(index=idx)
    if not quiet:
        print(f"   selected: {texts[idx]}")
    page.wait_for_load_state("networkidle")   # dependent dropdowns reload
    page.wait_for_timeout(1000)


def page_labels(page):
    """Labels of the visible dropdowns on the page, in order (e.g. 'Select Branch')."""
    return page.evaluate(r"""() => [...document.querySelectorAll('select')]
        .filter(s => s.offsetParent !== null)
        .map(s => {
            const tr = s.closest('tr');
            const td = tr && tr.querySelector('td');
            return ((td ? td.innerText : s.name) || '').replace(/\s+/g, ' ').trim();
        })""")


def resolve_levels(page, job, default):
    """Which dropdowns to loop over: the job's own list, 'auto' (every dropdown after the
    course), or the default for that task."""
    labels = page_labels(page)
    print("-> Dropdowns on this page:", labels)
    lv = job.get("levels", default)
    if lv == "auto":
        skip = ["Session", "Course", "Exam Center"] + list(job.get("skip", []))
        lv = [l for l in labels if l and not any(s.lower() in l.lower() for s in skip)]
    print("-> Looping over:", lv)
    return lv


SHOWN = set()


def log_options(label, opts):
    """Print a dropdown's options once, so the log shows what is being looped."""
    texts = [t for _, t in opts]
    key = (label, tuple(texts))
    if key not in SHOWN:
        SHOWN.add(key)
        more = f" ... (+{len(texts) - 12} more)" if len(texts) > 12 else ""
        print(f"   [{label}] {len(texts)} option(s): {texts[:12]}{more}")


def popup_message(ok_btn):
    """Text of the message box that the OK button belongs to (e.g. 'No record found')."""
    try:
        return ok_btn.first.evaluate(r"""b => {
            let el = b;
            for (let i = 0; i < 6 && el.parentElement; i++) {
                el = el.parentElement;
                const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
                if (t.replace(/ok/ig, '').replace(/message/ig, '').trim().length > 2) return t.slice(0, 160);
            }
            return '';
        }""")
    except Exception:
        return ""


def real_options(page, label, wait_s=4):
    """Returns [(index, text)] of real options (placeholders like 'Select' / '--ALL--' left out)."""
    sel = get_dropdown(page, label)
    try:
        sel.wait_for(state="visible", timeout=15000)
    except Exception:
        raise ConfigError(
            f"This page has no dropdown labelled '{label}'. Dropdowns found: {page_labels(page)}. "
            f"Set \"levels\": \"auto\" (or a list of these labels) for this job in jobs.py.")
    texts = []
    for _ in range(wait_s * 2):
        texts = [t.strip() for t in sel.locator("option").all_inner_texts()]
        if len(texts) > 1:
            break
        page.wait_for_timeout(500)

    result = []
    for i, t in enumerate(texts):
        low = t.lower()
        if not t or low.startswith("select"):
            continue
        if t.startswith("--") and not (INCLUDE_ALL and "all" in low):
            continue
        if t in IGNORE.get(label, []):
            continue
        result.append((i, t))
    return result


def select_index(page, label, idx):
    # select by position, because some option texts contain line breaks
    get_dropdown(page, label).select_option(index=idx)
    page.wait_for_load_state("networkidle")   # the next dropdowns reload
    page.wait_for_timeout(1000)


def selection_problems(page, labels, path):
    """Dropdowns whose current value is not the one wanted (a later selection can reset an
    earlier dropdown on some pages). Empty list = everything is as expected."""
    norm = lambda s: " ".join(s.split())
    bad = []
    for label, want in zip(labels, path):
        try:
            got = get_dropdown(page, label).evaluate(
                "s => s.options[s.selectedIndex] ? s.options[s.selectedIndex].text : ''")
        except Exception:
            got = "?"
        if norm(got) != norm(want):
            bad.append(f"{label}: wanted '{want}', page shows '{norm(got)}'")
    return bad


def clean(text):
    return re.sub(r'[\\/:*?"<>|\[\]\s]+', "", text)


def to_folder_name(paper):
    # "LLB201 - SPECIAL CONTRACT - IIND" -> "LLB201_SPECIAL_CONTRACT_-_IIND"
    name = "_".join(paper.split(" - ", 1))
    name = re.sub(r"\s+", "_", name)
    return re.sub(r'[\\/:*?"<>|]', "", name)


def documents_dir():
    """Windows 'My Documents' folder (also works if it is redirected to OneDrive)."""
    try:
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # 5 = My Documents
        return Path(buf.value)
    except Exception:
        return Path.home() / "Documents"


# ----------------------------------------------------------------------------
# Task 1: sitting plans (report shown on the page, saved through its Print window)
# ----------------------------------------------------------------------------
def generate_fresh_report(page):
    """Click Generate Report. True = a new report appeared, False = no data."""
    # Mark the old report so it can't be mistaken for the new one
    page.locator(HEADING).evaluate_all("els => els.forEach(e => e.setAttribute('data-old','1'))")

    page.locator(
        "input[value='Generate Report'], button:has-text('Generate Report'), a:has-text('Generate Report')"
    ).first.click()
    page.wait_for_load_state("networkidle")

    fresh = page.locator(HEADING + ":not([data-old])")
    ok_btn = page.locator("input[value='ok' i], button:text-matches('^ok$','i')")

    for _ in range(15):
        if fresh.count() > 0 and fresh.first.is_visible():
            return True
        if ok_btn.count() > 0 and ok_btn.first.is_visible():
            print(f"   [message] {popup_message(ok_btn)!r}")
            ok_btn.first.click()          # e.g. "No record found" message
            page.wait_for_timeout(500)
            return False
        page.wait_for_timeout(1000)
    return False


def print_to_pdf(page, p, pdf_path):
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    heading = page.locator("text=SITTING ARRANGEMENT DETAILS").first
    heading.wait_for(state="visible", timeout=30000)

    # Stop the print window from printing or closing itself, so we can read it
    page.context.add_init_script("window.print = () => {}; window.close = () => {};")
    page.evaluate("window.print = () => {}; window.close = () => {};")

    print_btn = page.locator(
        "input[value*='Print' i], button:has-text('Print'), a:has-text('Print'), "
        "[onclick*='print' i], :text-is('Print')"
    ).last

    html, base = None, page.url
    try:
        print_btn.wait_for(state="visible", timeout=8000)
        print_btn.scroll_into_view_if_needed()
        print("-> Clicking Print")
        with page.context.expect_page(timeout=5000) as info:
            print_btn.click()
        popup = info.value
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=5000)
            html = popup.content()
            if popup.url and not popup.url.startswith("about:"):
                base = popup.url
            print("-> Captured the print window")
        finally:
            try:
                popup.close()
            except Exception:
                pass
    except Exception as e:
        print(f"-> Print window not usable ({type(e).__name__}), using the report on the page")

    # Fallback (or if the print window came back empty): take the report block from the page
    if not html or "SITTING ARRANGEMENT" not in html.upper():
        report = heading.evaluate("""el => {
            const hasGen = n => [...n.querySelectorAll('input,button,a')]
                .some(b => /Generate Report/i.test(b.value || b.innerText || ''));
            while (el.parentElement && !hasGen(el.parentElement)) el = el.parentElement;
            return el.outerHTML;
        }""")
        head = page.evaluate("() => document.head.innerHTML")
        html = f"<html><head>{head}</head><body>{report}</body></html>"
        base = page.url

    extra = (f"<base href='{base}'>"
             "<style>input[type=button],input[type=submit],button{display:none!important}"
             "@page{size:A4 landscape;margin:8mm}</style>")
    html = html.replace("<head>", "<head>" + extra, 1) if "<head>" in html else extra + html

    # page.pdf() only works in headless Chromium, so render in a hidden browser
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(java_script_enabled=False)
    ctx.add_cookies(page.context.cookies())
    pg = ctx.new_page()
    pg.set_content(html, wait_until="networkidle")
    pg.pdf(path=str(pdf_path), format="A4", landscape=True, print_background=True)
    browser.close()

    print("-> PDF saved:", pdf_path)
    return pdf_path


def combo_names(labels, path):
    """File-name stem from the chosen values. For the usual dropdowns this gives
    <sem>_<regular/private>_<status>_<branch> (same as before); any extra dropdown
    (for example a roll-number series) is added before the branch."""
    vals = dict(zip(labels, path))
    first = [l for l in ("Select Semester/Year", "Regular/Private", "Status") if l in vals]
    last = [l for l in ("Select Branch",) if l in vals]
    skip = ("Select College", "Select Paper")
    mid = [l for l in labels if l not in first + last and l not in skip]
    stem = "_".join(clean(vals[l]) for l in first + mid + last)
    return vals, stem


def process_combo(page, p, path, stats):
    vals, stem = combo_names(LEVELS, path)
    paper = vals.get("Select Paper")
    sess = SESSION.replace("-", "_") + TAG                      # JUN-2026 -> JUN_2026 (+ _NEP)
    if paper:
        folder = documents_dir() / sess / f"{sess}_{to_folder_name(paper)}"
    else:                                                       # pages without a Paper dropdown
        folder = documents_dir() / sess / f"{sess}_{clean(COURSE)[:40]}"
    pdf_path = folder / f"{stem}.pdf"
    label = " | ".join(" ".join(v.split())[:30] for v in path)

    if pdf_path.exists():
        print(f"   SKIP (already saved): {label}")
        stats["skipped"] += 1
        return
    bad = selection_problems(page, LEVELS, path)
    if bad:
        print(f"   FAILED: {label} -> a dropdown changed by itself: {bad}")
        print("            Put that dropdown LAST in this job's 'levels' list in jobs.py.")
        stats["failed"] += 1
        if TEST_ONE:
            input("TEST MODE: check the page, then press Enter to stop...")
            raise StopRun()
        return

    outcome = None                                              # "saved" / "failed" / None (no data)
    try:
        if not generate_fresh_report(page):
            print(f"   no data: {label}")
            stats["empty"] += 1
            return
        print_to_pdf(page, p, pdf_path)
        stats["saved"] += 1
        outcome = "saved"
    except Exception as e:
        if page.is_closed():
            raise BrowserLost(str(e))
        print(f"   FAILED: {label} -> {type(e).__name__}: {e}")
        stats["failed"] += 1
        outcome = "failed"

    if TEST_ONE and outcome:                                    # test mode: stop after the first real result
        input("TEST MODE: check the result, then press Enter to stop...")
        raise StopRun()


def walk(page, p, depth, path, stats):
    if depth == len(LEVELS):
        process_combo(page, p, path, stats)
        return
    label = LEVELS[depth]
    opts = real_options(page, label)
    log_options(label, opts)
    for idx, text in opts:
        select_index(page, label, idx)
        walk(page, p, depth + 1, path + [text], stats)


# ----------------------------------------------------------------------------
# Task 2: attestation sheets (report viewer with an Export > PDF menu)
# ----------------------------------------------------------------------------
def find_in_frames(page, selector=None, text=None):
    """First visible match in the page or any iframe (the viewer may live in a frame)."""
    for fr in page.frames:
        try:
            loc = fr.locator(selector) if selector else fr.get_by_text(text, exact=True)
            for i in range(min(loc.count(), 5)):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        except Exception:
            pass
    return None


def mark_old_toolbar(page):
    """Tag the previous report's export icon, so a stale one isn't mistaken for the new report."""
    for fr in page.frames:
        try:
            fr.locator(EXPORT_SEL).evaluate_all(
                "els => els.forEach(e => e.setAttribute('data-old','1'))")
        except Exception:
            pass


def collect_result(page, pdf_path):
    """Returns ('saved', path) | ('empty', None) | ('unknown', None)."""
    ctx = page.context
    ok_btn = page.locator("input[value='ok' i], button:text-matches('^ok$','i')")

    # 1. wait for the new report viewer toolbar (or a "no record" message)
    btn = None
    for _ in range(60):
        btn = find_in_frames(page, EXPORT_FRESH)
        if btn:
            break
        if ok_btn.count() > 0 and ok_btn.first.is_visible():
            print(f"   [message] {popup_message(ok_btn)!r}")
            ok_btn.first.click()
            page.wait_for_timeout(500)
            return "empty", None
        page.wait_for_timeout(1000)
    if not btn:
        print("   [step] report toolbar never appeared")
        return "unknown", None

    page.wait_for_timeout(2500)          # let the report finish rendering

    # 2. open the export dropdown
    btn.click()
    item = None
    for _ in range(10):
        item = find_in_frames(page, text="PDF")
        if item:
            break
        page.wait_for_timeout(500)
    if not item:
        print("   [step] PDF item not found in the export menu")
        return "unknown", None

    # 3. catch the PDF response ourselves instead of using the popup download
    captured = {}

    def handler(route, request):
        try:
            resp = route.fetch()
            body = resp.body()
            if body[:4] == b"%PDF":
                captured["pdf"] = body
                route.fulfill(status=204)        # the popup gets nothing to download
            else:
                route.fulfill(response=resp)     # not the PDF, pass it through
        except Exception as e:
            captured["err"] = str(e)
            try:
                route.continue_()
            except Exception:
                pass

    ctx.route(EXPORT_URL_RE, handler)
    try:
        print("   [step] clicking PDF")
        item.click()
        for _ in range(180):                      # up to 3 minutes for big reports
            if "pdf" in captured:
                break
            page.wait_for_timeout(1000)
    finally:
        ctx.unroute(EXPORT_URL_RE, handler)
        for pg in ctx.pages:                      # close the leftover blank popup
            if pg != page and not pg.is_closed():
                try:
                    pg.close()
                except Exception:
                    pass

    if "pdf" not in captured:
        print("   [step] no PDF captured", captured.get("err", ""))
        return "unknown", None

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(captured["pdf"])
    return "saved", pdf_path


def process_att(page, path, stats):
    vals, stem = combo_names(ATT_LEVELS, path)
    key = tuple(path)
    sess = SESSION.replace("-", "_") + TAG
    base = documents_dir() / sess / f"{sess}_ATTESTATION"
    if "Select College" in vals:                                # pages with a College dropdown
        folder = base / clean(vals["Select College"])[:60]
    else:                                                       # NEP page: one folder per course
        folder = base / clean(COURSE)[:40]
    label = " | ".join(" ".join(v.split())[:30] for v in path)

    if folder.exists() and any(folder.glob(stem + ".*")):
        print(f"   SKIP (already saved): {label}")
        stats["skipped"] += 1
        return

    if CRASHES.get(key, 0) >= 2:
        print(f"   GIVING UP (browser closed twice here): {label}")
        stats["failed"] += 1
        return

    bad = selection_problems(page, ATT_LEVELS, path)
    if bad:
        print(f"   FAILED: {label} -> a dropdown changed by itself: {bad}")
        print("            Put that dropdown LAST in this job's 'levels' list in jobs.py.")
        stats["failed"] += 1
        if TEST_ONE:
            input("TEST MODE: check the page, then press Enter to stop...")
            raise StopRun()
        return

    result, target = "failed", None
    try:
        mark_old_toolbar(page)
        page.locator(
            "input[value='Generate Report'], button:has-text('Generate Report'), a:has-text('Generate Report')"
        ).first.click()
        result, target = collect_result(page, folder / f"{stem}.pdf")
    except Exception as e:
        if page.is_closed():
            CRASHES[key] = CRASHES.get(key, 0) + 1
            for f in folder.glob(stem + ".*"):      # remove a half-written file
                try:
                    f.unlink()
                except Exception:
                    pass
            raise BrowserLost(str(e))
        print(f"   FAILED: {label} -> {type(e).__name__}: {e}")
        stats["failed"] += 1

    if result == "saved":
        size_kb = target.stat().st_size / 1024
        if size_kb < MIN_PDF_KB:
            target.unlink()
            print(f"   no data (empty report, {size_kb:.1f} KB): {label}")
            stats["empty"] += 1
        else:
            print(f"   saved ({size_kb:.0f} KB): {target}")
            stats["saved"] += 1
    elif result == "empty":
        print(f"   no data: {label}")
        stats["empty"] += 1
    elif result == "unknown":
        print(f"   UNKNOWN result (no file, new tab or message appeared): {label}")
        stats["failed"] += 1

    if TEST_ONE and result in ("saved", "unknown", "failed"):   # not after a "no data" result
        input("TEST MODE: check the result, then press Enter to stop...")
        raise StopRun()


def walk_att(page, depth, path, stats):
    if depth == len(ATT_LEVELS):
        process_att(page, path, stats)
        return
    label = ATT_LEVELS[depth]
    opts = real_options(page, label)
    log_options(label, opts)
    for idx, text in opts:
        select_index(page, label, idx)
        walk_att(page, depth + 1, path + [text], stats)


# ----------------------------------------------------------------------------
# Jobs: open the link, choose session + course, loop the rest
# ----------------------------------------------------------------------------
def attach_diagnostics(ctx):
    t0 = time.time()

    def stamp(msg):
        print(f"   [diag +{time.time() - t0:.0f}s] {msg}")

    def hook(pg):
        pg.on("close", lambda *_: stamp(f"page closed: {pg.url[:90]}"))
        pg.on("crash", lambda *_: stamp(f"PAGE CRASHED: {pg.url[:90]}"))
        pg.on("download", lambda d: stamp(f"download started: {d.suggested_filename} <- {d.url[:150]}"))

    ctx.on("close", lambda *_: stamp("BROWSER CONTEXT CLOSED"))
    for pg in ctx.pages:
        hook(pg)
    ctx.on("page", lambda pg: (stamp(f"new tab: {pg.url[:90]}"), hook(pg)))


def launch(p):
    ctx = p.chromium.launch_persistent_context("mponline_profile", headless=False, viewport=None)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    if DEBUG:
        attach_diagnostics(ctx)
    return ctx, page


def apply_job(job):
    global SESSION, COURSE, LEVELS, ATT_LEVELS, TAG, IGNORE
    if job.get("task") not in ("sitting", "attestation"):
        raise ConfigError(f"Job '{job.get('name')}': task must be 'sitting' or 'attestation'.")
    SESSION = job["session"]
    COURSE = job["course"]
    TAG = "_" + job["tag"] if job.get("tag") else ""
    IGNORE = job.get("ignore", {})
    lv = job.get("levels")
    lv = lv if isinstance(lv, list) else None       # "auto" is worked out once the page is open
    if job["task"] == "sitting":
        LEVELS = lv or DEFAULT_LEVELS
    else:
        ATT_LEVELS = lv or DEFAULT_ATT_LEVELS


def open_report_link(page, job):
    if job.get("href"):
        link = page.locator(f"a[href*='{job['href']}' i]:visible")
        what = job["href"]
    elif job.get("text"):
        pat = re.compile(r"^\s*\S?\s*" + re.escape(job["text"]) + r"\s*$")
        link = page.locator("a:visible", has_text=pat)
        what = job["text"]
    else:
        raise ConfigError("Each job needs 'href' or 'text'.")

    if link.count() == 0:                       # expand the Reports section
        try:
            page.get_by_text("Reports", exact=True).first.click(timeout=3000)
            page.wait_for_timeout(1000)
        except Exception:
            pass
    try:
        link.first.wait_for(state="visible", timeout=15000)
    except Exception:
        raise ConfigError(f"Link not found: {what}. Run: python mponline.py links")

    hrefs = [link.nth(i).get_attribute("href") for i in range(link.count())]
    for i, h in enumerate(hrefs):
        print(f"   link {i}: {h}")
    if len(set(hrefs)) > 1:
        print("   WARNING: different links match; using the first. Use a longer 'href'.")

    target = link.first
    print("-> Clicking:", target.get_attribute("href"))
    try:
        with page.context.expect_page(timeout=5000) as info:
            target.click()
        new_page = info.value
        new_page.wait_for_load_state("networkidle")
        return new_page
    except Exception:
        page.wait_for_load_state("networkidle")
        return page


def start_sitting(p, job):
    global LEVELS
    ctx, page = launch(p)
    login(page)
    page = open_report_link(page, job)
    try:
        dismiss_message(page)
    except Exception:
        pass                                    # not every page shows the opening message
    select_row(page, "Select Session", SESSION)
    select_row(page, "Select Course", COURSE)
    LEVELS = resolve_levels(page, job, LEVELS)
    return ctx, page


def start_attestation(p, job):
    global ATT_LEVELS
    ctx, page = launch(p)
    login(page)
    page = open_report_link(page, job)
    page.wait_for_timeout(1500)
    ok = page.locator("input[value='ok' i]")
    if ok.count() > 0 and ok.first.is_visible():
        ok.first.click()
    select_row(page, "Select Session", SESSION)
    select_row(page, "Select Course", COURSE)
    try:
        print("-> Exam Center:", get_dropdown(page, "Exam Center").evaluate(
            "s => s.options[s.selectedIndex] ? s.options[s.selectedIndex].text : '(empty)'"))
    except Exception:
        pass
    ATT_LEVELS = resolve_levels(page, job, ATT_LEVELS)
    return ctx, page


TASKS = {
    "sitting": (start_sitting, lambda page, p, stats: walk(page, p, 0, [], stats)),
    "attestation": (start_attestation, lambda page, p, stats: walk_att(page, 0, [], stats)),
}


def run_job(p, job):
    stats = {"saved": 0, "empty": 0, "skipped": 0, "failed": 0}
    try:
        apply_job(job)
    except ConfigError as e:
        print(f"\n>>> SETTINGS PROBLEM: {e}")
        return
    start, walker = TASKS[job["task"]]
    print(f"\n===== {job.get('name', job['task'])} | {job['session']} | {job['course']} =====")

    for attempt in range(1, 4):                      # up to 3 browser restarts
        ctx = None
        try:
            ctx, page = start(p, job)
            walker(page, p, stats)
            break                                    # finished
        except StopRun:
            break
        except ConfigError as e:
            print(f"\n>>> SETTINGS PROBLEM (not retrying): {e}")
            break
        except Exception as e:
            msg = (str(e).splitlines() or [""])[0][:200]
            print(f"\n>>> interrupted ({type(e).__name__}: {msg}). Restarting (attempt {attempt})...\n")
            time.sleep(3)
        finally:
            try:
                if ctx:
                    ctx.close()
            except Exception:
                pass
    print("DONE:", stats)


def expand(jobs, names):
    """A list of sessions or courses in a job becomes one run for each."""
    for j in jobs:
        if names and j.get("name") not in names:
            continue
        if not names and not j.get("run", True):
            continue
        sessions = j["session"] if isinstance(j["session"], list) else [j["session"]]
        courses = j["course"] if isinstance(j["course"], list) else [j["course"]]
        for s in sessions:
            for c in courses:
                yield {**j, "session": s, "course": c}


# ----------------------------------------------------------------------------
# Backfill: attestation sheets for every session / course that is not downloaded yet
# ----------------------------------------------------------------------------
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
MAX_ATTEMPTS = 3


def resolve_course(options, name):
    """A short name ('LLB', 'BA') -> the course exactly as this page lists it ('LLB(L.L.B.)',
    'BA - B.A.'). A full name that is already exact is returned unchanged. None = not offered."""
    norm = lambda s: " ".join(s.split())
    opts, want = [norm(o) for o in options], norm(name).lower()
    for o in opts:                                    # 1. exactly as written
        if o.lower() == want:
            return o
    for o in opts:                                    # 2. 'BA' -> 'BA - B.A.'
        if o.lower().startswith(want + " - "):
            return o
    cands = [o for o in opts if o.lower().startswith(want + "(")]       # 3. 'LLB' -> 'LLB(L.L.B.)'
    simple = [o for o in cands if re.fullmatch(re.escape(want) + r"\([^()]*\)", o.lower())]
    return (simple or cands or [None])[0]             # MSC(M.Sc.) is preferred to MSC(HS)(...)


def wanted_sessions(options, months, min_year=None, max_year=None):
    """Sessions of the given months from the page's Session list, newest first."""
    found = []
    for o in options:
        m = re.fullmatch(r"([A-Z]{3})-(\d{4})", o.strip().upper())
        if not m or m.group(1) not in months:
            continue
        year = int(m.group(2))
        if (min_year and year < int(min_year)) or (max_year and year > int(max_year)):
            continue
        found.append((year, MONTHS.index(m.group(1)), o.strip()))
    return [s for _, _, s in sorted(found, reverse=True)]


class Ledger:
    """Remembers, per session + course, what was done - so the next run skips it."""

    def __init__(self, path=None):
        self.path = Path(path) if path else documents_dir() / "attestation_backfill.json"
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def get(self, key):
        return self.data.get(key)

    def put(self, key, **fields):
        e = self.data.setdefault(key, {})
        e.update(fields)
        e["at"] = time.time()
        e["when"] = f"{datetime.now():%Y-%m-%d %H:%M}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")


def skip_reason(entry):
    """Why a session + course needs no work now (None = it does)."""
    if not entry:
        return None
    state, age = entry.get("state"), (time.time() - entry.get("at", 0)) / 86400
    if state == "done":
        return f"already downloaded ({entry.get('when', '')})"
    if state in ("empty", "not_offered") and age < BACKFILL_RECHECK_DAYS:
        return f"{state.replace('_', ' ')} when checked on {entry.get('when', '')}"
    if state in ("partial", "error") and entry.get("attempts", 0) >= MAX_ATTEMPTS:
        return f"gave up after {MAX_ATTEMPTS} attempts ({entry.get('note', '')[:60]})"
    return None


class Portal:
    """One logged-in browser reused for many jobs; it is rebuilt when it dies or is logged out."""

    def __init__(self, p):
        self.p, self.ctx, self.page, self.home = p, None, None, None

    def open(self):
        self.close()
        self.ctx, self.page = launch(self.p)
        login(self.page)
        self.home = self.page.url

    def close(self):
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        self.ctx = self.page = None

    def fresh_page(self):
        """The first tab, back on the logged-in home page."""
        if self.ctx is None or self.page is None or self.page.is_closed():
            self.open()
        try:
            for pg in list(self.ctx.pages):
                if pg is not self.page:
                    pg.close()
            self.page.goto(self.home, wait_until="load")
            if self.page.locator("input[type='password']").count() > 0:     # logged out meanwhile
                self.open()
        except PWError:
            self.open()
        return self.page


def branches_to_check(page):
    """The branches this session + course offers (selects the first option of the dropdowns above)."""
    if "Select Branch" not in ATT_LEVELS:
        return []
    for label in ATT_LEVELS[:ATT_LEVELS.index("Select Branch")]:      # e.g. the College dropdown
        opts = [o for o in real_options(page, label) if not o[1].startswith("--")]   # not "--ALL--"
        if not opts:
            return []
        select_index(page, label, opts[0][0])
    return [t for _, t in real_options(page, "Select Branch") if not t.startswith("--")]


def downloaded_already(branches):
    """True if the session's folder already holds a PDF for every one of these branches."""
    if not branches:
        return False
    sess = SESSION.replace("-", "_") + TAG
    base = documents_dir() / sess / f"{sess}_ATTESTATION"
    if "Select College" not in ATT_LEVELS:                              # NEP: one folder per course
        base = base / clean(COURSE)[:40]
    stems = [f.stem for f in base.rglob("*.pdf")] if base.is_dir() else []
    return all(any(s.endswith("_" + clean(b)) for s in stems) for b in branches)


def backfill_job(page, grp, session, short, ledger, key, entry, force):
    """One session + course. Returns (state, note, stats)."""
    global ATT_LEVELS
    job = {**grp, "session": session, "course": short}
    page = open_report_link(page, job)
    page.wait_for_timeout(1000)
    ok = page.locator("input[value='ok' i]")
    if ok.count() > 0 and ok.first.is_visible():
        ok.first.click()

    select_row(page, "Select Session", session, quiet=True)
    sel = get_dropdown(page, "Select Course")
    for _ in range(12):                                  # up to 6 s for the course list to fill
        if sel.locator("option").count() > 1:
            break
        page.wait_for_timeout(500)
    exact = resolve_course([t.strip() for t in sel.locator("option").all_inner_texts()], short)
    if not exact:
        return "not_offered", f"no course like '{short}' in {session}", None
    select_row(page, "Select Course", exact, quiet=True)
    print(f"   course on the page: {exact}")

    apply_job({**job, "course": exact})
    ATT_LEVELS = resolve_levels(page, job, ATT_LEVELS)

    if not force and not entry:                      # never handled by this function before
        branches = branches_to_check(page)
        if downloaded_already(branches):
            return "done", f"files already on disk for all {len(branches)} branch(es)", None

    ledger.put(key, state="running", exact=exact)
    stats = {"saved": 0, "empty": 0, "skipped": 0, "failed": 0}
    walk_att(page, 0, [], stats)
    if stats["failed"]:
        return "partial", f"{stats['failed']} combination(s) failed", stats
    if stats["saved"] + stats["skipped"] == 0:
        return "empty", "no student data for this session / course", stats
    return "done", "", stats


def run_backfill(p, names=None, force=False, plan_only=False):
    groups = [g for g in BACKFILL if not names or g.get("name") in names]
    if not groups:
        print("No BACKFILL group selected. Add a BACKFILL list to jobs.py (see the example) "
              "or check the group name.")
        return
    ledger, portal = Ledger(), Portal(p)
    counts = {}

    def count(label):
        counts[label] = counts.get(label, 0) + 1

    print(f"Memory of finished work: {ledger.path}")
    try:
        for grp in groups:
            months = [m.upper() for m in grp["months"]]
            print(f"\n######## {grp['name']}: months {months}, courses {grp['courses']}"
                  f"{' (NEP)' if grp.get('tag') else ''}")
            sessions = None
            for _ in range(3):
                try:
                    page = open_report_link(portal.fresh_page(), grp)
                    options = [t for _, t in real_options(page, "Select Session")]
                    sessions = wanted_sessions(options, months, grp.get("min_year"), grp.get("max_year"))
                    break
                except (BrowserLost, PWError, PWTimeout) as e:
                    print(f"   >>> could not read the session list ({type(e).__name__}: "
                          f"{(str(e).splitlines() or [''])[0][:100]}); trying again")
                    portal.close()
            if sessions is None:
                print("   could not read the session list; skipping this group")
                continue
            print(f"   {len(sessions)} session(s): {', '.join(sessions)}")

            for session in sessions:
                for short in grp["courses"]:
                    key = f"{session}|{short}|{grp.get('tag', '')}"
                    entry = ledger.get(key)
                    why = None if force else skip_reason(entry)
                    if why:
                        print(f"   skip {session} {short}: {why}")
                        count("skipped")
                        continue
                    if plan_only:
                        print(f"   TO DO: {session} {short}")
                        count("to do")
                        continue

                    print(f"\n=== {session} | {short} ({grp['name']}) ===")
                    state = note = stats = None
                    for attempt in range(1, 4):
                        try:
                            state, note, stats = backfill_job(portal.fresh_page(), grp, session, short,
                                                              ledger, key, entry, force)
                            break
                        except StopRun:
                            raise
                        except ConfigError as e:
                            state, note, stats = "error", str(e)[:200], None
                            break
                        except (BrowserLost, PWError, PWTimeout) as e:
                            msg = (str(e).splitlines() or [""])[0][:100]
                            print(f"   >>> browser problem ({type(e).__name__}: {msg}); restarting (attempt {attempt})")
                            portal.close()
                    if state is None:
                        state, note = "error", "the browser kept failing"
                    attempts = (entry or {}).get("attempts", 0) + (1 if state in ("partial", "error") else 0)
                    ledger.put(key, state=state, note=note, stats=stats, attempts=attempts)
                    print(f"   -> {state}{': ' + note if note else ''}  {stats or ''}")
                    count(state)
    except StopRun:
        print("\nStopped (test mode).")
    except KeyboardInterrupt:
        print("\nStopped by you. Finished work is remembered; run it again to continue.")
    finally:
        portal.close()
    print("\nSUMMARY:", counts or "nothing to do")


def list_links(p):
    ctx, page = launch(p)
    login(page)
    seen = set()
    for a in page.locator("a:visible").all():
        href = a.get_attribute("href") or ""
        text = " ".join(a.inner_text().split())
        if re.search(r"(?i)sitting|attest|docket", text + href) and (text, href) not in seen:
            seen.add((text, href))
            print(f"{text}\n     {href}")
    input("\nPress Enter to close...")
    ctx.close()


def main():
    args = sys.argv[1:]
    with sync_playwright() as p:
        if args[:1] == ["backfill"]:
            rest = args[1:]
            run_backfill(p, [a for a in rest if not a.startswith("--")],
                         force="--again" in rest, plan_only="--plan" in rest)
            return
        if args[:1] == ["links"]:
            list_links(p)
            return
        selected = list(expand(JOBS, args))
        if not selected:
            print("No jobs selected. Check the names and the 'run' flags in jobs.py.")
        for job in selected:
            run_job(p, job)


if __name__ == "__main__":
    main()

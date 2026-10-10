"""explorer.py - opens the report page for one job (read-only, saves nothing) and prints
what the page really offers, as one line:  EXPLORE_JSON: {...}

Usage:  python explorer.py JOB_NAME
Keep it in the same folder as mponline.py and jobs.py.
"""
import re
import sys
import json

from playwright.sync_api import sync_playwright

import mponline as m

norm = lambda s: " ".join(s.split())

DUMP_JS = r"""() => [...document.querySelectorAll('select')]
    .filter(s => s.offsetParent !== null)
    .map(s => {
        const tr = s.closest('tr');
        const td = tr && tr.querySelector('td');
        return {
            label: ((td ? td.innerText : s.name) || '').replace(/\s+/g, ' ').trim(),
            options: [...s.options].map(o => o.text.replace(/\s+/g, ' ').trim()).slice(0, 60)
        };
    })"""


def emit(result):
    print("EXPLORE_JSON: " + json.dumps(result))


def real(options):
    return [o for o in options if o and not o.lower().startswith("select") and not o.startswith("--")]


def options_of(page, label):
    sel = m.get_dropdown(page, label)
    sel.wait_for(state="visible", timeout=15000)
    for _ in range(10):
        if sel.locator("option").count() > 1:
            break
        page.wait_for_timeout(500)
    return [norm(t) for t in sel.locator("option").all_inner_texts()]


def uniq(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


def page_links(page):
    links, seen = [], set()
    for a in page.locator("a:visible").all():
        href = a.get_attribute("href") or ""
        text = norm(a.inner_text())
        if re.search(r"(?i)sitting|attest|docket", text + href) and (text, href) not in seen:
            seen.add((text, href))
            links.append({"text": text, "href": href})
    return links


def explore(page, job_name):
    jobs = list(m.expand(m.JOBS, [job_name]))
    if not jobs:
        return {"problem": "job_not_found", "message": f"job '{job_name}' not in jobs.py"}

    base = jobs[0]
    wanted_s = uniq([j["session"] for j in jobs])
    wanted_c = uniq([j["course"] for j in jobs])
    result = {"job": job_name, "task": base.get("task"), "href": base.get("href"),
              "text": base.get("text"), "wanted_sessions": wanted_s, "wanted_courses": wanted_c}

    m.login(page)
    try:
        page = m.open_report_link(page, base)
    except m.ConfigError as e:
        result.update(problem="link_not_found", message=str(e), links=page_links(page))
        return result

    try:
        m.dismiss_message(page)
    except Exception:
        pass
    ok = page.locator("input[value='ok' i]")
    if ok.count() > 0 and ok.first.is_visible():
        ok.first.click()

    avail_s = real(options_of(page, "Select Session"))
    result["sessions_available"] = avail_s
    have_s = {norm(s) for s in avail_s}
    result["missing_sessions"] = [s for s in wanted_s if norm(s) not in have_s]

    courses_by_session, missing_courses, first_ok = {}, {}, None
    for s in wanted_s:
        if norm(s) not in have_s:
            continue
        m.select_row(page, "Select Session", s)
        cs = real(options_of(page, "Select Course"))
        courses_by_session[s] = cs
        have_c = {norm(c) for c in cs}
        miss = [c for c in wanted_c if norm(c) not in have_c]
        if miss:
            missing_courses[s] = miss
        elif first_ok is None:
            first_ok = s
    result["courses_by_session"] = courses_by_session
    result["missing_courses"] = missing_courses

    if first_ok:  # open the full form to see which dropdowns it has
        m.select_row(page, "Select Session", first_ok)
        m.select_row(page, "Select Course", wanted_c[0])
        result["dropdowns"] = page.evaluate(DUMP_JS)
        labels = m.page_labels(page)
        result["dropdown_labels"] = labels
        lv = base.get("levels")
        if lv != "auto":
            if not isinstance(lv, list):
                lv = m.DEFAULT_LEVELS if base.get("task") == "sitting" else m.DEFAULT_ATT_LEVELS
            result["configured_levels"] = lv
            result["missing_levels"] = [l for l in lv if not any(l in lab for lab in labels)]

    result["problem"] = bool(result["missing_sessions"] or missing_courses
                             or result.get("missing_levels"))
    return result


def list_links_mode():
    """python explorer.py --links  -> prints LINKS_JSON: [...] of report links after login."""
    with sync_playwright() as p:
        ctx, page = m.launch(p)
        try:
            m.login(page)
            links = page_links(page)
            if not links:                      # expand the Reports section
                try:
                    page.get_by_text("Reports", exact=True).first.click(timeout=3000)
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
                links = page_links(page)
            print("LINKS_JSON: " + json.dumps(links))
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def main():
    if sys.argv[1:2] == ["--links"]:
        return list_links_mode()
    if len(sys.argv) < 2:
        sys.exit("Usage: python explorer.py JOB_NAME   |   python explorer.py --links")
    with sync_playwright() as p:
        ctx, page = m.launch(p)
        try:
            emit(explore(page, sys.argv[1]))
        finally:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
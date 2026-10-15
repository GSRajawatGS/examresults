"""
student_search.py - every exam of one roll number (or enrollment number), in detail, from the command line.

    python student_search.py 251041691            result of every exam, marks of every paper; writes an Excel file
    python student_search.py 251041691 --no-link  only this roll number (not the same student's other roll numbers)

It reads Supabase if it is set up (.env), otherwise the class Excel files in your Documents folder.
The Excel file is written to Documents\\student_<roll>.xlsx. The app has the same search (Student search page).
"""
import sys
from pathlib import Path

import analytics as an
import db
import result_report as rr


def main():
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if not a:
        sys.exit(__doc__)
    q, link = a[0], "--no-link" not in sys.argv
    docs = Path(rr.DOCS_ROOT) if rr.DOCS_ROOT else rr.documents_dir()
    use_db = db.configured() and "--local" not in sys.argv
    print(f"Searching {q} in {'Supabase' if use_db else docs} ...")
    frames = an.db_frames(db.connect()) if use_db else an.local_frames(docs)
    res = an.find_student(frames, q, link)
    if not res["found"]:
        sys.exit("Not found in the results data.")
    p = res["profile"]
    print(f"\n{p.get('name', '')}   father: {p.get('father_name', '')}   enrollment: {p.get('enrollment_no', '')}")
    print("roll numbers:", ", ".join(res["rolls"]))
    print("\nALL EXAMS")
    print(res["exams"].to_string(index=False))
    for d in res["details"]:
        k = d["key"]
        print(f"\n--- {k['session']} {d.get('class_label') or k['exam_code']} roll {k['roll_no']}: {d['result']} ---")
        if len(d["papers"]):
            print(d["papers"].to_string(index=False))
    out = docs / f"student_{q}.xlsx"
    out.write_bytes(an.student_workbook(res))
    print(f"\nExcel file: {out}")


if __name__ == "__main__":
    main()

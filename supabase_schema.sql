-- supabase_schema.sql
-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- It is safe to run again (everything is "if not exists" / "or replace").
--
-- What goes where
--   attestation_students  one row per student per attestation PDF - every course, normal and NEP, whether or
--                         not results exist yet (straight from the PDFs mponline.py downloads)
--   students           one row per student per class: the attestation-sheet data (name, father, address ...)
--   marksheets         one row per student per class: the result found on the result site (status, total, result ...)
--   marksheet_papers   one row per student per paper: the marks of every component (theory, sessional ...)
--   sitting_plans      one row per sitting-plan PDF: class, paper, and the roll numbers sitting that paper
--   student_results    a view that joins students + marksheets (what the app reads)
--
-- "session" is like 'JUN-2026'; "exam_code" is the class, like 'LL323' (main) or 'ALL323' (ATKT);
-- a result found in a later chance (A2LL323, 2LL323) is still filed under the class's own exam_code,
-- and marksheets.result_exam_code says which exam it came from.

create table if not exists attestation_students (
    id              bigint generated always as identity primary key,
    session         text not null,             -- 'JUN-2026'
    tag             text not null default '',  -- 'NEP' or ''
    file_key        text not null,             -- path of the PDF below <SESSION>_ATTESTATION
    roll_no         text not null,
    folder          text,                      -- college folder (normal) / course folder (NEP)
    course          text,                      -- first part of the Exam Name line, e.g. LLB
    branch          text,
    sem             text,                      -- 2SEM / 1YEAR
    regular_private text,                      -- from the PDF file name
    status          text,                      -- REGULAR / EXR / ATKT ... (from the PDF file name)
    exam_code       text,                      -- result-site code when the course is known (ALL323), else null
    enrollment_no   text,
    regular_backlog text,
    exam_name       text,
    exam_centre     text,
    college_name    text,
    name            text,
    father_name     text,
    mother_name     text,
    gender          text,
    category        text,
    address         text,
    papers          text,
    updated_at      timestamptz not null default now(),
    unique (session, tag, file_key, roll_no)
);
create index if not exists attestation_roll_idx on attestation_students (roll_no);
create index if not exists attestation_session_idx on attestation_students (session, tag, course);

create table if not exists students (
    id                  bigint generated always as identity primary key,
    session             text not null,
    exam_code           text not null,
    roll_no             text not null,
    name                text,
    father_name         text,
    mother_name         text,
    enrollment_no       text,
    regular_backlog     text,
    exam_name           text,
    exam_centre         text,
    college_name        text,
    gender              text,
    category            text,
    address             text,
    papers              text,
    class_label         text,
    att_regular_private text,
    att_status          text,
    att_branch          text,
    source_file         text,
    updated_at          timestamptz not null default now(),
    unique (session, exam_code, roll_no)
);
create index if not exists students_roll_idx on students (roll_no);
create index if not exists students_name_idx on students (lower(name));

create table if not exists marksheets (
    id                bigint generated always as identity primary key,
    session           text not null,
    exam_code         text not null,
    roll_no           text not null,
    result_status     text,                 -- OK / NOT FOUND / ERROR
    result_note       text,
    result_exam_code  text,                 -- the exam the result was found in (ALL323, A2LL323 ...)
    fetched_at        timestamptz,
    title             text,
    ms_roll_number    text,
    ms_enrollment     text,
    ms_name           text,
    ms_father         text,
    ms_mother         text,
    abcid             text,
    total_obtained    numeric,
    total_max         numeric,
    percentage        numeric,
    final_result      text,
    extra             jsonb,                -- any other "Final ..." / "Marksheet ..." field of the site
    raw_text          text,
    updated_at        timestamptz not null default now(),
    unique (session, exam_code, roll_no)
);
create index if not exists marksheets_status_idx on marksheets (session, exam_code, result_status);

create table if not exists marksheet_papers (
    id          bigint generated always as identity primary key,
    session     text not null,
    exam_code   text not null,
    roll_no     text not null,
    paper       text not null,
    components  jsonb,                      -- {"Theory I": "55C", "Sessional I": "12", "Total": "55"}
    total       text,
    total_num   numeric,
    unique (session, exam_code, roll_no, paper)
);
create index if not exists marksheet_papers_paper_idx on marksheet_papers (session, exam_code, paper);

create table if not exists sitting_plans (
    id              bigint generated always as identity primary key,
    session         text not null,
    tag             text not null default '',   -- 'NEP' or ''
    file_key        text not null,              -- folder/file below the session folder
    exam_centre     text,
    class_line      text,                       -- "LLB / 2SEM / REGULAR / REGULAR / JUN-2026"
    branch          text,
    sem             text,
    regular_private text,
    status          text,
    paper_code      text,
    paper_name      text,
    total_students  integer,                    -- the "Total:" printed on the sheet
    roll_count      integer,                    -- roll numbers actually read
    rolls           text[],
    updated_at      timestamptz not null default now(),
    unique (session, tag, file_key)
);
create index if not exists sitting_rolls_idx on sitting_plans using gin (rolls);

create or replace view student_results as
select s.*,
       m.result_status, m.result_note, m.result_exam_code, m.fetched_at, m.title,
       m.ms_name, m.ms_father, m.ms_mother, m.abcid,
       m.total_obtained, m.total_max, m.percentage, m.final_result
from students s
left join marksheets m using (session, exam_code, roll_no);

-- Security: the tables hold personal data of students. Row level security is switched on with NO
-- policies, so nobody using the public (anon) key can read or write them. The scripts and the app
-- use the service-role key (SUPABASE_KEY in .env), which bypasses these rules - keep that key private.
alter table attestation_students enable row level security;
alter table students         enable row level security;
alter table marksheets       enable row level security;
alter table marksheet_papers enable row level security;
alter table sitting_plans    enable row level security;
alter view  student_results  set (security_invoker = true);

# FHIR Patient Migration: working slice

This is a working implementation of [Plan.md](Plan.md) against the public [HAPI FHIR R4 sandbox](https://hapi.fhir.org/baseR4):

1. FHIR Bulk Data `$export` produces NDJSON files.
2. The files are migrated into an internal Patient/Observation schema in SQLite.
3. Verification compares the files with the database.
4. The exports are deleted on the server and locally.

A read-only REST API and a Vue UI show the migrated data.

> **Synthetic data only.** This only talks to the public HAPI sandbox. Don't point `FHIR_BASE_URL` at a server that holds real patient data.

## Repository layout

```
Plan.md                     Part 1: the migration plan (with the DB schema)
backend/                    Django 5.2 LTS + Django REST Framework
  config/                   settings and root URLs
  fhir_migration/           the Plan's four scripts and their audit tables
    models.py               ExportJob, ExportFile, MigrationRun, MigrationChunk,
                            VerificationRun, VerificationIssue
    fhir_client.py          HTTP calls of the $export flow + retry/backoff policy
    mapping.py              FHIR resource -> model fields (pure functions)
    export.py               Export script (+ Delete script)
    migration.py            Migration script (phases, chunks, resume, --issues, rollback)
    verification.py         Verification script
    management/commands/    export_fhir, migrate_fhir, verify_fhir, delete_fhir_export
  patients/                 the internal service: Patient, Observation + REST API
frontend/                   Vue 3 + Vite + TypeScript
  src/App.vue                             page, global styles and the observations modal
  src/components/PatientList.vue          patient table (fetches its own data)
  src/components/PatientObservations.vue  one patient's observations table
  src/useFetch.ts                         shared, abortable fetch
```

## Requirements

- Python 3.13 (supported by Django 5.2 LTS)
- Node.js 24 LTS (or ≥ 22.18)

## Setup

```bash
cd backend
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate          # create the SQLite schema
```

For editor autocomplete and types (Pylance), also run `pip install -r requirements-dev.txt` and select `backend/.venv` as the interpreter. It only adds Django and DRF type stubs. `pyrightconfig.json` at the root points Pylance at that venv, and the reverse relations (`job.files`) are declared for the editor under `TYPE_CHECKING` in `models.py`.

## Configuration

Settings come from environment variables. For local development, copy `backend/.env.example` to `backend/.env` (git-ignored); variables already set in the system environment win over the file.

| Variable | Default | Effect |
|---|---|---|
| `ENVIRONMENT` | `dev` | `dev` or `prod` (anything else fails at startup). `prod` turns off DRF's browsable API, so the API answers JSON only |
| `FHIR_BASE_URL` | the HAPI sandbox | The FHIR server to export from |
| `CODE_VERSION` | the git short hash | Stored on each migration run |

## Running the migration

These are the Plan's four scripts, in order:

```bash
cd backend && source .venv/bin/activate   # skip if you're already there with the venv active

python manage.py export_fhir             # POST /$export, poll, download -> "Export 1"
python manage.py migrate_fhir 1          # phase 1 patients -> verify -> phase 2 observations
python manage.py verify_fhir 1           # full verification; lists issues by check
python manage.py delete_fhir_export 1    # DELETE on the server + remove local NDJSON
```

The `1` after the command is the **export folder id**: the number `export_fhir` prints ("Export 1"), which is also the name of the export's folder, `backend/exports/1/`. In the table below it appears as `<export_folder_id>`.

The export takes about 2 minutes, because the sandbox answers each poll with `Retry-After: 120` and the script honours it. Each step prints the next command to run.

> **Test-only limit:** the sandbox is far too large to export in full, so the first export only asks for what changed in the last day (`TEST_ONLY_FIRST_EXPORT_WINDOW` in `export.py`, about 4–5k resources). In a real migration the first export is full (`since=None`).

`since` always comes from the previous export's `transactionTime`, stored in the DB. It's never typed by hand, because a date that's too late would silently skip changes.

The other flows from the Plan:

| Command | Plan section |
|---|---|
| `export_fhir` (again), then `migrate_fhir <export_folder_id>` | Delta export: `since` is the previous export's `transactionTime`. It also fetches, automatically, the ids the source still has (`_elements=id`), and the migration removes local records missing from that list |
| `export_fhir` (after a failure) | Resumes the latest export if it's `dead`, `failed` or interrupted, from where it stopped |
| `migrate_fhir <export_folder_id>` (again) | Resume an unfinished run (pending/failed chunks), or re-run safely (upserts) |
| `verify_fhir <export_folder_id> [--type Patient]` | On-demand verification |
| `migrate_fhir --issues <verification_run_id>` | Re-process only the flagged lines |
| `migrate_fhir --rollback <migration_run_id>` | Remove the rows a bad run wrote |
| `delete_fhir_export <export_folder_id> [--force]` | A completed export: refuses unless its latest migration completed and verification passes (`--force` skips that). An unfinished export can't be migrated, so it's deleted without checks |

**What you'll see on the sandbox:** a few observations have no `status`, which FHIR requires (1..1). They're migrated with status `unknown`, a valid value, so they aren't lost. A line that can't be mapped at all (invalid JSON, no `id`) is skipped instead: its chunk is marked `failed`, `verify_fhir` reports it as a `bad_line` issue, and `delete_fhir_export` refuses until it's handled (or you pass `--force`). After a mapping fix, `migrate_fhir --issues <verification_run_id>` re-processes just those lines. This is the Plan's "bad data that needs a mapping fix" path.

## Running the app

```bash
# terminal 1
cd backend && source .venv/bin/activate && python manage.py runserver   # http://127.0.0.1:8000/api/
# terminal 2
cd frontend && npm install && npm run dev                               # http://localhost:5173
```

Vite proxies `/api` to Django, so the backend doesn't need any CORS setup. In `dev`, opening an API URL in a browser shows DRF's browsable API.

| Method & path | Response |
|---|---|
| `GET /api/patients/` | `200`: patients with `observation_count` |
| `GET /api/patients/{id}/` | `200`: one patient with its `observations`; `404` if unknown |

## Tests

```bash
cd backend && source .venv/bin/activate && python manage.py test
```

Type check, from the repo root (it reads `pyrightconfig.json`, so it needs `requirements-dev.txt` installed in `backend/.venv`):

```bash
npx pyright
```

There are 28 tests. They use synthetic in-code resources and mock the HTTP calls, so they never touch the network.

- **Mapping:** official name, MR identifier, partial dates, a missing `status` becoming `unknown`, reference parsing.
- **Kickoff:** all types in one comma-joined `_type`, `_elements=id` for ID-only exports.
- **Retry policy:** backoff, `Retry-After`, no retry on 4xx, giving up after the maximum number of attempts.
- **Export:**
  - polling until ready, honouring `Retry-After`
  - a kickoff that keeps failing marks the job `dead`
  - a download that keeps failing goes through manifest re-fetch, then a re-export of its type; old files become `dead` and the replaced export gets a `DELETE`
  - a delta export also downloads the source's ids after the data export, then `DELETE`s that ID-only export
  - the command resumes an unfinished export, otherwise continues the `since` chain
- **Migration:**
  - patients before observations, orphans, idempotent re-runs
  - patients failing verification stop the run after 3 fix rounds, before phase 2
  - `--issues` links orphans once their patient exists
  - tombstones remove local records; so do the delta's ids files when the source deleted silently
  - the reconcile refuses to delete an empty list or more than 10% of a table
  - `SUBSETTED` resources create records but never overwrite them
  - rollback
- **Verification:** missing and changed rows.
- **Delete:** refuses until the data is migrated and verified; fails loudly if local files can't be removed.
- **API:** list, detail, 404.

## How the Plan maps to the code

- **Reliability:**
  - **Retries:** every HTTP call goes through `with_retries`. It retries network errors, 429s and 5xx responses with exponential backoff (1s, 2s, 4s…, 5 tries) and waits at least the server's `Retry-After`. Polling uses the same rule, capped at 5 minutes, with a 1-hour overall timeout.
  - **Download fallback:** a download that keeps failing goes through a manifest re-fetch, one more try, and finally a re-export of that resource type.
  - **Re-runnable steps:** every step is recorded in the audit tables, so every command can be re-run.
- **API limits:** Bulk export is a single async job instead of thousands of paged searches. The server sets the pace through `Retry-After`.
- **Observability:**
  - Logs go to stdout: `X-Progress` on each poll, one line per file and per chunk, and per-line warnings for skipped lines.
  - Each run's state can be queried in the audit tables: per-file attempts and status, per-chunk status, and verification issues.
- **Parallelism:** chunks run in a thread pool (`--workers`). SQLite is set up with WAL and `IMMEDIATE` transactions, so writers queue for the lock instead of failing. On Postgres, the same code would write truly in parallel.
- **Atomicity:** each chunk commits as a unit. A crash leaves it `in_progress`, and the next run re-processes it.
- **Mapping:** follows the Plan's tables exactly, in `mapping.py`. Partial dates such as `"1980"` become `null` rather than being guessed, and a date-only `effectiveDateTime` becomes midnight UTC.

### Where the implementation interprets the Plan

- **Logging instead of a log table:** the Plan's schema has no table for skipped lines or orphans, so they're logged to stdout. Verification still finds them from the files and the DB:
  - a skipped line shows up as a `bad_line` or `missing` issue
  - an orphan whose patient now exists shows up as `orphan_linkable`
  - other orphans count as explained: their subject isn't a Patient, or their patient is still missing
- **A missing `status` becomes `unknown`:** FHIR requires `Observation.status` (1..1), but some sandbox observations lack it. `unknown` is a value of FHIR's observation status list, so the record is kept. The fact that the source had no status isn't recorded.
- **The ID-only export lives on the delta's job:** its files are `ExportFile`s of kind `ids`. The job's one `polling_url` stays the data export's (the Delete script needs it), so the ID-only export is `DELETE`d on the server right after its files are downloaded.
- **Re-exports reuse the job:** a re-export of one type stays on the same `ExportJob`. It gets the new `polling_url`, and the files it replaces become `dead`. The first `transactionTime` is kept, because it's the safer `since` for the next delta.
- **`SUBSETTED` resources** (many sandbox patients are stored in summary mode): the IG says an incomplete resource must not overwrite a complete one. So a `SUBSETTED` line creates the record if it's new and leaves an existing record alone.
- **`_type` is comma-joined:** the IG asks for one `_type` parameter per value, but HAPI only honours the first of several. The other types are dropped, with no error and no `outcome` file, so an export would silently come back without Observations. A stricter provider may need the repeated form.
- **HAPI ignores `_elements=id`:** it returns full resources. The reconcile only reads `id`, so it works either way.
- **Comparing the manifest `count`:** the comparison is implemented, but HAPI doesn't send `count`. The check falls back to the line count of each file against the lines the chunks read.

## Responsible data handling

This is synthetic data, but it's handled as if it were PHI:

- NDJSON files go to `backend/exports/`, which is git-ignored along with `db.sqlite3`.
- `delete_fhir_export` removes the files on the server and locally once verification passes. A replaced export is `DELETE`d as soon as it's superseded.
- Logs and verification issues contain ids, line numbers, field *names* and error types, never field values or raw lines.
- The API is read-only.

A real migration would add what the Plan's Security section lists:

- TLS
- encryption at rest
- SMART Backend Services auth (the manifest says `requiresAccessToken: true`)

On top of that you'd add authentication on this API, which is out of scope here.

## Where I used AI

<!-- TODO(author): check that this describes accurately how you used AI. -->
- **Plan.md (Part 1):** written by me. Claude updated it afterwards only with my approval, so it matches the code.
- **Strategy:** the Plan is the contract. I gave Claude Code (Claude Opus) the Plan and my constraints (Django and Vue LTS, the framework CLIs, KISS/AHA, colocation, a monorepo) and asked it to implement the Plan, not its own design. When the result departed from the Plan, I sent it back: the first version used the search API instead of Bulk Export, and was rebuilt.
- **What Claude did:** probed the sandbox's `$export` behaviour, scaffolded the project with the official CLIs, and drafted the scripts, models, tests, Vue components and this README.
- **What I did:** reviewed each step against the Plan and the FHIR Bulk Data IG, decided the behaviours (resume and delta rules, the ID-only reconcile, `SUBSETTED` handling), and ran it end to end on the sandbox and in the browser.
- **Next features:** the same loop. The Plan changes first, then Claude implements it, then I review.

## What I'd do next

In order of value:

1. **Bulk upserts.** Use `bulk_create(update_conflicts=True)` for each chunk instead of one `update_or_create` per line, and cache the patient lookups. This is the main thing standing between this code and 50k patients.
2. **Postgres.** Real parallel writes, and no SQLite limit on `IN (...)` parameters. The reconcile's `exclude(fhir_id__in=...)` needs batching on SQLite at scale.
3. **SMART Backend Services auth** in `fhir_client`, refreshing the token on long polls.
4. **Encryption at rest** for `exports/`, and a retention timer that deletes files even when nobody runs the Delete script.
5. **Frontend tests** (Vitest) for the loading, error and empty states.
6. **Real full export.** The first export is limited to the last day only because the public sandbox is too big (`TEST_ONLY_FIRST_EXPORT_WINDOW`). Make the window a setting where `None` means `since=None`, and add `export_fhir --full` to start over from scratch. If the latest export is unfinished, it would ask in the terminal before deleting it, by calling the Delete script.
7. **Migration order guards.** A new delta should wait until the previous export is migrated, and an older export shouldn't be migrated after a newer one. Without them, a skipped export leaves a gap in the data, and a late one overwrites newer records with older ones.
8. **Jitter and caps in the retry policy.** Add random jitter to the backoff, so clients that fail together don't retry together, and cap `Retry-After` (for example at 5 minutes) so a very long value from the server can't block the script. Polling already has a cap and an overall timeout, but no jitter.
9. **Pagination** on `GET /api/patients/` (and on a patient's observations), once the list outgrows a single response. The Vue list would then load pages instead of filtering the whole list in the browser.
10. **Production config:** move `SECRET_KEY`, `DEBUG` and `ALLOWED_HOSTS` to the environment, and use structured (JSON) logs.

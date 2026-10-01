# Project context

Internal Ledger is a Python/SQLite JSON API built for a take-home assignment. The priority is financial correctness and inspectable code, not a UI or a production payments platform. [README.md](README.md) is the canonical reviewer runbook; keep its commands aligned with the implementation.

## Repository and live service

- Source: https://github.com/SomneelSaha2042/phillip-ledger — branch `main`.
- Live API: https://phillip-ledger-production.up.railway.app.
- Public readiness: https://phillip-ledger-production.up.railway.app/health.
- Ledger routes require a privately supplied review token. There is no frontend. Readiness does not prove financial integrity.

## Code map

| File | Responsibility |
| --- | --- |
| `app.py` | Input validation, financial operations, SQLite schema/transactions, HTTP routing and startup configuration |
| `demo.py` | Executable local/live HTTP walkthrough, token loading, failure assertions and saved-history/retry check |
| `test_app.py` | Isolated unit, HTTP, concurrency, crash, model-based and CLI-restart tests |
| `Dockerfile` | Python 3.12 runtime; bind to 0.0.0.0 and runtime PORT; DB at /data/ledger.db |
| `.gitignore`, `.dockerignore` | Exclude credentials, databases and generated files |

Only Python standard-library modules are required. SQLite needs STRICT-table support (3.37+). No ORM, framework, PostgreSQL service or JavaScript build step is involved.

## Local review

From the repository directory, in terminal one:

```sh
python app.py serve --db ledger.db --port 8000
```

In terminal two:

```sh
python demo.py
python -B -m unittest -v
```

Expect `ALL CHECKS PASSED` from the demo and 34 tests / `OK` from the suite. Local and live databases are separate. The suite uses temporary data and test credentials, not the user's database or `.env`. Use another DB filename/port for an independent local ledger; never delete user data to reset a test.

## Live review and credentials

The reviewer obtains the token privately and saves a UTF-8 `.env` **beside `demo.py`**, containing only the actual credential assignment (replace the placeholder):

```dotenv
LEDGER_API_TOKEN=PASTE_THE_ACTUAL_REVIEW_TOKEN_HERE
```

Then run:

```sh
python demo.py --url https://phillip-ledger-production.up.railway.app
```

The demo loads only this key from the file; it does not execute shell syntax or expand other variables. Process `LEDGER_API_TOKEN` takes precedence over the file, including stale/invalid values. To use the file after setting a different token, clear that process variable first (`Remove-Item Env:LEDGER_API_TOKEN -ErrorAction SilentlyContinue` in PowerShell, `unset LEDGER_API_TOKEN` in Bash). Shell environment changes do not propagate to unrelated terminal/agent processes.

Only the demo auto-loads `.env`. The server, Docker and manual HTTP clients still need explicit process/platform environment settings. Do not implement a server `.env` fallback accidentally: hosting must receive its secret from Railway Variables. Do not print credentials, put them in CLI arguments, commit them or include them in Docker build context. `.env` is intentionally ignored. Preserve the user's credential file during edits and tests.

The live demo creates uniquely named synthetic accounts and immutable records, leaves its cash account at USD 100.00 and destination accounts at zero after reversals, and prints its prefix. Repeating a fresh demo adds a new account group; nothing is deleted. Its global audit can fail if another account in the shared ledger is corrupt.

After a full run, use the exact printed command to check saved data:

```sh
python demo.py --url https://phillip-ledger-production.up.railway.app --prefix YOUR_PRINTED_PREFIX --check-only
```

This replays the original transfer key after reversal and checks balances, ordered history, pagination and integrity. For **redeploy persistence**, the owner must restart/redeploy first, then the reviewer runs this command. Do not present a check-only run without a restart as a redeploy test.

## Hosting and deployment boundaries

Railway must have one service instance, a persistent volume at `/data`, `LEDGER_API_TOKEN` set in the correct service/environment, and `/health` configured as deployment healthcheck. The image supplies `LEDGER_DB_PATH=/data/ledger.db`. The domain target port must match the injected runtime PORT and startup log, not necessarily the local default 8000. Apply variable/volume changes and redeploy before testing the new settings. See [the deployment runbook](README.md#deploy-the-live-demo-on-railway).

SQLite files, WAL and shared-memory files belong on the same volume. No volume/no in-volume DB path causes startup refusal on Railway. Avoid multiple writers on separate service replicas; SQLite's writer lock is within one shared database, not a distributed lock. Volume-backed redeploys may briefly interrupt traffic. A code push can trigger Railway deployment; changing/pushing code or restarting the live service requires the user's authorization for that workflow.

## Financial invariants and deliberate limits

- Money is integer cents; FX uses Decimal with half-even destination rounding. Supported currencies all use two decimal places; rates are caller-supplied.
- Acquire `BEGIN IMMEDIATE` before funds/key checks. Persist transaction, both balances and both signed entries atomically. Never fix a concurrency bug by separately updating accounts.
- Idempotency keys and normalized request fingerprints persist in SQLite. Retry uncertain outcomes with the same key/payload. Different payload under the same key is a conflict.
- Journal records are append-only. Corrections append one linked reversal, restoring exact original cents. The recipient must still have funds; reversals cannot be reversed again.
- Audits independently reconstruct balances and match journal entries. Keep totals separate by currency, including explicit FX adjustments; never add currencies as one monetary total.
- Reconciliation is on-demand for completed UTC months, excludes accounts opened after the cutoff, and stores immutable successful/failed reports. It is internal history reconciliation, not a bank-statement comparison.
- STRICT tables, foreign keys/checks/triggers and schema version 1 protect normal writes. Unknown schemas are refused, not silently migrated. An administrator controlling the DB file is outside the immutability guarantee.
- Writers are serialized, audits scan the journal in memory, and the stdlib HTTP server is for controlled review traffic. The shared bearer token is not per-user authorization. Real funds need a production server/gateway, access controls, backups/restore drills, monitoring and operational reconciliation.

## Verification record and next check

On 2026-10-01 all 34 tests passed (`python -B -m unittest -q`), including a real authenticated CLI-process restart with a persistent temporary DB and demo token loading from a temporary `.env`. The exact documented live demo command also passed with automatic loading of the local review token (`demo-0e35f68091c8`). An earlier live run and its follow-up saved-history/idempotency check passed as well. The token was not printed or published. The auth issue came from assuming another shell's environment would reach the demo; the client now supports its own ignored `.env`, with process environment taking precedence.

An actual **Railway redeploy-persistence check remains pending** until the owner redeploys and the same printed-prefix check passes afterward. Docker was not available locally for an image build; live functionality checks do not imply a local Docker build was verified. Update this record only after running the corresponding check; avoid claiming unperformed deployment, load or security tests.

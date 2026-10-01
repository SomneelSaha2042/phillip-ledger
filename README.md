# Internal Ledger

A small, durable JSON API for moving money between accounts. Built for the internal-ledger take-home assignment: atomic transfers, accurate balances, ordered history, safe retries, reversals, currency conversion, concurrent access and month-end reconciliation.

**Reviewer quick start:** start the server, run `python demo.py`, then run `python -B -m unittest -v`. The demo checks real HTTP responses and prints the balances after each step. No UI, package installation or external database is required.

- [Run locally](#run-locally) — an independent SQLite ledger on your computer.
- [Test the existing live Railway deployment](#test-the-existing-live-railway-deployment) — no server setup needed; obtain the review token privately.
- [Automated verification](#verification-and-submission) — isolated tests, no live-service writes.
- [CONTEXT.md](CONTEXT.md) — code map, safety rules and recorded verification status.

## Run locally

Requirements: Python **3.12+** with SQLite **3.37+** (for STRICT tables). Check your installation:

```sh
python --version
python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

For a fresh checkout:

```sh
git clone https://github.com/SomneelSaha2042/phillip-ledger.git
cd phillip-ledger
```

If you already have the repository, use that directory instead. Open a terminal there and start the API:

```sh
python app.py serve --db ledger.db --port 8000
```

The server listens on `http://127.0.0.1:8000`; the database and schema are created automatically. Leave this terminal running. In a second terminal, from the same directory:

```sh
python demo.py
python -B -m unittest -v
```

On macOS/Linux, use `python3` if that is your Python 3 command. Stop with Ctrl+C. Restart with the **same database path** to retain balances, history and retry keys. For an independent ledger, choose a different `--db` filename; do not delete an existing financial database to reset it.

If port 8000 is already in use, run these in separate terminals:

```sh
python app.py serve --db demo-ledger.db --port 8001
python demo.py --url http://127.0.0.1:8001
```

Opening `/` in a browser shows API information, not a dashboard. `/health` returns `{"status":"ok"}` when the database can be read; `/favicon.ico` is not implemented.

Local startup needs no token. The **server does not load `.env`**; it reads process environment variables and CLI options. If you explicitly set `LEDGER_API_TOKEN` before starting it, the local demo must use the same token. An existing live token in `.env` does not enable authentication on the local server. Local and Railway databases are completely separate.

## Test the existing live Railway deployment

Live API: **https://phillip-ledger-production.up.railway.app**

Public readiness check: [GET /health](https://phillip-ledger-production.up.railway.app/health). Expect `{"status":"ok"}`. This confirms database access, not financial correctness; all ledger routes require the review token. Availability is not guaranteed indefinitely.

1. Clone/open this repository and ensure Python 3.12+ is available. You do not need Docker, a local server or a Railway account to test the deployed API.
2. Obtain the token privately from the project owner. Create a UTF-8 file named **`.env`** next to **`demo.py`**, with this assignment, replacing the placeholder:

   ```dotenv
   LEDGER_API_TOKEN=PASTE_THE_ACTUAL_REVIEW_TOKEN_HERE
   ```

   Paste only the actual token: no `Bearer ` prefix, no angle brackets, and no spaces. `.env` is excluded by both Git and Docker; never publish it. The demo reads this file automatically, including when invoked from another working directory. It accepts a plain assignment or a value in matching single/double quotes; comments must be on their own lines. It reads only this one key, not arbitrary shell commands or variable interpolation.
3. Run the exact command below; **no separate environment-loading command is necessary**:

   ```sh
   python demo.py --url https://phillip-ledger-production.up.railway.app
   ```

   Expect `ALL CHECKS PASSED`. This creates unique synthetic accounts, transfers/reversals and reconciliation reports in the shared live ledger. Records are immutable and remain after the demo. Use synthetic data only.
4. Save the prefix and follow-up command printed by the script. You can run the `--check-only` command immediately to verify saved history and safe retries. To prove **redeploy persistence**, ask the owner to restart/redeploy the Railway service first, then run that command again against the same URL. Merely running it twice does not test a redeploy.

**Token precedence:** an existing `LEDGER_API_TOKEN` in the Python process environment overrides `.env`, even if that value is stale or invalid. If you previously set a different token, clear it before relying on the file:

PowerShell:

```powershell
Remove-Item Env:LEDGER_API_TOKEN -ErrorAction SilentlyContinue
python demo.py --url https://phillip-ledger-production.up.railway.app
```

macOS/Linux:

```sh
unset LEDGER_API_TOKEN
python3 demo.py --url https://phillip-ledger-production.up.railway.app
```

Environment-only use is also supported: set the token in the **same terminal** that launches Python. Shell variables do not automatically transfer to a separate terminal, another user's process or an agent's tools. A missing remote token fails before any network request; invalid formatting is reported without printing its value.

## What the demo proves

`demo.py` creates three uniquely named, synthetic accounts and verifies:

| Step | Expected result |
| --- | --- |
| Create accounts | USD cash 100.00, USD reserve 0.00, EUR account 0.00 |
| Transfer USD 30.00 | USD balances become 70.00 / 30.00 |
| Repeat the same request and key | Same transaction response; no second debit |
| Change the amount with the same key | 409; balances unchanged |
| Attempt overdraft / fractional cent | 409 / 400; balances unchanged |
| Convert USD 10.00 at 0.91 | USD cash 60.00, EUR account 9.10 |
| Reverse both transfers | Balances return to 100.00 / 0.00 / 0.00 |
| Retry reversals / attempt second reversals | Retries return the same records; different reversal keys get 409 |
| Read paginated history and verify integrity | Four cash movements: -30.00, -10.00, 30.00, 10.00; integrity passes |
| Reconcile the previous UTC month | Immutable report saved/retrieved; current month rejected |

Success ends with **ALL CHECKS PASSED**; failure exits non-zero. Each fresh run uses a new account prefix, so it can be repeated on a populated demo database. It **writes synthetic accounts and permanent journal/report records**, never deletes them, and checks the entire ledger's integrity.

The script prints a follow-up command containing its prefix. Stop and restart the server (or redeploy Railway), then run that exact command, for example:

```sh
python demo.py --url http://127.0.0.1:8000 --prefix YOUR_PRINTED_PREFIX --check-only
```

Use the **actual printed prefix**, not the placeholder. This verifies saved balances/history and replays the original transfer key after reversal: no money should move again. On a newly created ledger, the previous month's report has no accounts because the demo accounts did not exist at that cutoff; populated historical reconciliation is covered by the tests.

## Try requests yourself

This PowerShell example uses unique account names and works locally or against Railway. For Railway, change `$base` to `https://phillip-ledger-production.up.railway.app` and run `$env:LEDGER_API_TOKEN = Read-Host 'Paste the review token'` in that same terminal first. **Only `demo.py` loads `.env` automatically; PowerShell/curl/API clients do not.** If the local server is token-protected, supply its token too.

```powershell
$base = 'http://127.0.0.1:8000'
$tag = [guid]::NewGuid().ToString('N').Substring(0, 12)
$cash = "manual-$tag-cash"
$reserve = "manual-$tag-reserve"
$headers = @{}
if ($env:LEDGER_API_TOKEN) { $headers.Authorization = "Bearer $env:LEDGER_API_TOKEN" }

$account = @{ id = $cash; currency = 'USD'; opening_balance = '100.00' } | ConvertTo-Json
Invoke-RestMethod "$base/accounts" -Method Post -ContentType 'application/json' -Headers $headers -Body $account
$account = @{ id = $reserve; currency = 'USD' } | ConvertTo-Json
Invoke-RestMethod "$base/accounts" -Method Post -ContentType 'application/json' -Headers $headers -Body $account

$headers['Idempotency-Key'] = "manual-$tag-payment"
$body = @{ source_account = $cash; destination_account = $reserve; amount = '30.00' } | ConvertTo-Json
$posted = Invoke-RestMethod "$base/transactions" -Method Post -ContentType 'application/json' -Headers $headers -Body $body
$posted | ConvertTo-Json -Depth 5
Invoke-RestMethod "$base/accounts/$cash" -Headers $headers    # balance: 70.00
Invoke-RestMethod "$base/accounts/$reserve" -Headers $headers # balance: 30.00
Invoke-RestMethod "$base/accounts/$cash/transactions" -Headers $headers | ConvertTo-Json -Depth 5

# Retry: same transaction ID, still 70.00 / 30.00.
Invoke-RestMethod "$base/transactions" -Method Post -ContentType 'application/json' -Headers $headers -Body $body

# Reverse: new linked transaction restores 100.00 / 0.00; original stays in history.
$headers['Idempotency-Key'] = "manual-$tag-reversal"
Invoke-RestMethod "$base/transactions/$($posted.id)/reversal" -Method Post -ContentType 'application/json' -Headers $headers -Body '{}'
Invoke-RestMethod "$base/integrity" -Headers $headers | ConvertTo-Json -Depth 6
```

## Deploy the live demo on Railway

The root `Dockerfile` is [automatically detected by Railway](https://docs.railway.com/builds/dockerfiles). It runs Python 3.12, binds to 0.0.0.0, reads Railway's PORT and stores SQLite at /data/ledger.db. No PostgreSQL service is needed.

1. Push the source to your GitHub repository. Do **not** commit databases or secrets; the ignore files exclude them.
2. Create a Railway project/service from that GitHub repository. The Dockerfile supplies the build and start command; leave custom build/start commands unset.
3. Attach a **persistent volume**, mounted at **/data**, to this service. From the project canvas, create a volume and select the ledger service, then set its mount path to `/data` and apply the change; see Railway's [volume setup instructions](https://docs.railway.com/volumes). Keep **one instance**. The database, WAL and shared-memory files must stay on that volume. Startup refuses a Railway configuration with no mounted volume or a database path outside it. Railway's [volume reference](https://docs.railway.com/volumes/reference) explains storage and single-instance/redeploy limitations.
4. Generate a review token on your computer:

   ```sh
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   Add it to the service's Variables as **LEDGER_API_TOKEN**, paste the value without quotes and apply/redeploy. Keep it secret; it grants access to the entire synthetic ledger. The image already sets **LEDGER_DB_PATH=/data/ledger.db**. Local `.env` is not uploaded to Railway; configure the server variable there separately. Leave the start command unset.
5. In deployment settings, set the **healthcheck path to /health** and use an **On Failure** restart policy. The health route is public and returns no account data. Railway [uses the injected PORT for healthchecks](https://docs.railway.com/deployments/healthchecks).
6. Deploy/apply the changes, then generate a public domain in the service's networking settings. Keep the detected **target port**, or use the number in the log line `Ledger listening on http://0.0.0.0:PORT_NUMBER`. It must match the runtime `PORT`; do not assume the local default 8000. If you deliberately want 8000, set `PORT=8000`, redeploy and choose target port 8000. See Railway's [target port documentation](https://docs.railway.com/networking/domains/working-with-domains#target-ports). Open https://YOUR-DOMAIN/health; expect `{"status":"ok"}`. Use HTTPS for all external calls.
7. Test from your computer using the same token. Prefer the `.env` setup in [live testing](#test-the-existing-live-railway-deployment), substituting your new domain. Alternatively, set the process environment directly:

   PowerShell:

   ```powershell
   $env:LEDGER_API_TOKEN = Read-Host 'Paste the Railway review token'
   python demo.py --url https://YOUR-DOMAIN
   ```

   macOS/Linux (Bash):

   ```sh
   read -r -s -p 'Railway review token: ' LEDGER_API_TOKEN; echo
   export LEDGER_API_TOKEN
   python3 demo.py --url https://YOUR-DOMAIN
   ```

8. Redeploy/restart once and run the script's printed **--check-only** command against the same HTTPS URL. This deployment-specific persistence check must pass before sharing the demo.

Share the GitHub link, Railway URL and token **privately** with the reviewer. The URL alone shows service/health information, not ledger data. Rotate the token by changing the service variable and redeploying when access should end. Use the script, PowerShell, curl or an API client; there is no CORS-enabled browser frontend.

This is a **low-traffic, synthetic-money review deployment**, not a production financial service. A shared token is a demo gate, not per-user authorization. Volume redeploys can have brief downtime; Railway's deployment healthcheck is not continuous monitoring. Do not upload customer data or expose an unprotected localhost server through a tunnel.

## API contract

Bodies use **Content-Type: application/json**. Send money as decimal **strings**, e.g. "30.00"; responses also use strings. Numeric JSON is accepted without binary-float parsing, but strings are simplest for clients.

| Method | Path | Body / purpose |
| --- | --- | --- |
| GET | / | Public service information; no UI |
| GET | /health | Public database readiness, not a financial audit |
| POST | /accounts | {"id":"cash","currency":"USD","opening_balance":"100.00"}; setup/funding fixture |
| GET | /accounts/{id} | Account ID, currency and balance |
| POST | /transactions | {"source_account":"cash","destination_account":"reserve","amount":"30.00"} |
| GET | /accounts/{id}/transactions?limit=100&after={transaction_id} | Oldest-first history, signed amounts, next_cursor; limit 1-500 |
| POST | /transactions/{id}/reversal | {}; return original exact amounts in a new transaction |
| GET | /integrity | Whole-ledger balance/journal checks, per-currency totals and discrepancies |
| POST | /reconciliations/{YYYY-MM} | Save a completed UTC month's report; no body required |
| GET | /reconciliations/{id} | Retrieve a saved, immutable report |

Transfers and reversals require **Idempotency-Key**: 1-255 printable ASCII characters without spaces. Alternatively supply idempotency_key in the body; both values must agree if both are present. Keys are global across transfers/reversals and persist with the transaction. Same key + same normalized request returns the original response; different request gets 409. Account creation and reconciliation do not use this mechanism: duplicate account IDs get 409, and each reconciliation run saves a new report.

When **LEDGER_API_TOKEN** is set, every ledger route requires **Authorization: Bearer YOUR_TOKEN**. Only / and /health are public. Localhost startup may omit the token; non-loopback startup requires a 32-255 character token. Setting a token also protects localhost routes.

Success: GET 200, POST 201 (including idempotent replays). Errors return `{"error":"..."}`: 400 invalid input, 401 missing/invalid token, 404 missing resource, 409 conflict/insufficient funds, 503 temporary unavailability (Retry-After: 1), 500 unexpected failure. For transfer/reversal timeouts or 503, retry with the **same key and payload**; never invent a new key for an uncertain posting.

For FX, amount is the **source-currency amount** and exchange_rate means **destination units per source unit**:

```json
{"source_account":"cash","destination_account":"euro","amount":"10.00","exchange_rate":"0.91"}
```

## Design and tradeoffs

**Stack:** Python standard library (http.server, sqlite3, decimal, unittest), SQLite and a small Docker image. No third-party runtime dependencies. The HTTP layer validates requests; Ledger owns financial rules and database transactions. Keeping those boundaries in one small module makes the correctness path easy to inspect without framework/ORM machinery.

**Money:** one currency per account; integer cents in storage; decimal arithmetic for FX; round-half-even to the destination cent. Supported two-decimal currencies: USD EUR GBP SGD AUD CAD CHF NZD HKD CNY. Other minor-unit conventions are out of scope. Opening balances are setup funding, not external bank transfers. Account IDs use 1-128 ASCII letters/digits/underscores/hyphens. Amounts must be positive, have at most two fractional decimal places and fit the bounded integer range (maximum 90,000,000,000,000,000.00 per amount/account). Rates allow 12 decimal places, from 0.000000000001 to 1000000; conversion below one destination cent or beyond balance bounds is rejected. Same-currency rates must be 1; cross-currency rates come from the caller, not a price feed.

**Atomicity/concurrency:** BEGIN IMMEDIATE obtains the SQLite writer lock before checking balances or retry keys. Transaction metadata, both balances and both signed entries commit together or roll back together. WAL allows concurrent readers, synchronous=FULL requests durable commits under SQLite's storage guarantees, and unique constraints enforce keys/one reversal per original. STRICT tables, checks, foreign keys and triggers protect valid amounts, relationships and append-only records. Writers are serialized: a correctness tradeoff for the assignment, not horizontal scaling. Five seconds of lock contention leads to retryable 503.

**Journal/corrections:** cached balances give cheap reads; immutable transactions and signed entries explain every movement. A reversal appends a linked correction, never edits history. FX reversals restore exact original cents rather than reconverting at a new rate. Reversals fail if the recipient spent the funds; a reversal cannot itself be reversed. This is a two-account internal transfer ledger, not a full accounting general ledger with clearing, fees and settlement accounts.

**Integrity/month-end:** consistent-snapshot audits reconstruct expected balances from transactions and independently check every signed entry. They detect offsetting per-account corruption even if aggregate totals match. Totals remain separate by currency: same-currency transfers conserve money; FX has explicit per-currency adjustments, never a sum of USD and EUR. Month-end reports reconstruct balances at the start of the following UTC month and exclude accounts opened after that cutoff. This reconciles internal history, not external bank statements. Failed reports are retained; current cached-balance corruption cannot be dated retrospectively. Reconciliation is on-demand, not scheduled. Audits scan the ledger in memory; stream/aggregate when volume requires it.

**Operational scope:** the threaded standard-library HTTP server is for controlled review traffic, not hostile public workloads. Before real funds: add per-user/service authorization, bounded concurrency/rate limits behind a production server/gateway, backup-and-restore drills, monitoring, schema migrations and reconciliation scheduling. Move to PostgreSQL when write concurrency/multiple instances require it. Database immutability protects normal writes, not an administrator who can edit the file/remove triggers. Schema version 1 refuses unknown databases rather than guessing a destructive migration.

## Configuration and troubleshooting

| Setting | Default | Notes |
| --- | --- | --- |
| --db / LEDGER_DB_PATH | ledger.db | CLI overrides environment; parent directory must exist |
| --host / HOST | 127.0.0.1 | Docker explicitly uses 0.0.0.0; public binding needs a token |
| --port / PORT | 8000 | CLI overrides environment; Railway supplies PORT |
| LEDGER_API_TOKEN | unset | Optional for loopback, required for public binding; no secret CLI argument |

`demo.py` also reads the ignored `.env` beside the script when that environment variable is absent. `app.py`, Docker and manual API clients do not automatically read that file. Setting `.env` never modifies Railway's configuration.

- **401:** the API is reachable but did not accept the sent token. Verify that `.env` has the active deployment's token, clear a stale process variable (it takes precedence), and ensure the Railway variable changes were applied/redeployed in the correct service/environment. Browsers do not automatically send bearer tokens. Never paste the token into an issue or chat.
- **Remote token missing:** create `.env` next to `demo.py`, or set `LEDGER_API_TOKEN` in the terminal that launches Python. A `.env.txt` file is not `.env`; on Windows enable file extensions before naming it.
- **Railway volume guard:** attach /data, apply/redeploy, and keep LEDGER_DB_PATH inside the mount. Do not work around the guard with ephemeral storage.
- **Unable to open database:** check the parent directory/volume exists and is writable.
- **Address already in use:** use another port and the matching demo --url.
- **Unsupported schema:** preserve the old database; use a new path. No automatic migration.
- **Health passes, audit fails:** readiness checks database access, not money correctness. Inspect /integrity discrepancies.

If Docker is installed, generate/set LEDGER_API_TOKEN in your terminal first, then:

```sh
docker build -t internal-ledger .
docker run --rm -p 127.0.0.1:8001:8000 -e PORT=8000 -e LEDGER_API_TOKEN -v ledger-demo-data:/data internal-ledger
```

In a second terminal with the same token, run `python demo.py --url http://127.0.0.1:8001`. The named volume keeps data when the container is removed.

## Verification and submission

```sh
python -B -m unittest -v
```

The **34 tests** use temporary databases and real loopback HTTP servers; they do not alter your running ledger. Coverage: threaded retry/overdraft races; four independent writer processes posting 401 unique transfers while audits run; abrupt exits before/after commit; lock contention; 500 seeded operations checked against an independent exact-fraction model; malformed HTTP/JSON; money/rate bounds; FX rounding/reversals; pagination; corruption detection; UTC month boundaries; immutable reports; unknown-schema refusal; bearer authentication; deployment guards; `.env` loading and environment precedence; missing-token rejection before networking; the executable demo using a temporary token file against a real CLI server, followed by a process restart and persisted retry check. Tests do not read or use your real `.env` token. Run from the repository directory and permit child processes/loopback sockets. The contention test intentionally waits five seconds.

These are correctness tests, not a throughput benchmark, penetration test, physical-power-loss simulation or proof of a live Railway deployment. Before submitting:

1. Run the suite; expect **Ran 34 tests / OK**.
2. Run the local demo and its restart check; expect **ALL CHECKS PASSED**.
3. Deploy with volume/token; run the live demo and redeploy check.
4. Share the **GitHub repository link** (the required submission), HTTPS URL and privately supplied review token. Never publish an untested URL or token in the repository.

Files: app.py (API/domain/storage), test_app.py (verification), demo.py (live HTTP walkthrough), Dockerfile (runtime), README.md (reviewer instructions) and [CONTEXT.md](CONTEXT.md) (maintainer/agent orientation). Generated databases, real-money fixtures and credentials do not belong in the submission.

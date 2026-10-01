import json
import os
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import closing
from fractions import Fraction
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from app import Conflict, Handler, Ledger, LedgerError, ThreadingHTTPServer, main
import demo


def process_batch(arguments):
    database, worker, count = arguments
    ledger = Ledger(database)
    body = {"source_account": "a", "destination_account": "b", "amount": "0.01"}
    ledger.transfer(body, "process-shared")
    for index in range(count):
        ledger.transfer(body, f"process-{worker}-{index}")
    return count


def crash_post(database, phase):
    ledger = Ledger(database)
    if phase == "before":
        # The response is built after all SQL writes but before _write commits.
        ledger._transaction = lambda *_: os._exit(23)
    ledger.transfer({"source_account": "a", "destination_account": "b", "amount": "10"}, "crash-key")
    os._exit(24)  # Commit succeeded, but no caller receives a response.


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ledger.db"
        self.clock = patch("app.now", return_value="2026-01-15T12:00:00.000000+00:00")
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.ledger = Ledger(self.path)
        for identifier, code, balance in (("a", "USD", "100.00"), ("b", "USD", "0"), ("e", "EUR", "0")):
            self.ledger.create_account({"id": identifier, "currency": code, "opening_balance": balance})

    def transfer(self, amount="10.00", key="transfer", **extra):
        body = {"source_account": "a", "destination_account": "b", "amount": amount}
        body.update(extra)
        return self.ledger.transfer(body, key)

    def start_http(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.ledger = self.ledger
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.shutdown)
        return server

    def test_requires_retry_key_and_rejects_different_request(self):
        with self.assertRaises(LedgerError):
            self.transfer(key=None)
        original = self.transfer()
        self.transfer("1", "another")
        self.assertEqual(original, self.transfer())
        with self.assertRaises(Conflict):
            self.transfer("11")
        self.assertEqual("89.00", self.ledger.get_account("a")["balance"])

    def test_rate_ten_is_not_one_and_replay_is_canonical(self):
        posted = self.transfer(destination_account="e", exchange_rate="10")
        self.assertEqual("10", posted["exchange_rate"])
        self.assertEqual("100.00", posted["destination"]["amount"])
        self.assertEqual(posted, self.transfer(destination_account="e", exchange_rate="10.0"))
        with self.assertRaises(Conflict):
            self.transfer(destination_account="e", exchange_rate="1")

    def test_invalid_amounts_and_rates_do_not_move_money(self):
        for value in ("NaN", "sNaN", "Infinity", "-1", "0", "0.001", "1e-99999999", "1e999999", "1.000000000000000000000000000001", True, {}, "9" * 200):
            with self.subTest(value=value), self.assertRaises(LedgerError):
                self.transfer(value)
        for value in ("NaN", "Infinity", "0", "-1", "1e999999", "0.0000000000001", {}):
            with self.subTest(rate=value), self.assertRaises(LedgerError):
                self.transfer(destination_account="e", exchange_rate=value)
        self.assertEqual("100.00", self.ledger.get_account("a")["balance"])
        self.assertEqual([], self.ledger.account_transactions("a")["transactions"])

    def test_fx_conversion_overflow_is_atomic(self):
        self.ledger.create_account({"id": "large", "currency": "USD", "opening_balance": "90000000000000000"})
        body = {"source_account": "large", "destination_account": "e", "amount": "90000000000000000", "exchange_rate": "1.000000000001"}
        with self.assertRaises(LedgerError):
            self.ledger.transfer(body, "too-large")
        self.assertEqual("90000000000000000.00", self.ledger.get_account("large")["balance"])
        self.assertEqual("0.00", self.ledger.get_account("e")["balance"])

    def test_currency_totals_include_fx_and_conserve_same_currency_money(self):
        self.transfer()
        posted = self.transfer(destination_account="e", exchange_rate="0.91", key="fx")
        report = self.ledger.verify()
        self.assertEqual("90.00", report["totals"]["USD"]["expected"])
        self.assertEqual("-10.00", report["totals"]["USD"]["fx_adjustment"])
        self.assertEqual("9.10", report["totals"]["EUR"]["expected"])
        self.ledger.reverse(posted["id"], {}, "fx-reverse")
        self.assertEqual("100.00", self.ledger.verify()["totals"]["USD"]["expected"])

    def test_large_account_totals_do_not_overflow(self):
        for index in range(2):
            self.ledger.create_account({"id": f"large-{index}", "currency": "USD", "opening_balance": "90000000000000000"})
        report = self.ledger.verify()
        self.assertTrue(report["ok"])
        self.assertEqual("180000000000000100.00", report["totals"]["USD"]["actual"])

    def test_insufficient_funds_and_overflow_are_atomic(self):
        with self.assertRaises(Conflict):
            self.transfer("100.01")
        self.ledger.create_account({"id": "full", "currency": "USD", "opening_balance": "90000000000000000"})
        with self.assertRaises(LedgerError):
            self.transfer(destination_account="full")
        self.assertEqual("100.00", self.ledger.get_account("a")["balance"])
        self.assertEqual([], self.ledger.account_transactions("a")["transactions"])

    def test_fx_rounding_and_exact_once_reversal(self):
        posted = self.transfer("0.03", destination_account="e", exchange_rate="0.5")
        self.assertEqual("0.02", posted["destination"]["amount"])
        reversed_post = self.ledger.reverse(posted["id"], {}, "reverse")
        self.assertEqual(reversed_post, self.ledger.reverse(posted["id"], {}, "reverse"))
        with self.assertRaises(Conflict):
            self.ledger.reverse(posted["id"], {}, "reverse-again")
        with self.assertRaises(LedgerError):
            self.ledger.reverse(reversed_post["id"], {}, "reverse-reversal")
        self.assertEqual("100.00", self.ledger.get_account("a")["balance"])
        self.assertEqual("0.00", self.ledger.get_account("e")["balance"])
        self.assertTrue(self.ledger.verify()["ok"])

    def test_reversal_fails_if_money_was_spent(self):
        posted = self.transfer()
        self.ledger.transfer({"source_account": "b", "destination_account": "e", "amount": "10", "exchange_rate": "1"}, "spend")
        with self.assertRaises(Conflict):
            self.ledger.reverse(posted["id"], {}, "reverse")
        self.assertEqual("90.00", self.ledger.get_account("a")["balance"])
        self.assertTrue(self.ledger.verify()["ok"])

    def test_concurrent_retries_and_overdraft_race(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            posts = list(pool.map(lambda _: self.transfer(), range(12)))
        self.assertEqual(1, len({post["id"] for post in posts}))

        def attempt(index):
            try:
                self.transfer("10", f"race-{index}")
                return True
            except Conflict:
                return False

        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertEqual(9, sum(pool.map(attempt, range(20))))
        self.assertEqual("0.00", self.ledger.get_account("a")["balance"])
        self.assertEqual("100.00", self.ledger.get_account("b")["balance"])
        self.assertTrue(Ledger(self.path).verify()["ok"])

    def test_failure_after_balance_updates_rolls_back_and_can_be_retried(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""CREATE TRIGGER fail_entry BEFORE INSERT ON entries
                                  WHEN NEW.account_id = 'b'
                                  BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.transfer()
        self.assertEqual("100.00", self.ledger.get_account("a")["balance"])
        self.assertEqual("0.00", self.ledger.get_account("b")["balance"])
        self.assertEqual([], self.ledger.account_transactions("a")["transactions"])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
            connection.execute("DROP TRIGGER fail_entry")
        self.transfer()  # A rolled-back request did not consume the key.
        self.assertTrue(self.ledger.verify()["ok"])

    def test_history_pagination_with_equal_timestamps(self):
        ids = [self.transfer("1", f"page-{index}")["id"] for index in range(5)]
        first = self.ledger.account_transactions("a", limit=2)
        second = self.ledger.account_transactions("a", limit=2, after=first["next_cursor"])
        third = self.ledger.account_transactions("a", limit=2, after=second["next_cursor"])
        self.assertEqual(ids, [row["id"] for page in (first, second, third) for row in page["transactions"]])
        self.assertIsNone(third["next_cursor"])
        with self.assertRaises(LedgerError):
            self.ledger.account_transactions("a", limit=501)

    def test_reconcile_past_month_after_new_postings(self):
        self.transfer()
        with patch("app.now", return_value="2026-02-15T12:00:00.000000+00:00"):
            self.transfer("5", "february")
            january = self.ledger.reconcile("2026-01")
        balances = {row["account_id"]: row for row in january["accounts"]}
        self.assertTrue(january["ok"])
        self.assertEqual("90.00", balances["a"]["expected_balance"])
        self.assertEqual("90.00", balances["a"]["actual_balance"])
        self.assertEqual("85.00", self.ledger.get_account("a")["balance"])
        self.assertEqual(january, Ledger(self.path).get_reconciliation(january["id"]))
        with self.assertRaises(LedgerError):
            self.ledger.reconcile("2026-1")

    def test_reconciliation_excludes_new_accounts_and_rejects_open_period(self):
        with self.assertRaises(Conflict):
            self.ledger.reconcile("2026-01")
        with patch("app.now", return_value="2026-02-01T00:00:00.000000+00:00"):
            self.ledger.create_account({"id": "new", "currency": "USD", "opening_balance": "50"})
            first = self.ledger.reconcile("2026-01")
            second = self.ledger.reconcile("2026-01")
        self.assertEqual("100.00", first["totals"]["USD"]["expected"])
        self.assertNotIn("new", {row["account_id"] for row in first["accounts"]})
        self.assertNotEqual(first["id"], second["id"])

    def test_missing_journal_entry_is_detected_independently_of_balances(self):
        self.transfer()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TRIGGER entries_no_delete")
            connection.execute("DELETE FROM entries WHERE account_id = 'b'")
        report = self.ledger.verify()
        self.assertFalse(report["ok"])
        self.assertEqual([], report["discrepancies"])
        self.assertEqual(1, len(report["journal_discrepancies"]))

    def test_input_contract_and_currency_scope(self):
        for body in (
            {"id": "bad/id", "currency": "USD"},
            {"id": "jpy", "currency": "JPY"},
            {"id": "bad", "currency": "USD", "opening_balnce": "10"},
        ):
            with self.subTest(body=body), self.assertRaises(LedgerError):
                self.ledger.create_account(body)
        with self.assertRaises(LedgerError):
            self.transfer(destination_account="a")
        with self.assertRaises(LedgerError):
            self.transfer(exchange_rate="2")
        with self.assertRaises(LedgerError):
            self.transfer(idempotency_key="different")

    def test_integrity_detects_offsetting_balance_corruption(self):
        self.transfer()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE accounts SET balance_cents = balance_cents + 1 WHERE id = 'a'")
            connection.execute("UPDATE accounts SET balance_cents = balance_cents - 1 WHERE id = 'b'")
        report = self.ledger.verify()
        self.assertFalse(report["ok"])
        self.assertEqual(2, len(report["discrepancies"]))
        self.assertEqual(report["totals"]["USD"]["actual"], report["totals"]["USD"]["expected"])
        with patch("app.now", return_value="2026-02-01T00:00:00.000000+00:00"):
            self.assertFalse(self.ledger.reconcile("2026-01")["ok"])

    def test_journal_is_immutable_and_database_constraints_hold(self):
        self.transfer()
        with closing(sqlite3.connect(self.path)) as connection:
            for statement in (
                "UPDATE entries SET amount_cents = 1",
                "DELETE FROM entries",
                "UPDATE transactions SET source_amount_cents = 1",
                "DELETE FROM transactions",
                "UPDATE accounts SET opening_balance_cents = 0",
                "UPDATE accounts SET balance_cents = -1",
            ):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement)

    def test_http_contract(self):
        server = self.start_http()

        def request(method, path, body=None, headers=None, raw=None):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                data = raw if raw is not None else (None if body is None else json.dumps(body))
                connection.request(method, path, data, {"Content-Type": "application/json", **(headers or {})})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        body = {"source_account": "a", "destination_account": "b", "amount": "10.00"}
        self.assertEqual(400, request("POST", "/transactions", body)[0])
        status, posted = request("POST", "/transactions", body, {"Idempotency-Key": "http"})
        self.assertEqual(201, status)
        self.assertEqual(posted, request("POST", "/transactions", body, {"Idempotency-Key": "http"})[1])
        self.assertEqual("90.00", request("GET", "/accounts/a")[1]["balance"])
        self.assertEqual(1, len(request("GET", "/accounts/a/transactions")[1]["transactions"]))
        self.assertEqual(404, request("GET", "/accounts/missing")[0])
        self.assertEqual(400, request("POST", "/accounts", [1])[0])
        self.assertEqual(400, request("POST", "/accounts", raw='{"id":"x","id":"y","currency":"USD"}')[0])
        self.assertEqual(400, request("POST", "/transactions", raw='{"amount":NaN}')[0])
        self.assertEqual(400, request("POST", "/transactions", raw='{"amount":1}', headers={"Content-Length": "bad"})[0])
        self.assertEqual(400, request("GET", "/accounts/a/transactions?limit=0")[0])
        numeric = '{"source_account":"a","destination_account":"b","amount":0.01}'
        self.assertEqual(201, request("POST", "/transactions", raw=numeric, headers={"Idempotency-Key": "numeric"})[0])
        status, reversal = request("POST", f'/transactions/{posted["id"]}/reversal', {}, {"Idempotency-Key": "http-reverse"})
        self.assertEqual(201, status)
        self.assertEqual(posted["id"], reversal["reversal_of"])
        self.assertTrue(request("GET", "/integrity")[1]["ok"])

    def test_multiple_processes_and_audits_share_a_consistent_database(self):
        with ProcessPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(process_batch, (str(self.path), worker, 100)) for worker in range(4)]
            deadline = time.monotonic() + 30
            audit_count = 0
            while not all(future.done() for future in futures):
                report = self.ledger.verify()
                self.assertTrue(report["ok"], report)
                audit_count += 1
                self.assertLess(time.monotonic(), deadline, "process stress test exceeded 30 seconds")
                time.sleep(0.01)
            self.assertEqual(400, sum(future.result() for future in futures))
        self.assertGreater(audit_count, 0)
        self.assertEqual("95.99", self.ledger.get_account("a")["balance"])
        self.assertEqual("4.01", self.ledger.get_account("b")["balance"])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(401, connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
            self.assertEqual(802, connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])

    def run_crash(self, phase):
        result = subprocess.run(
            [sys.executable, "-B", "-c", "import sys; from test_app import crash_post; crash_post(sys.argv[1], sys.argv[2])", str(self.path), phase],
            cwd=Path(__file__).parent,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(23 if phase == "before" else 24, result.returncode, result.stderr.decode())

    def test_process_crash_before_commit_rolls_back_everything(self):
        self.run_crash("before")
        reopened = Ledger(self.path)
        self.assertEqual("100.00", reopened.get_account("a")["balance"])
        self.assertEqual("0.00", reopened.get_account("b")["balance"])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
        self.transfer(key="crash-key")
        self.assertTrue(reopened.verify()["ok"])

    def test_process_crash_after_commit_preserves_posting_and_retry_key(self):
        self.run_crash("after")
        reopened = Ledger(self.path)
        posted = reopened.transfer({"source_account": "a", "destination_account": "b", "amount": "10.00"}, "crash-key")
        self.assertEqual("90.00", reopened.get_account("a")["balance"])
        self.assertEqual("10.00", reopened.get_account("b")["balance"])
        self.assertEqual(posted["id"], reopened.account_transactions("a")["transactions"][0]["id"])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
        self.assertTrue(reopened.verify()["ok"])

    def test_randomized_transfers_fx_retries_and_reversals_against_fraction_model(self):
        rng = random.Random(20261001)
        balances = {"a": 10000, "b": 0, "e": 0, "c": 10000, "d": 10000, "f": 10000}
        currencies = {"a": "USD", "b": "USD", "e": "EUR", "c": "USD", "d": "EUR", "f": "SGD"}
        for identifier in ("c", "d", "f"):
            self.ledger.create_account({"id": identifier, "currency": currencies[identifier], "opening_balance": "100"})
        originals = []
        posted_count = 0
        for index in range(500):
            if originals and rng.randrange(4) == 0:
                position = rng.randrange(len(originals))
                transaction_id, source, destination, cents, converted = originals[position]
                if balances[destination] < converted:
                    with self.assertRaises(Conflict):
                        self.ledger.reverse(transaction_id, {}, f"model-reverse-{index}")
                else:
                    self.ledger.reverse(transaction_id, {}, f"model-reverse-{index}")
                    balances[source] += cents
                    balances[destination] -= converted
                    originals.pop(position)
                    posted_count += 1
            else:
                source, destination = rng.sample(list(balances), 2)
                cents = rng.randint(1, 3000)
                exchange_rate = "1" if currencies[source] == currencies[destination] else rng.choice(("0.5", "0.91", "1.1", "10"))
                # Independent exact rational arithmetic supplies the expected rounded cents.
                converted = round(cents * Fraction(exchange_rate))
                body = {"source_account": source, "destination_account": destination, "amount": f"{cents // 100}.{cents % 100:02d}", "exchange_rate": exchange_rate}
                key = f"model-transfer-{index}"
                if converted == 0 or balances[source] < cents:
                    with self.assertRaises(LedgerError):
                        self.ledger.transfer(body, key)
                else:
                    result = self.ledger.transfer(body, key)
                    self.assertEqual(converted, int(Fraction(result["destination"]["amount"]) * 100))
                    balances[source] -= cents
                    balances[destination] += converted
                    originals.append((result["id"], source, destination, cents, converted))
                    posted_count += 1
                    if rng.randrange(4) == 0:
                        self.assertEqual(result, self.ledger.transfer(body, key))
            if index % 25 == 0:
                self.assertTrue(self.ledger.verify()["ok"])
                for identifier, cents in balances.items():
                    self.assertEqual(cents, int(Fraction(self.ledger.get_account(identifier)["balance"]) * 100))
        report = self.ledger.verify()
        self.assertTrue(report["ok"], report)
        for identifier, cents in balances.items():
            self.assertEqual(cents, int(Fraction(self.ledger.get_account(identifier)["balance"]) * 100))
        for code in set(currencies.values()):
            self.assertEqual(sum(value for identifier, value in balances.items() if currencies[identifier] == code), int(Fraction(report["totals"][code]["actual"]) * 100))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(posted_count, connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
            self.assertEqual(posted_count * 2, connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])

    def test_http_database_contention_is_retryable_without_partial_writes(self):
        server = self.start_http()
        body = json.dumps({"source_account": "a", "destination_account": "b", "amount": "10"})
        with closing(sqlite3.connect(self.path, isolation_level=None)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=8)
            try:
                connection.request("POST", "/transactions", body, {"Content-Type": "application/json", "Idempotency-Key": "busy"})
                response = connection.getresponse()
                self.assertEqual(503, response.status, response.read())
                self.assertEqual("1", response.getheader("Retry-After"))
            finally:
                connection.close()
                lock.rollback()
        self.assertEqual("100.00", self.ledger.get_account("a")["balance"])
        self.assertEqual([], self.ledger.account_transactions("a")["transactions"])
        self.transfer(key="busy")
        self.assertTrue(self.ledger.verify()["ok"])

    def test_http_rejects_malformed_envelopes_without_posting(self):
        server = self.start_http()
        cases = (
            (b"{}", [("Content-Length", "2"), ("Content-Length", "2")], "one valid Content-Length"),
            (b"{}", [("Content-Length", "2"), ("Idempotency-Key", "one"), ("Idempotency-Key", "two")], "only one Idempotency-Key"),
            (b"", [("Content-Length", "1000001")], "request body must be non-empty JSON under 1 MB"),
            (b"{}", [("Content-Length", "2"), ("Transfer-Encoding", "chunked")], "one valid Content-Length"),
            (b"{}", [("Content-Length", "2"), ("Content-Type", "text/plain")], "Content-Type must be application/json"),
            (b'{"amount":Infinity}', [("Content-Length", "19")], "invalid JSON number"),
            (b"[" * 5000 + b"0" + b"]" * 5000, [("Content-Length", "10001")], "request JSON is too complex"),
            (b"{}", [("Content-Length", "20")], "incomplete request body"),
        )
        for raw, headers, expected_error in cases:
            with self.subTest(headers=headers):
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    connection.putrequest("POST", "/transactions")
                    if not any(name == "Content-Type" for name, _ in headers):
                        connection.putheader("Content-Type", "application/json")
                    for name, value in headers:
                        connection.putheader(name, value)
                    connection.endheaders()
                    if any(name == "Transfer-Encoding" for name, _ in headers):
                        # A rejected envelope can receive its body after the response has been sent.
                        time.sleep(0.05)
                    if raw:
                        connection.send(raw)
                    if expected_error == "incomplete request body":
                        connection.sock.shutdown(socket.SHUT_WR)
                    response = connection.getresponse()
                    self.assertEqual(400, response.status)
                    error = json.loads(response.read())["error"]
                    self.assertTrue(error.startswith(expected_error), error)
                finally:
                    connection.close()
        self.assertEqual("100.00", self.ledger.get_account("a")["balance"])
        self.assertEqual([], self.ledger.account_transactions("a")["transactions"])
        self.assertTrue(self.ledger.verify()["ok"])

    def test_month_boundary_and_audit_report_immutability(self):
        with patch("app.now", return_value="2026-01-31T23:59:59.999999+00:00"):
            self.transfer()
        with patch("app.now", return_value="2026-02-01T00:00:00.000000+00:00"):
            self.transfer("5", "boundary")
            report = self.ledger.reconcile("2026-01")
        account = next(row for row in report["accounts"] if row["account_id"] == "a")
        self.assertEqual("90.00", account["expected_balance"])
        self.assertEqual("90.00", account["actual_balance"])
        self.assertEqual("85.00", self.ledger.get_account("a")["balance"])
        with closing(sqlite3.connect(self.path)) as connection:
            for statement in ("UPDATE reconciliations SET report = '{}'", "DELETE FROM reconciliations"):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement)

    def test_unknown_database_schema_is_refused_without_modification(self):
        legacy = Path(self.directory.name) / "legacy.db"
        with closing(sqlite3.connect(legacy)) as connection, connection:
            connection.execute("CREATE TABLE old_accounts (balance INTEGER)")
            connection.execute("INSERT INTO old_accounts VALUES (123)")
        with self.assertRaises(RuntimeError):
            Ledger(legacy)
        with closing(sqlite3.connect(legacy)) as connection:
            self.assertEqual(123, connection.execute("SELECT balance FROM old_accounts").fetchone()[0])
            self.assertEqual(0, connection.execute("PRAGMA user_version").fetchone()[0])

    def test_bearer_auth_protects_reads_and_writes_but_not_health(self):
        server = self.start_http()
        server.api_token = "test-" + "x" * 40

        def request(method, path, authorization=()):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.putrequest(method, path)
                for value in authorization:
                    connection.putheader("Authorization", value)
                connection.putheader("Content-Length", "2" if method == "POST" else "0")
                connection.putheader("Content-Type", "application/json")
                connection.endheaders(b"{}" if method == "POST" else None)
                response = connection.getresponse()
                return response.status, json.loads(response.read()), response.getheader("WWW-Authenticate")
            finally:
                connection.close()

        valid = "Bearer " + server.api_token
        for method, path in (("GET", "/accounts/a"), ("GET", "/integrity"), ("GET", "/accounts/a/transactions"), ("GET", "/reconciliations/missing"), ("POST", "/accounts"), ("POST", "/transactions"), ("POST", "/transactions/missing/reversal"), ("POST", "/reconciliations/2025-12")):
            for headers in ((), ("Bearer wrong",), (valid, valid), ("Basic " + server.api_token,)):
                with self.subTest(method=method, path=path, headers=headers):
                    status, body, challenge = request(method, path, headers)
                    self.assertEqual(401, status)
                    self.assertEqual("Bearer", challenge)
        self.assertEqual(200, request("GET", "/accounts/a", (valid,))[0])
        self.assertEqual({"status": "ok"}, request("GET", "/health")[1])
        self.assertEqual(200, request("GET", "/")[0])
        self.assertEqual("100.00", self.ledger.get_account("a")["balance"])
        self.assertEqual([], self.ledger.account_transactions("a")["transactions"])

    def test_health_returns_unavailable_without_exposing_database_error(self):
        server = self.start_http()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            with patch.object(self.ledger, "health", side_effect=sqlite3.OperationalError("private database path")), self.assertLogs("app", level="ERROR"):
                connection.request("GET", "/health")
                response = connection.getresponse()
                self.assertEqual(503, response.status)
                self.assertEqual({"error": "database unavailable"}, json.loads(response.read()))
        finally:
            connection.close()

    def test_cli_environment_and_public_deployment_guards(self):
        token = "test-" + "x" * 40
        valid_environment = {"HOST": "0.0.0.0", "PORT": "9000", "LEDGER_DB_PATH": str(self.path), "LEDGER_API_TOKEN": token, "RAILWAY_ENVIRONMENT_ID": "test", "RAILWAY_VOLUME_MOUNT_PATH": self.directory.name}
        for environment, flags, valid in (
            ({}, [], True),
            (valid_environment, [], True),
            (valid_environment, ["--host", "127.0.0.1", "--port", "9001", "--db", str(self.path)], True),
            ({"HOST": "0.0.0.0"}, [], False),
            ({"LEDGER_API_TOKEN": "short"}, [], False),
            ({"LEDGER_API_TOKEN": ""}, [], False),
            ({**valid_environment, "RAILWAY_VOLUME_MOUNT_PATH": ""}, [], False),
            ({**valid_environment, "LEDGER_DB_PATH": "outside-volume.db"}, [], False),
        ):
            with self.subTest(environment=environment, flags=flags), patch.dict(os.environ, environment, clear=True), patch("sys.argv", ["app.py", *flags]), patch("app.Ledger") as ledger, patch("app.ThreadingHTTPServer") as factory, patch("app.signal.signal"), patch("builtins.print"), patch("sys.stderr"):
                factory.return_value.serve_forever.side_effect = KeyboardInterrupt
                if valid:
                    main()
                    host = "127.0.0.1" if flags or not environment else "0.0.0.0"
                    port = 9001 if flags else (9000 if environment else 8000)
                    factory.assert_called_once_with((host, port), Handler)
                    ledger.assert_called_once_with(str(self.path) if environment else "ledger.db")
                    self.assertEqual(environment.get("LEDGER_API_TOKEN"), factory.return_value.api_token)
                    factory.return_value.server_close.assert_called_once()
                else:
                    with self.assertRaises(SystemExit) as error:
                        main()
                    self.assertEqual(2, error.exception.code)
                    ledger.assert_not_called()
                    factory.assert_not_called()

    def test_demo_and_persisted_check_run_against_authenticated_http(self):
        environment = {**os.environ, "HOST": "0.0.0.0", "PORT": "0", "LEDGER_DB_PATH": str(self.path), "LEDGER_API_TOKEN": "test-" + "x" * 40, "RAILWAY_ENVIRONMENT_ID": "test", "RAILWAY_VOLUME_MOUNT_PATH": self.directory.name}
        demo_script = Path(self.directory.name) / "demo.py"
        shutil.copyfile(Path(__file__).parent / "demo.py", demo_script)
        demo_script.with_name(".env").write_text("LEDGER_API_TOKEN=" + environment["LEDGER_API_TOKEN"] + "\n", encoding="utf-8")
        client_environment = {**environment}
        client_environment.pop("LEDGER_API_TOKEN")  # Exercise file loading, not the user's real .env.
        logfile = Path(self.directory.name) / "server.log"
        for flags in ([], ["--check-only"]):
            with logfile.open("w", encoding="utf-8") as output:
                process = subprocess.Popen([sys.executable, "-B", "-u", "app.py", "serve"], cwd=Path(__file__).parent, env=environment, stdout=output, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 10
                    while True:
                        logs = logfile.read_text(encoding="utf-8")
                        match = re.search(r"Ledger listening on http://0\.0\.0\.0:(\d+)", logs)
                        if match:
                            break
                        self.assertIsNone(process.poll(), logs)
                        self.assertLess(time.monotonic(), deadline, "CLI server did not start: " + logs)
                        time.sleep(0.05)
                    command = [sys.executable, "-B", str(demo_script), "--url", f"http://127.0.0.1:{match[1]}", "--prefix", "integration-demo"]
                    result = subprocess.run(command + flags, cwd=Path(__file__).parent, env=client_environment, capture_output=True, text=True, timeout=30)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertIn("ALL CHECKS PASSED", result.stdout)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        self.assertTrue(self.ledger.verify()["ok"])

    def test_demo_token_file_loading_and_environment_precedence(self):
        script = Path(self.directory.name) / "demo.py"
        path = script.with_name(".env")
        token = "test-" + "x" * 40
        with patch("demo.__file__", str(script)), patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(demo.demo_token())
            path.write_text("# Local reviewer credential\nOTHER_KEY=ignored\n LEDGER_API_TOKEN = '" + token + "'\n", encoding="utf-8-sig")
            self.assertEqual(token, demo.demo_token())
            path.write_text("LEDGER_API_TOKEN=" + token + "\nLEDGER_API_TOKEN=duplicate\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                demo.demo_token()
            with patch.dict(os.environ, {"LEDGER_API_TOKEN": "test-" + "y" * 40}):
                self.assertEqual("test-" + "y" * 40, demo.demo_token())
            for value in ("", "short", "Bearer " + token, "x" * 256, token + "\t", "non-ascii-" + "\u00e9" * 40):
                with self.subTest(value=value), patch.dict(os.environ, {"LEDGER_API_TOKEN": value}), self.assertRaises(ValueError) as error:
                    demo.demo_token()
                if value:
                    self.assertNotIn(value, str(error.exception))
            path.write_text("OTHER_KEY=ignored\n", encoding="utf-8")
            self.assertIsNone(demo.demo_token())

    def test_remote_demo_missing_token_fails_before_network(self):
        with patch("demo.demo_token", return_value=None), patch("sys.argv", ["demo.py", "--url", "https://example.invalid"]), patch("demo.build_opener") as opener, patch("sys.stderr"):
            with self.assertRaises(SystemExit) as error:
                demo.main()
            self.assertEqual(2, error.exception.code)
            opener.assert_not_called()

    def test_demo_rejects_unsafe_remote_url_before_any_request(self):
        for url in ("http://example.com", "https://user:password@example.com", "https://example.com/unexpected", "https://example.com?query=1", "file:///etc/passwd"):
            with self.subTest(url=url):
                result = subprocess.run([sys.executable, "-B", "demo.py", "--url", url], cwd=Path(__file__).parent, capture_output=True, timeout=5)
                self.assertEqual(2, result.returncode)


if __name__ == "__main__":
    unittest.main()

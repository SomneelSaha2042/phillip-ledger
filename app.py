"""Small, durable HTTP ledger service. Run: python app.py serve"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


MAX_CENTS = 9_000_000_000_000_000_000
SUPPORTED_CURRENCIES = frozenset("USD EUR GBP SGD AUD CAD CHF NZD HKD CNY".split())
logger = logging.getLogger(__name__)


class LedgerError(Exception):
    status = 400


class NotFound(LedgerError):
    status = 404


class Conflict(LedgerError):
    status = 409


class Unavailable(LedgerError):
    status = 503


class Unauthorized(LedgerError):
    status = 401


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def decimal_value(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)) or len(str(value)) > 128:
        raise LedgerError(f"{field} must be a decimal string or number up to 128 characters")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise LedgerError(f"{field} must be a decimal amount") from None
    if not result.is_finite():
        raise LedgerError(f"{field} must be finite")
    return result


def money(value: object, field: str, *, allow_zero: bool = False) -> int:
    amount = decimal_value(value, field)
    if amount < 0 or (amount == 0 and not allow_zero):
        raise LedgerError(f"{field} must be {'non-negative' if allow_zero else 'positive'}")
    if amount > Decimal(MAX_CENTS) / 100:
        raise LedgerError(f"{field} is too large")
    if 0 < amount < Decimal("0.01"):
        raise LedgerError(f"{field} must have at most two decimal places")
    with localcontext() as context:
        context.prec = 200  # All accepted input digits fit without rounding away fractional cents.
        cents = amount * 100
        if cents != cents.to_integral_value():
            raise LedgerError(f"{field} must have at most two decimal places")
        return int(cents)


def rate(value: object) -> Decimal:
    result = decimal_value(value, "exchange_rate")
    if not Decimal("0.000000000001") <= result <= Decimal("1000000"):
        raise LedgerError("exchange_rate must be between 0.000000000001 and 1000000")
    with localcontext() as context:
        context.prec = 200
        if result != result.quantize(Decimal("0.000000000001")):
            raise LedgerError("exchange_rate must have at most 12 decimal places")
    return result


def decimal_text(value: Decimal) -> str:
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def amount_text(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def account_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise LedgerError("account id must contain 1-128 ASCII letters, digits, underscores or hyphens")
    return value


def currency(value: object) -> str:
    if not isinstance(value, str) or value.upper() not in SUPPORTED_CURRENCIES:
        raise LedgerError("currency must be one of: " + ", ".join(sorted(SUPPORTED_CURRENCIES)))
    return value.upper()


def idempotency_key(body: dict, header: str | None) -> str:
    supplied = body.get("idempotency_key")
    if header is not None and "idempotency_key" in body and header != supplied:
        raise LedgerError("Idempotency-Key header and idempotency_key body value differ")
    key = header if header is not None else supplied
    if not isinstance(key, str) or not re.fullmatch(r"[\x21-\x7e]{1,255}", key):
        raise LedgerError("Idempotency-Key is required: 1-255 printable ASCII characters without spaces")
    return key


def fields(body: dict, allowed: set[str]) -> None:
    if unknown := body.keys() - allowed:
        raise LedgerError("unknown fields: " + ", ".join(sorted(unknown)))


def json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise LedgerError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def reject_json_constant(value: str):
    raise LedgerError(f"invalid JSON number: {value}")


class Ledger:
    def __init__(self, database: str | Path):
        self.database = str(database)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, isolation_level=None, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                return
            if version != 0 or connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone():
                raise RuntimeError("Unsupported database schema; preserve the file and use a new --db path")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE accounts (
                    id TEXT NOT NULL PRIMARY KEY,
                    currency TEXT NOT NULL,
                    opening_balance_cents INTEGER NOT NULL CHECK(opening_balance_cents BETWEEN 0 AND 9000000000000000000),
                    balance_cents INTEGER NOT NULL CHECK(balance_cents BETWEEN 0 AND 9000000000000000000),
                    created_at TEXT NOT NULL,
                    UNIQUE(id, currency)
                ) STRICT;
                CREATE TABLE transactions (
                    sequence INTEGER PRIMARY KEY,
                    id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_fingerprint TEXT NOT NULL,
                    source_account_id TEXT NOT NULL,
                    destination_account_id TEXT NOT NULL CHECK(source_account_id != destination_account_id),
                    source_amount_cents INTEGER NOT NULL CHECK(source_amount_cents BETWEEN 1 AND 9000000000000000000),
                    destination_amount_cents INTEGER NOT NULL CHECK(destination_amount_cents BETWEEN 1 AND 9000000000000000000),
                    source_currency TEXT NOT NULL,
                    destination_currency TEXT NOT NULL,
                    exchange_rate TEXT NOT NULL,
                    reversal_of TEXT UNIQUE REFERENCES transactions(id),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(source_account_id, source_currency) REFERENCES accounts(id, currency),
                    FOREIGN KEY(destination_account_id, destination_currency) REFERENCES accounts(id, currency),
                    CHECK(source_currency != destination_currency OR
                          (source_amount_cents = destination_amount_cents AND exchange_rate = '1'))
                ) STRICT;
                CREATE TABLE entries (
                    transaction_id TEXT NOT NULL REFERENCES transactions(id),
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    amount_cents INTEGER NOT NULL CHECK(amount_cents != 0),
                    PRIMARY KEY (transaction_id, account_id)
                ) STRICT;
                CREATE TABLE reconciliations (
                    id TEXT NOT NULL PRIMARY KEY,
                    month TEXT NOT NULL,
                    reconciled_at TEXT NOT NULL,
                    report TEXT NOT NULL
                ) STRICT;
                CREATE INDEX entries_by_account ON entries(account_id);
                CREATE INDEX transactions_by_time ON transactions(created_at, sequence);
                CREATE TRIGGER account_metadata_immutable BEFORE UPDATE OF id, currency, opening_balance_cents, created_at ON accounts
                BEGIN SELECT RAISE(ABORT, 'account metadata is immutable'); END;
                CREATE TRIGGER accounts_no_delete BEFORE DELETE ON accounts
                BEGIN SELECT RAISE(ABORT, 'accounts cannot be deleted'); END;
                CREATE TRIGGER entries_validate BEFORE INSERT ON entries
                WHEN NOT EXISTS (
                    SELECT 1 FROM transactions t WHERE t.id = NEW.transaction_id AND
                    ((t.source_account_id = NEW.account_id AND -t.source_amount_cents = NEW.amount_cents) OR
                     (t.destination_account_id = NEW.account_id AND t.destination_amount_cents = NEW.amount_cents))
                )
                BEGIN SELECT RAISE(ABORT, 'entry must match the transaction'); END;
                """
            )
            for table in ("transactions", "entries", "reconciliations"):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table} "
                        "BEGIN SELECT RAISE(ABORT, 'journal and reconciliation records are immutable'); END"
                    )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _write(self, action):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = action(connection)
            connection.commit()
            return result
        except sqlite3.OperationalError as error:
            connection.rollback()
            if (getattr(error, "sqlite_errorcode", 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise Unavailable("database is busy; retry with the same Idempotency-Key") from error
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _fingerprint(value: dict) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def create_account(self, body: dict) -> dict:
        fields(body, {"id", "currency", "opening_balance"})
        identifier = account_id(body.get("id"))
        account_currency = currency(body.get("currency"))
        opening = money(body.get("opening_balance", "0"), "opening_balance", allow_zero=True)

        def action(connection):
            if connection.execute("SELECT 1 FROM accounts WHERE id = ?", (identifier,)).fetchone():
                raise Conflict("account already exists")
            connection.execute(
                "INSERT INTO accounts VALUES (?, ?, ?, ?, ?)",
                (identifier, account_currency, opening, opening, now()),
            )
            return self._account(connection, identifier)

        return self._write(action)

    def _account(self, connection: sqlite3.Connection, identifier: str) -> dict:
        row = connection.execute("SELECT * FROM accounts WHERE id = ?", (identifier,)).fetchone()
        if row is None:
            raise NotFound("account not found")
        return {"id": row["id"], "currency": row["currency"], "balance": amount_text(row["balance_cents"])}

    def get_account(self, identifier: str) -> dict:
        connection = self._connect()
        try:
            return self._account(connection, account_id(identifier))
        finally:
            connection.close()

    def _existing_idempotent(self, connection, key: str, fingerprint: str) -> str | None:
        row = connection.execute(
            "SELECT id, request_fingerprint FROM transactions WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["request_fingerprint"] != fingerprint:
            raise Conflict("idempotency key is already associated with a different request")
        return row["id"]

    def _transaction(self, connection, transaction_id: str) -> dict:
        row = connection.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
        if row is None:
            raise NotFound("transaction not found")
        return {
            "id": row["id"],
            "confirmation": "posted",
            "created_at": row["created_at"],
            "reversal_of": row["reversal_of"],
            "source": {
                "account_id": row["source_account_id"],
                "currency": row["source_currency"],
                "amount": amount_text(row["source_amount_cents"]),
            },
            "destination": {
                "account_id": row["destination_account_id"],
                "currency": row["destination_currency"],
                "amount": amount_text(row["destination_amount_cents"]),
            },
            "exchange_rate": row["exchange_rate"],
        }

    def _post(
        self,
        connection: sqlite3.Connection,
        *,
        source: sqlite3.Row,
        destination: sqlite3.Row,
        source_cents: int,
        destination_cents: int,
        exchange_rate: Decimal,
        key: str,
        fingerprint: str,
        reversal_of: str | None = None,
    ) -> str:
        if source["balance_cents"] < source_cents:
            raise Conflict("insufficient funds")
        if not 0 < destination_cents <= MAX_CENTS or destination["balance_cents"] > MAX_CENTS - destination_cents:
            raise LedgerError("destination balance would exceed the supported integer range")
        transaction_id = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO transactions
               (id, idempotency_key, request_fingerprint, source_account_id, destination_account_id,
                source_amount_cents, destination_amount_cents, source_currency, destination_currency,
                exchange_rate, reversal_of, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                transaction_id,
                key,
                fingerprint,
                source["id"],
                destination["id"],
                source_cents,
                destination_cents,
                source["currency"],
                destination["currency"],
                decimal_text(exchange_rate),
                reversal_of,
                now(),
            ),
        )
        connection.execute(
            "UPDATE accounts SET balance_cents = balance_cents - ? WHERE id = ?",
            (source_cents, source["id"]),
        )
        connection.execute(
            "UPDATE accounts SET balance_cents = balance_cents + ? WHERE id = ?",
            (destination_cents, destination["id"]),
        )
        connection.executemany(
            "INSERT INTO entries VALUES (?, ?, ?)",
            ((transaction_id, source["id"], -source_cents), (transaction_id, destination["id"], destination_cents)),
        )
        return transaction_id

    def transfer(self, body: dict, key: str | None = None) -> dict:
        fields(body, {"source_account", "destination_account", "amount", "exchange_rate", "idempotency_key"})
        source_id = account_id(body.get("source_account"))
        destination_id = account_id(body.get("destination_account"))
        if source_id == destination_id:
            raise LedgerError("source_account and destination_account must differ")
        source_cents = money(body.get("amount"), "amount")
        key = idempotency_key(body, key)

        def action(connection):
            source = connection.execute("SELECT * FROM accounts WHERE id = ?", (source_id,)).fetchone()
            destination = connection.execute("SELECT * FROM accounts WHERE id = ?", (destination_id,)).fetchone()
            if source is None or destination is None:
                raise NotFound("source or destination account not found")
            if source["currency"] == destination["currency"]:
                exchange_rate = rate(body["exchange_rate"]) if "exchange_rate" in body else Decimal(1)
                if exchange_rate != 1:
                    raise LedgerError("exchange_rate must be 1 for accounts in the same currency")
            else:
                exchange_rate = rate(body.get("exchange_rate"))
            with localcontext() as context:
                context.prec = 200
                destination_cents = int(
                    (Decimal(source_cents) * exchange_rate).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
                )
            if destination_cents <= 0:
                raise LedgerError("exchange_rate produces an amount below one cent")
            fingerprint = self._fingerprint(
                {
                    "kind": "transfer",
                    "source": source_id,
                    "destination": destination_id,
                    "source_cents": source_cents,
                    "exchange_rate": decimal_text(exchange_rate),
                }
            )
            if previous := self._existing_idempotent(connection, key, fingerprint):
                return self._transaction(connection, previous)
            posted = self._post(
                connection,
                source=source,
                destination=destination,
                source_cents=source_cents,
                destination_cents=destination_cents,
                exchange_rate=exchange_rate,
                key=key,
                fingerprint=fingerprint,
            )
            return self._transaction(connection, posted)

        return self._write(action)

    def reverse(self, transaction_id: str, body: dict, key: str | None = None) -> dict:
        fields(body, {"idempotency_key"})
        key = idempotency_key(body, key)
        transaction_id = str(transaction_id)
        fingerprint = self._fingerprint({"kind": "reversal", "reversal_of": transaction_id})

        def action(connection):
            if previous := self._existing_idempotent(connection, key, fingerprint):
                return self._transaction(connection, previous)
            original = connection.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
            if original is None:
                raise NotFound("transaction not found")
            if original["reversal_of"] is not None:
                raise LedgerError("a reversal cannot itself be reversed")
            if connection.execute(
                "SELECT id FROM transactions WHERE reversal_of = ?", (transaction_id,)
            ).fetchone():
                raise Conflict("transaction is already reversed; retry using the original reversal key")
            source = connection.execute(
                "SELECT * FROM accounts WHERE id = ?", (original["destination_account_id"],)
            ).fetchone()
            destination = connection.execute(
                "SELECT * FROM accounts WHERE id = ?", (original["source_account_id"],)
            ).fetchone()
            with localcontext() as context:
                context.prec = 40
                reverse_rate = Decimal(original["source_amount_cents"]) / Decimal(original["destination_amount_cents"])
            posted = self._post(
                connection,
                source=source,
                destination=destination,
                source_cents=original["destination_amount_cents"],
                destination_cents=original["source_amount_cents"],
                exchange_rate=reverse_rate,
                key=key,
                fingerprint=fingerprint,
                reversal_of=transaction_id,
            )
            return self._transaction(connection, posted)

        return self._write(action)

    def account_transactions(self, identifier: str, *, limit: int = 100, after: str | None = None) -> dict:
        identifier = account_id(identifier)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise LedgerError("limit must be between 1 and 500")
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            account = self._account(connection, identifier)
            cursor = ("", 0)
            if after is not None:
                previous = connection.execute(
                    """SELECT t.created_at, t.sequence FROM transactions t
                       JOIN entries e ON e.transaction_id = t.id WHERE t.id = ? AND e.account_id = ?""",
                    (after, identifier),
                ).fetchone()
                if previous is None:
                    raise LedgerError("after must identify a transaction in this account's history")
                cursor = (previous["created_at"], previous["sequence"])
            rows = connection.execute(
                """SELECT t.*, e.amount_cents AS account_amount_cents
                   FROM entries e JOIN transactions t ON t.id = e.transaction_id
                   WHERE e.account_id = ? AND (t.created_at, t.sequence) > (?, ?)
                   ORDER BY t.created_at, t.sequence LIMIT ?""",
                (identifier, *cursor, limit + 1),
            ).fetchall()
            return {
                "account_id": identifier,
                "transactions": [
                    {
                        "id": row["id"],
                        "created_at": row["created_at"],
                        "reversal_of": row["reversal_of"],
                        "source_account": row["source_account_id"],
                        "destination_account": row["destination_account_id"],
                        "account_amount": amount_text(row["account_amount_cents"]),
                        "currency": account["currency"],
                    }
                    for row in rows[:limit]
                ],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }
        finally:
            connection.close()

    def _audit(self, connection: sqlite3.Connection, cutoff: str | None = None) -> dict:
        # ponytail: O(accounts + journal) memory for audits; stream per-account aggregates when volume requires it.
        accounts = connection.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        transactions = connection.execute("SELECT * FROM transactions ORDER BY sequence").fetchall()
        entries = {
            (row["transaction_id"], row["account_id"]): row["amount_cents"]
            for row in connection.execute("SELECT * FROM entries")
        }
        expected_entries = {}
        before = {row["id"]: 0 for row in accounts}
        after = before.copy()
        fx = before.copy()
        for transaction in transactions:
            changes = (
                (transaction["source_account_id"], -transaction["source_amount_cents"]),
                (transaction["destination_account_id"], transaction["destination_amount_cents"]),
            )
            is_before = cutoff is None or transaction["created_at"] < cutoff
            for identifier, cents in changes:
                expected_entries[transaction["id"], identifier] = cents
                (before if is_before else after)[identifier] += cents
                if is_before and transaction["source_currency"] != transaction["destination_currency"]:
                    fx[identifier] += cents
        journal_discrepancies = [
            {
                "transaction_id": transaction_id,
                "account_id": identifier,
                "expected_amount": amount_text(expected_entries[transaction_id, identifier])
                if (transaction_id, identifier) in expected_entries else None,
                "actual_amount": amount_text(entries[transaction_id, identifier])
                if (transaction_id, identifier) in entries else None,
            }
            for transaction_id, identifier in sorted(entries.keys() | expected_entries.keys())
            if entries.get((transaction_id, identifier)) != expected_entries.get((transaction_id, identifier))
        ]
        totals = {}
        account_reports = []
        for account in accounts:
            if cutoff is not None and account["created_at"] >= cutoff:
                continue
            identifier = account["id"]
            opening = account["opening_balance_cents"]
            expected = opening + before[identifier]
            actual = account["balance_cents"] - after[identifier]
            account_reports.append({
                "account_id": identifier,
                "currency": account["currency"],
                "expected_balance": amount_text(expected),
                "actual_balance": amount_text(actual),
                "difference": amount_text(actual - expected),
            })
            total = totals.setdefault(account["currency"], dict(opening=0, fx_adjustment=0, expected=0, actual=0, difference=0))
            for name, cents in (("opening", opening), ("fx_adjustment", fx[identifier]), ("expected", expected), ("actual", actual)):
                total[name] += cents
            total["difference"] += actual - expected
        discrepancies = [row for row in account_reports if row["difference"] != "0.00"]
        return {
            "ok": not discrepancies and not journal_discrepancies,
            "accounts": account_reports,
            "discrepancies": discrepancies,
            "journal_discrepancies": journal_discrepancies,
            "totals": {code: {name: amount_text(cents) for name, cents in total.items()} for code, total in sorted(totals.items())},
        }

    def verify(self) -> dict:
        connection = self._connect()
        try:
            connection.execute("BEGIN")  # All audit reads see the same committed snapshot.
            return self._audit(connection)
        finally:
            connection.close()

    def health(self) -> dict:
        connection = self._connect()
        try:
            connection.execute("SELECT id FROM accounts LIMIT 1").fetchone()
            return {"status": "ok"}
        finally:
            connection.close()

    def reconcile(self, month: str) -> dict:
        if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
            raise LedgerError("month must be YYYY-MM")
        try:
            start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
            cutoff = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1, tzinfo=timezone.utc)
        except ValueError:
            raise LedgerError("month must be YYYY-MM") from None
        cutoff_text = cutoff.isoformat(timespec="microseconds")

        def action(connection):
            reconciled_at = now()
            if cutoff > datetime.fromisoformat(reconciled_at):
                raise Conflict("only completed UTC calendar months can be reconciled")
            report = self._audit(connection, cutoff_text)
            report.update(id=str(uuid.uuid4()), month=month, cutoff=cutoff_text, reconciled_at=reconciled_at)
            connection.execute(
                "INSERT INTO reconciliations VALUES (?, ?, ?, ?)",
                (report["id"], month, reconciled_at, json.dumps(report)),
            )
            return report

        return self._write(action)

    def get_reconciliation(self, identifier: str) -> dict:
        connection = self._connect()
        try:
            row = connection.execute("SELECT report FROM reconciliations WHERE id = ?", (identifier,)).fetchone()
            if row is None:
                raise NotFound("reconciliation not found")
            return json.loads(row["report"])
        finally:
            connection.close()


class Handler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format: str, *args) -> None:
        logger.info("%s %s", self.address_string(), format % args)

    @property
    def ledger(self) -> Ledger:
        return self.server.ledger  # type: ignore[attr-defined]

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if status == 503:
            self.send_header("Retry-After", "1")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.wfile.write(data)
        if status >= 400:
            # Send the error before a bounded drain: unread/delayed body bytes must not erase it with a TCP reset.
            try:
                self.connection.shutdown(socket.SHUT_WR)
                self.connection.settimeout(0.1)
                for _ in range(16):  # At most 1 MiB / 1.6 seconds, including malformed framing.
                    if not self.connection.recv(65536):
                        break
            except OSError:
                pass  # The peer may close as soon as it reads the complete response.

    def _body(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise LedgerError("Content-Type must be application/json")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,7}", lengths[0]) or self.headers.get("Transfer-Encoding"):
            raise LedgerError("one valid Content-Length is required; chunked requests are not supported")
        length = int(lengths[0])
        if length <= 0 or length > 1_000_000:
            raise LedgerError("request body must be non-empty JSON under 1 MB")
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise LedgerError("incomplete request body")
            body = json.loads(raw, parse_float=Decimal, parse_constant=reject_json_constant, object_pairs_hook=json_object)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LedgerError("request body must be valid JSON") from None
        except TimeoutError:
            raise LedgerError("request body timed out") from None
        except (RecursionError, ValueError):
            raise LedgerError("request JSON is too complex") from None
        if not isinstance(body, dict):
            raise LedgerError("request JSON must be an object")
        return body

    def _dispatch(self, method: str) -> dict:
        parsed = urlsplit(self.path)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if method == "GET" and not parts:
            return {"service": "internal-ledger", "health": "/health", "message": "JSON API; see the repository README for usage."}
        if method == "GET" and parts == ["health"]:
            try:
                return self.ledger.health()
            except sqlite3.Error:
                logger.exception("healthcheck could not read the database")
                raise Unavailable("database unavailable") from None
        token = getattr(self.server, "api_token", None)
        if token:
            authorization = self.headers.get_all("Authorization", [])
            if len(authorization) != 1 or not hmac.compare_digest(
                authorization[0].encode("utf-8"), f"Bearer {token}".encode("utf-8")
            ):
                raise Unauthorized("a valid Bearer token is required")
        keys = self.headers.get_all("Idempotency-Key", [])
        if len(keys) > 1:
            raise LedgerError("only one Idempotency-Key header is allowed")
        if method == "GET" and parts == ["integrity"]:
            return self.ledger.verify()
        if method == "GET" and len(parts) == 2 and parts[0] == "accounts":
            return self.ledger.get_account(parts[1])
        if method == "GET" and len(parts) == 3 and parts[0] == "accounts" and parts[2] == "transactions":
            query = parse_qs(parsed.query, keep_blank_values=True)
            if query.keys() - {"limit", "after"} or any(len(values) != 1 for values in query.values()):
                raise LedgerError("history accepts one limit and one after parameter")
            try:
                limit = int(query.get("limit", ["100"])[0])
            except ValueError:
                raise LedgerError("limit must be an integer") from None
            return self.ledger.account_transactions(parts[1], limit=limit, after=query.get("after", [None])[0])
        if method == "GET" and len(parts) == 2 and parts[0] == "reconciliations":
            return self.ledger.get_reconciliation(parts[1])
        if method == "POST" and parts == ["accounts"]:
            return self.ledger.create_account(self._body())
        if method == "POST" and parts == ["transactions"]:
            return self.ledger.transfer(self._body(), keys[0] if keys else None)
        if method == "POST" and len(parts) == 3 and parts[0] == "transactions" and parts[2] == "reversal":
            return self.ledger.reverse(parts[1], self._body(), keys[0] if keys else None)
        if method == "POST" and len(parts) == 2 and parts[0] == "reconciliations":
            return self.ledger.reconcile(parts[1])
        raise NotFound("route not found")

    def _handle(self, method: str) -> None:
        try:
            try:
                result = self._dispatch(method)
                status = 200 if method == "GET" else 201
            except LedgerError as error:
                status, result = error.status, {"error": str(error)}
            except Exception:
                logger.exception("request failed: %s %s", method, urlsplit(self.path).path)
                status, result = 500, {"error": "internal server error"}
            self._reply(status, result)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            logger.warning("client disconnected before receiving the response")

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", default="serve", choices=["serve"])
    parser.add_argument("--db", default=os.environ.get("LEDGER_DB_PATH", "ledger.db"))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", default=os.environ.get("PORT", "8000"), type=int)
    args = parser.parse_args()
    token = os.environ.get("LEDGER_API_TOKEN")
    if token is not None and not re.fullmatch(r"[\x21-\x7e]{32,255}", token):
        parser.error("LEDGER_API_TOKEN must contain 32-255 printable ASCII characters without spaces")
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not token:
        parser.error("LEDGER_API_TOKEN is required when listening beyond localhost")
    if os.environ.get("RAILWAY_ENVIRONMENT_ID"):
        mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
        if not mount or not Path(mount).is_dir() or not Path(args.db).resolve().is_relative_to(Path(mount).resolve()):
            parser.error("Railway requires an attached volume and LEDGER_DB_PATH inside its mount path")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ledger = Ledger(args.db)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.ledger = ledger  # type: ignore[attr-defined]
    server.api_token = token  # type: ignore[attr-defined]
    print(f"Ledger listening on http://{args.host}:{server.server_port}", flush=True)

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

"""Versioned HTML statement adapters with a review-then-confirm workflow.

Finance stages an authorized HTML response under an adapter version, reviews
a preview of normalized BankEvidence lines plus quarantined rows carrying
their original table position, and only then confirms.  The confirmation
binds the source digest and adapter version; any change after preview forces
a fresh review, and business transaction identity (channel + account +
transaction id) prevents the same flow from being booked twice across
months, pages or row numbers.
"""

import base64

from ..primitives import (
    identifier,
    integer,
    canonical,
    digest,
    decode,
    error,
)
from .htmladapter import (
    validate_config,
    decode_source,
    parse_statement,
    compose_line_id,
)

MAX_SOURCE_BYTES = 10_000_000

DDL = """
CREATE TABLE IF NOT EXISTS bank_adapters(
 id TEXT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bank_statement_runs(
 id TEXT PRIMARY KEY, channel TEXT NOT NULL, account TEXT NOT NULL,
 adapter TEXT NOT NULL, adapter_version INTEGER NOT NULL, source TEXT NOT NULL,
 source_digest TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
 review_digest TEXT, reviewer TEXT, result TEXT, quarantine TEXT);
"""


class BankStatements:
    def __init__(self, database, bank):
        self.db = database
        self.bank = bank
        self.db.connection.executescript(DDL)

    # -- versioned adapters -------------------------------------------------

    def save_adapter(self, adapter_id, config, expected_version=0):
        identifier(adapter_id)
        normalized = validate_config(config)
        integer(expected_version, 0)
        payload = {"config": normalized}
        with self.db.transaction() as con:
            old = self.db.one(
                "SELECT * FROM bank_adapters WHERE id=?", (adapter_id,)
            )
            version = old["version"] if old else 0
            if expected_version != version:
                error("conflict", "adapter version changed")
            version += 1
            con.execute(
                "INSERT INTO bank_adapters VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET version=excluded.version,"
                "payload=excluded.payload,digest=excluded.digest",
                (adapter_id, version, canonical(payload), digest(payload)),
            )
            self.db.audit(
                "bank_adapter_saved",
                adapter_id,
                {"version": version, "digest": digest(payload)},
            )
        return self.get_adapter(adapter_id)

    def get_adapter(self, adapter_id):
        identifier(adapter_id)
        row = self.db.one("SELECT * FROM bank_adapters WHERE id=?", (adapter_id,))
        if row is None:
            error("missing", "bank statement adapter not found")
        payload = decode(row["payload"])
        if digest(payload) != row["digest"]:
            error("integrity", "adapter fingerprint differs")
        return {
            "id": adapter_id,
            "version": row["version"],
            "config": payload["config"],
            "digest": row["digest"],
        }

    # -- staging ------------------------------------------------------------

    def stage(self, run_id, channel, account, adapter_id, content=None, content_base64=None):
        identifier(run_id)
        identifier(channel)
        identifier(account)
        adapter = self.get_adapter(adapter_id)
        raw = self._read_source(content, content_base64)
        text, codec = decode_source(raw)
        source_payload = {
            "content": text,
            "encoding": codec,
            "adapter": adapter_id,
            "adapter_version": adapter["version"],
        }
        checksum = digest(source_payload)
        with self.db.transaction() as con:
            old = self.db.one(
                "SELECT * FROM bank_statement_runs WHERE id=?", (run_id,)
            )
            if old:
                if old["source_digest"] != checksum:
                    error("conflict", "staged statement identity differs")
                return self.get(run_id)
            con.execute(
                "INSERT INTO bank_statement_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    channel,
                    account,
                    adapter_id,
                    adapter["version"],
                    canonical(source_payload),
                    checksum,
                    "staged",
                    1,
                    None,
                    None,
                    None,
                    None,
                ),
            )
            self.db.audit(
                "bank_statement_staged",
                run_id,
                {
                    "digest": checksum,
                    "adapter": adapter_id,
                    "adapter_version": adapter["version"],
                },
            )
        return self.get(run_id)

    @staticmethod
    def _read_source(content, content_base64):
        if content_base64 is not None:
            if content is not None or not isinstance(content_base64, str):
                error("input", "provide exactly one of content or content_base64")
            try:
                raw = base64.b64decode(content_base64, validate=True)
            except (ValueError, TypeError) as exc:
                error("input", "content_base64 is not valid base64", reason=str(exc))
        elif isinstance(content, str):
            raw = content.encode("utf-8")
        else:
            error("input", "HTML statement content must be text or base64 bytes")
        if not raw or len(raw) > MAX_SOURCE_BYTES:
            error("input", "HTML statement is empty or exceeds supported size")
        return raw

    def get(self, run_id):
        identifier(run_id)
        row = self.db.one(
            "SELECT * FROM bank_statement_runs WHERE id=?", (run_id,)
        )
        if row is None:
            error("missing", "bank statement run not found")
        source = decode(row["source"])
        if digest(source) != row["source_digest"]:
            error("integrity", "staged statement changed")
        return {
            "id": run_id,
            "channel": row["channel"],
            "account": row["account"],
            "adapter": row["adapter"],
            "adapter_version": row["adapter_version"],
            "state": row["state"],
            "revision": row["revision"],
            "review_digest": row["review_digest"],
            "reviewer": row["reviewer"],
            "source_digest": row["source_digest"],
            "encoding": source["encoding"],
            "result": decode(row["result"]) if row["result"] else None,
            "quarantine": decode(row["quarantine"]) if row["quarantine"] else [],
        }

    # -- preview / confirm --------------------------------------------------

    def _load_snapshot(self, run_id):
        row = self.db.one(
            "SELECT * FROM bank_statement_runs WHERE id=?", (run_id,)
        )
        if row is None:
            error("missing", "bank statement run not found")
        adapter_row = self.db.one(
            "SELECT * FROM bank_adapters WHERE id=?", (row["adapter"],)
        )
        if adapter_row is None:
            error("missing", "bound adapter no longer exists")
        if adapter_row["version"] != row["adapter_version"]:
            error(
                "conflict",
                "adapter rules changed after staging; review the statement again",
                staged_version=row["adapter_version"],
                current_version=adapter_row["version"],
            )
        source = decode(row["source"])
        config = decode(adapter_row["payload"])["config"]
        return row, source, config

    def preview(self, run_id):
        """Pure read: normalize the staged HTML and compute review evidence."""
        row, source, config = self._load_snapshot(run_id)
        lines, failures, audit = parse_statement(source["content"], config)
        prepared = []
        for line in lines:
            line_id = compose_line_id(row["channel"], row["account"], line["tx_id"])
            prepared.append(
                {
                    "id": line_id,
                    "tx_id": line["tx_id"],
                    "account": row["account"],
                    "currency": line["currency"],
                    "amount": line["amount"],
                    "direction": line["direction"],
                    "booked": line["booked"],
                    "reference": line["reference"],
                    "payer": line["payer"],
                    "memo": line["memo"],
                    "location": line["location"],
                }
            )
        # Same business flow already booked (possibly in another month/page):
        # replay is safe for identical evidence; differences are quarantined.
        store_conflicts = []
        for item in prepared:
            existing = self.db.one(
                "SELECT * FROM bank_lines WHERE id=?", (item["id"],)
            )
            if existing is None:
                item["status"] = "new"
                continue
            if existing["digest"] == digest(self._bank_payload(item)):
                item["status"] = "existing"
            else:
                item["status"] = "conflict"
                store_conflicts.append(
                    {
                        "ref": item["tx_id"],
                        "code": "conflict",
                        "message": "transaction id already booked with different content",
                        "location": item["location"],
                        "details": {"bank_line": item["id"]},
                    }
                )
        quarantine = failures + store_conflicts
        review = {
            "run": run_id,
            "source_digest": row["source_digest"],
            "adapter": row["adapter"],
            "adapter_version": row["adapter_version"],
            "lines": [
                {key: item[key] for key in item if key != "status"}
                for item in prepared
            ],
            "statuses": {item["id"]: item["status"] for item in prepared},
            "quarantine": quarantine,
            "excluded": audit["skipped"],
            "duplicates": audit["duplicates"],
            "encoding": source["encoding"],
        }
        return {
            "id": run_id,
            "state": row["state"],
            "revision": row["revision"],
            "adapter": row["adapter"],
            "adapter_version": row["adapter_version"],
            "source_digest": row["source_digest"],
            "lines": prepared,
            "quarantine": quarantine,
            "excluded": audit["skipped"],
            "duplicates": audit["duplicates"],
            "review_digest": digest(review),
        }

    @staticmethod
    def _bank_payload(item):
        return {
            "id": item["id"],
            "account": item["account"],
            "currency": item["currency"],
            "amount": item["amount"],
            "booked": item["booked"],
            "reference": item["reference"],
            "payer": item["payer"],
            "direction": item["direction"],
        }

    def confirm(self, run_id, expected_revision, review_digest, reviewer):
        identifier(run_id)
        identifier(reviewer)
        integer(expected_revision, 1)
        preview = self.preview(run_id)
        if preview["review_digest"] != review_digest:
            error(
                "conflict",
                "preview no longer matches; rules or rows changed, review again",
            )
        run = self.get(run_id)
        if run["state"] == "confirmed":
            if expected_revision != run["revision"] - 1:
                error("conflict", "confirm revision differs")
            return run
        if run["state"] != "staged" or run["revision"] != expected_revision:
            error("conflict", "statement run is not staged at this revision")
        if preview["quarantine"]:
            # Durable isolation in its own committed unit so the quarantine
            # survives the rejected confirmation.
            with self.db.transaction() as con:
                con.execute(
                    "UPDATE bank_statement_runs SET quarantine=? WHERE id=?",
                    (canonical(preview["quarantine"]), run_id),
                )
                self.db.audit(
                    "bank_statement_quarantined",
                    run_id,
                    {"count": len(preview["quarantine"])},
                )
            error(
                "quarantine",
                "statement contains quarantined rows",
                quarantine=preview["quarantine"],
            )
        with self.db.transaction() as con:
            inserted = 0
            existing = 0
            for item in preview["lines"]:
                outcome = self.bank.add(self._bank_payload(item))
                inserted += not outcome["existing"]
                existing += outcome["existing"]
            result = {
                "run": run_id,
                "inserted": inserted,
                "existing": existing,
                "quarantined": 0,
                "source_digest": run["source_digest"],
                "adapter": run["adapter"],
                "adapter_version": run["adapter_version"],
            }
            con.execute(
                "UPDATE bank_statement_runs SET state='confirmed',revision=revision+1,"
                "review_digest=?,reviewer=?,result=? WHERE id=?",
                (review_digest, reviewer, canonical(result), run_id),
            )
            self.db.audit("bank_statement_confirmed", run_id, result)
        return self.get(run_id)

    def reject(self, run_id, expected_revision, reason, reviewer):
        identifier(reviewer)
        integer(expected_revision, 1)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            error("input", "rejection requires bounded reason")
        with self.db.transaction() as con:
            run = self.get(run_id)
            if run["state"] != "staged" or run["revision"] != expected_revision:
                error("conflict", "statement run revision changed")
            result = {"reason": reason, "reviewer": reviewer}
            con.execute(
                "UPDATE bank_statement_runs SET state='rejected',revision=revision+1,"
                "reviewer=?,result=? WHERE id=?",
                (reviewer, canonical(result), run_id),
            )
            self.db.audit("bank_statement_rejected", run_id, result)
        return self.get(run_id)

    def pending(self, channel=None, limit=100):
        integer(limit, 1, 1000)
        if channel is not None:
            identifier(channel)
        rows = self.db.rows(
            "SELECT * FROM bank_statement_runs WHERE state='staged' ORDER BY id LIMIT ?",
            (limit,),
        )
        return {
            "rows": [
                {
                    "id": row["id"],
                    "channel": row["channel"],
                    "account": row["account"],
                    "adapter": row["adapter"],
                    "adapter_version": row["adapter_version"],
                    "revision": row["revision"],
                }
                for row in rows
                if channel is None or row["channel"] == channel
            ]
        }

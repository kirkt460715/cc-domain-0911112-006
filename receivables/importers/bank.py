from ..primitives import (
    identifier,
    day,
    currency,
    money,
    display,
    obj,
    canonical,
    digest,
    error,
)


class BankEvidence:
    def __init__(self, database, ledger):
        self.db = database
        self.ledger = ledger
        self._ensure_direction_column()

    def _ensure_direction_column(self):
        """Gentle migration for stores that only held positive inbound lines."""
        columns = {
            row["name"]
            for row in self.db.rows("PRAGMA table_info(bank_lines)")
        }
        if "direction" not in columns:
            self.db.connection.executescript(
                "ALTER TABLE bank_lines ADD COLUMN direction TEXT NOT NULL DEFAULT 'credit'"
            )

    def add(self, data):
        fields = {
            "id",
            "account",
            "currency",
            "amount",
            "booked",
            "reference",
            "payer",
            "direction",
        }
        obj(data, fields, {"id", "account", "currency", "amount", "booked", "reference", "payer"})
        ident = identifier(data["id"])
        code = currency(data["currency"])
        amount = money(data["amount"], code)
        booked = day(data["booked"])
        direction = data.get("direction", "credit")
        if direction not in {"credit", "debit"}:
            error("input", "direction must be credit or debit")
        for field in ("reference", "payer"):
            if not isinstance(data[field], str) or len(data[field]) > 512:
                error("input", "bank reference and payer must be bounded text")
        payload = {
            **data,
            "amount": display(amount, code),
            "booked": booked,
            "account": identifier(data["account"]),
            "direction": direction,
        }
        with self.db.transaction() as con:
            old = self.db.one("SELECT * FROM bank_lines WHERE id=?", (ident,))
            if old:
                # Legacy rows were recorded before direction existed: an inbound
                # replay without the field must remain exactly compatible.
                old_direction = old["direction"] if "direction" in old.keys() else "credit"
                if old["digest"] != digest(payload) and not (
                    "direction" not in data
                    and old_direction == "credit"
                    and digest({k: v for k, v in payload.items() if k != "direction"})
                    == old["digest"]
                ):
                    error("conflict", "bank line identity differs")
                if "direction" not in data and old_direction == "credit":
                    con.execute(
                        "UPDATE bank_lines SET payload=?,digest=? WHERE id=?",
                        (canonical(payload), digest(payload), ident),
                    )
                    self.db.audit(
                        "bank_line_direction_normalized", ident, {"direction": "credit"}
                    )
                return {"id": ident, "existing": True, "direction": old_direction}
            con.execute(
                "INSERT INTO bank_lines VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    payload["account"],
                    code,
                    amount,
                    booked,
                    data["reference"],
                    data["payer"],
                    canonical(payload),
                    digest(payload),
                    direction,
                ),
            )
            self.db.audit("bank_line_added", ident, payload)
        return {"id": ident, "existing": False, "direction": direction}

    def line(self, line_id):
        row = self.db.one("SELECT * FROM bank_lines WHERE id=?", (line_id,))
        if row is None:
            error("missing", "bank line not found")
        return row

    def candidates(self, line_id):
        from datetime import date

        line = self.line(line_id)
        if line["direction"] == "debit":
            # Outgoing payments never enter the receipt-clearing candidate set.
            return {
                "line": line_id,
                "candidates": [],
                "ambiguous": False,
                "already_linked": False,
                "excluded": "debit_line_is_not_receipt",
            }
        if self.db.one("SELECT line FROM bank_links WHERE line=?", (line_id,)):
            return {"line": line_id, "candidates": [], "already_linked": True}
        result = []
        for payment in self.db.rows(
            "SELECT * FROM documents WHERE kind='payment' AND currency=? AND amount=? ORDER BY id",
            (line["currency"], line["amount"]),
        ):
            if not self.ledger.active_at(payment["id"], "2200-12-31"):
                continue
            if self.db.one(
                "SELECT line FROM bank_links WHERE payment=?", (payment["id"],)
            ):
                continue
            party = self.ledger.party(payment["party"])
            score = 0
            evidence = []
            if (
                line["reference"]
                and line["reference"].casefold() == payment["reference"].casefold()
            ):
                score += 70
                evidence.append("exact_reference")
            aliases = {party["name"].casefold(), *party["aliases"]}
            if line["payer"].strip().casefold() in aliases:
                score += 20
                evidence.append("payer_alias")
            delta = abs(
                (
                    date.fromisoformat(line["booked"])
                    - date.fromisoformat(payment["issued"])
                ).days
            )
            if delta <= 3:
                score += 10 - delta
                evidence.append("near_date")
            result.append(
                {
                    "payment": payment["id"],
                    "party": payment["party"],
                    "score": score,
                    "evidence": evidence,
                    "day_difference": delta,
                }
            )
        result.sort(key=lambda item: (-item["score"], item["payment"]))
        ambiguous = len(result) > 1 and result[0]["score"] == result[1]["score"]
        return {
            "line": line_id,
            "candidates": result,
            "ambiguous": ambiguous,
            "already_linked": False,
        }

    def link(self, line_id, payment_id, operator):
        identifier(operator)
        with self.db.transaction() as con:
            line = self.line(line_id)
            if line["direction"] == "debit":
                error("bank", "debit bank lines cannot be linked as receipts")
            payment = self.ledger.document(payment_id)
            if (
                payment["kind"] != "payment"
                or payment["currency"] != line["currency"]
                or payment["amount"] != line["amount"]
            ):
                error("bank", "bank evidence amount/currency/payment kind mismatch")
            if not self.ledger.active_at(payment_id, "2200-12-31"):
                error("bank", "cannot link reversed payment")
            existing = self.db.one("SELECT * FROM bank_links WHERE line=?", (line_id,))
            if existing:
                if existing["payment"] != payment_id:
                    error("conflict", "bank line linked to other payment")
                return {**existing, "existing": True}
            if self.db.one(
                "SELECT line FROM bank_links WHERE payment=?", (payment_id,)
            ):
                error("conflict", "payment already linked to another bank line")
            con.execute(
                "INSERT INTO bank_links VALUES(?,?,?)", (line_id, payment_id, operator)
            )
            self.db.audit(
                "bank_linked", line_id, {"payment": payment_id, "operator": operator}
            )
        return {
            "line": line_id,
            "payment": payment_id,
            "linked_by": operator,
            "existing": False,
        }

    def unlink(self, line_id, reason):
        if not isinstance(reason, str) or not reason.strip():
            error("input", "unlink requires reason")
        with self.db.transaction() as con:
            link = self.db.one("SELECT * FROM bank_links WHERE line=?", (line_id,))
            if link is None:
                error("missing", "bank link not found")
            con.execute("DELETE FROM bank_links WHERE line=?", (line_id,))
            self.db.audit(
                "bank_unlinked", line_id, {"payment": link["payment"], "reason": reason}
            )
        return link

    def unmatched(self, account=None):
        rows = self.db.rows(
            "SELECT b.* FROM bank_lines b LEFT JOIN bank_links l ON l.line=b.id "
            "WHERE l.line IS NULL AND b.direction='credit' ORDER BY b.booked,b.id"
        )
        return [row for row in rows if account is None or row["account"] == account]

    def outgoing(self, account=None):
        rows = self.db.rows(
            "SELECT * FROM bank_lines WHERE direction='debit' ORDER BY booked,id"
        )
        return [row for row in rows if account is None or row["account"] == account]

    def reconcile(self, account, start, end):
        start, end = day(start), day(end)
        if end <= start:
            error("input", "reconciliation interval must increase")
        groups = {}
        for line in self.db.rows(
            "SELECT * FROM bank_lines WHERE account=? AND booked>=? AND booked<? ORDER BY booked,id",
            (account, start, end),
        ):
            values = groups.setdefault(
                line["currency"],
                {
                    "total": 0,
                    "inflow": 0,
                    "outflow": 0,
                    "linked": 0,
                    "unmatched": 0,
                    "lines": 0,
                    "debit_lines": 0,
                },
            )
            values["lines"] += 1
            if line["direction"] == "debit":
                values["outflow"] += line["amount"]
                values["debit_lines"] += 1
                continue
            values["inflow"] += line["amount"]
            # "total" keeps the historical inbound-only semantics for old readers.
            values["total"] += line["amount"]
            linked = self.db.one(
                "SELECT payment FROM bank_links WHERE line=?", (line["id"],)
            )
            values["linked" if linked else "unmatched"] += line["amount"]
        return {
            "account": account,
            "start": start,
            "end": end,
            "currencies": {
                code: {
                    key: display(value, code)
                    if key not in ("lines", "debit_lines")
                    else value
                    for key, value in values.items()
                }
                for code, values in groups.items()
            },
        }

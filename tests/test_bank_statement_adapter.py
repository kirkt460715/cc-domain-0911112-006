import base64
import tempfile
import unittest
from pathlib import Path

from receivables.service import Workbench
from receivables.primitives import BookError, digest, canonical
from receivables.importers.htmladapter import (
    parse_statement,
    decode_source,
    parse_amount,
    validate_config,
    AdapterConfigError,
    expand_grid,
)

FIXTURE_A = (
    '<html><meta charset="utf-8"><table id="statement">'
    '<tr><th rowspan="2">流水号</th><th rowspan="2">日期</th>'
    '<th colspan="2">发生额</th><th rowspan="2">币种</th></tr>'
    '<tr><th>借方</th><th>贷方</th></tr>'
    '<tr><td>TX-001</td><td>2026-11-01</td><td></td><td>1,234.50</td><td>CNY</td></tr>'
    '<tr class="memo"><td colspan="5">付款人：合成工厂；附言：INV-001</td></tr>'
    '<tr><td>TX-002</td><td>2026-11-02</td><td>20.00</td><td></td><td>CNY</td></tr>'
    '<tr><td>TX-004</td><td>2026-11-03</td><td></td><td>(1,000.00)</td><td>CNY</td></tr>'
    '<tr><td>TX-005</td><td>2026-11-04</td><td></td><td>500</td><td>JPY</td></tr>'
    '<tr class="opening"><td colspan="5">期初余额 9,999.00</td></tr>'
    '<tr><td>TX-006</td><td>2026-11-05</td><td></td><td>30.00</td><td>CNY</td></tr>'
    '<tr><td colspan="5">第 2 页 / 共 3 页</td></tr>'
    '<tr class="total"><td colspan="3">合计</td><td>1,214.50</td><td>CNY</td></tr>'
    '</table></html>'
).encode("utf-8")

CFG_A = {
    "table_id": "statement",
    "mapping": {
        "id": "流水号",
        "booked": "日期",
        "debit": "发生额 / 借方",
        "credit": "发生额 / 贷方",
        "currency": "币种",
    },
    "date_formats": ["%Y-%m-%d"],
    "skip_classes": ["total", "opening"],
    "skip_patterns": [r"期初余额", r"第\s*\d+\s*页", r"分页"],
    "memo": {
        "continuation_classes": ["memo"],
        "patterns": [
            {"field": "payer", "pattern": r"付款人：(?P<value>[^；]+)"},
            {"field": "reference", "pattern": r"附言：(?P<value>[^；\s]+)"},
        ],
    },
}

FIXTURE_B = (
    '<html><meta charset="gb18030"><table id="statement">'
    "<tr><th>记账日</th><th>币种</th><th>贷记金额</th><th>交易编号</th>"
    "<th>借记金额</th><th>附言</th></tr>"
    '<tr><td>2026-11-01</td><td>CNY</td><td>1,234.50</td><td>TX-001</td>'
    '<td></td><td>INV-001</td></tr>'
    '<tr><td>01/02/2026</td><td>CNY</td><td>1.00</td><td>TX-003</td>'
    '<td>2.00</td><td>需要复核</td></tr>'
    "</table></html>"
).encode("gb18030")

CFG_B = {
    "table_id": "statement",
    "mapping": {
        "id": "交易编号",
        "booked": "记账日",
        "debit": "借记金额",
        "credit": "贷记金额",
        "currency": "币种",
        "memo": "附言",
    },
    "date_formats": ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"],
    "skip_patterns": [r"期初余额", r"分页"],
}


class AdapterParsingCase(unittest.TestCase):
    def test_rowspan_colspan_memo_and_exclusions(self):
        text, codec = decode_source(FIXTURE_A)
        self.assertEqual(codec, "utf-8")
        lines, failures, audit = parse_statement(text, CFG_A)
        self.assertEqual(failures, [])
        by_id = {line["tx_id"]: line for line in lines}
        self.assertEqual(
            [line["tx_id"] for line in lines],
            ["TX-001", "TX-002", "TX-004", "TX-005", "TX-006"],
        )
        first = by_id["TX-001"]
        self.assertEqual(first["amount"], "1234.50")
        self.assertEqual(first["direction"], "credit")
        self.assertEqual(first["payer"], "合成工厂")
        self.assertEqual(first["reference"], "INV-001")
        self.assertIn("INV-001", first["memo"])
        self.assertEqual(first["location"]["table_row"], 3)
        self.assertEqual(by_id["TX-002"]["direction"], "debit")
        # Brackets flip the credit column into an outgoing flow.
        self.assertEqual(by_id["TX-004"]["direction"], "debit")
        self.assertEqual(by_id["TX-004"]["amount"], "1000.00")
        # Zero-decimal currency keeps its precision.
        self.assertEqual(by_id["TX-005"]["currency"], "JPY")
        self.assertEqual(by_id["TX-005"]["amount"], "500")
        excluded = {item["text"]: item["reason"] for item in audit["skipped"]}
        self.assertTrue(any("期初余额" in text for text in excluded))
        self.assertTrue(any("合计" in text for text in excluded))
        self.assertTrue(any("第 2 页" in text for text in excluded))

    def test_gb18030_layout_and_quarantine_locations(self):
        text, codec = decode_source(FIXTURE_B)
        self.assertEqual(codec, "gb18030")
        lines, failures, _ = parse_statement(text, CFG_B)
        self.assertEqual([line["tx_id"] for line in lines], ["TX-001"])
        self.assertEqual(lines[0]["memo"], "INV-001")
        codes = {(item["code"], item["ref"]) for item in failures}
        self.assertEqual(
            codes, {("date", "TX-003"), ("direction", "TX-003")}
        )
        for item in failures:
            self.assertEqual(item["location"]["table_row"], 3)
            self.assertTrue(item["location"]["source_line"])

    def test_missing_fields_report_positions(self):
        html = (
            '<html><table><tr><th>id</th><th>d</th><th>cur</th>'
            '<th>db</th><th>cr</th></tr>'
            '<tr><td></td><td>2026-11-01</td><td>CNY</td><td></td><td>1.00</td></tr>'
            '<tr><td>X2</td><td></td><td>CNY</td><td></td><td>1.00</td></tr>'
            "</table></html>"
        )
        config = {
            "mapping": {
                "id": "id", "booked": "d", "currency": "cur",
                "debit": "db", "credit": "cr",
            }
        }
        lines, failures, _ = parse_statement(html, config)
        self.assertEqual(lines, [])
        self.assertEqual({item["code"] for item in failures}, {"missing_field"})
        self.assertEqual(
            {item["details"]["field"] for item in failures}, {"id", "booked"}
        )
        self.assertTrue(all(item["location"]["table_row"] for item in failures))

    def test_amount_variants_and_currency_precision(self):
        self.assertEqual(parse_amount("1,234.56")[0], "1234.56")
        self.assertEqual(parse_amount("1，234.56")[0], "1234.56")
        signed, negative = parse_amount("(1,000.00)")
        self.assertEqual(signed, "-1000.00")
        self.assertTrue(negative)
        self.assertIsNone(parse_amount("")[0])
        with self.assertRaises(ValueError):
            parse_amount("abc")
        html = (
            "<html><table><tr><th>id</th><th>d</th><th>cur</th>"
            "<th>db</th><th>cr</th></tr>"
            '<tr><td>J1</td><td>2026-11-01</td><td>JPY</td><td></td><td>1,234</td></tr>'
            '<tr><td>J2</td><td>2026-11-01</td><td>CNY</td><td></td><td>1.001</td></tr>'
            '<tr><td>J3</td><td>2026-11-01</td><td>JPY</td><td></td><td>12.5</td></tr>'
            "</table></html>"
        )
        config = {
            "mapping": {
                "id": "id", "booked": "d", "currency": "cur",
                "debit": "db", "credit": "cr",
            }
        }
        lines, failures, _ = parse_statement(html, config)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["amount"], "1234")  # zero-decimal currency
        self.assertEqual(
            sorted(item["code"] for item in failures), ["precision", "precision"]
        )
        self.assertEqual(
            sorted(item["ref"] for item in failures), ["J2", "J3"]
        )

    def test_duplicate_and_conflicting_ids_with_positions(self):
        html = (
            "<html><table><tr><th>id</th><th>d</th><th>cur</th>"
            "<th>db</th><th>cr</th></tr>"
            '<tr><td>D1</td><td>2026-11-01</td><td>CNY</td><td></td><td>1.00</td></tr>'
            '<tr><td>D1</td><td>2026-11-01</td><td>CNY</td><td></td><td>1.00</td></tr>'
            '<tr><td>D2</td><td>2026-11-02</td><td>CNY</td><td></td><td>2.00</td></tr>'
            '<tr><td>D2</td><td>2026-11-03</td><td>CNY</td><td></td><td>2.00</td></tr>'
            "</table></html>"
        )
        config = {
            "mapping": {
                "id": "id", "booked": "d", "currency": "cur",
                "debit": "db", "credit": "cr",
            }
        }
        lines, failures, audit = parse_statement(html, config)
        self.assertEqual([line["tx_id"] for line in lines], ["D1", "D2"])
        self.assertEqual(len(audit["duplicates"]), 1)
        self.assertEqual(
            {(item["code"], item["ref"]) for item in failures},
            {("conflict", "D2"), ("conflict", "D2")},
        )
        rows = {item["location"]["table_row"] for item in failures}
        self.assertEqual(rows, {4, 5})

    def test_repeated_header_rows_are_skipped(self):
        html = (
            "<html><table><tr><th>id</th><th>d</th><th>cur</th>"
            "<th>db</th><th>cr</th></tr>"
            '<tr><td>H1</td><td>2026-11-01</td><td>CNY</td><td></td><td>1.00</td></tr>'
            "<tr><th>id</th><th>d</th><th>cur</th><th>db</th><th>cr</th></tr>"
            '<tr><td>H2</td><td>2026-11-02</td><td>CNY</td><td>2.00</td><td></td></tr>'
            "</table></html>"
        )
        config = {
            "mapping": {
                "id": "id", "booked": "d", "currency": "cur",
                "debit": "db", "credit": "cr",
            }
        }
        lines, failures, audit = parse_statement(html, config)
        self.assertEqual([line["tx_id"] for line in lines], ["H1", "H2"])
        self.assertEqual(
            [item["reason"] for item in audit["skipped"]], ["repeated_header"]
        )
        self.assertEqual(lines[1]["direction"], "debit")

    def test_bad_adapter_configuration_is_rejected(self):
        with self.assertRaises(AdapterConfigError):
            validate_config({"mapping": {"id": "x"}})
        with self.assertRaises(AdapterConfigError):
            validate_config(
                {
                    "mapping": {
                        "id": "id", "booked": "d", "currency": "cur",
                        "debit": "db", "credit": "cr",
                    },
                    "date_formats": ["%H"],
                }
            )
        with self.assertRaises(AdapterConfigError):
            validate_config(
                {
                    "mapping": {
                        "id": "id", "booked": "d", "currency": "cur",
                        "debit": "db", "credit": "cr",
                    },
                    "skip_patterns": ["("],
                }
            )
        with self.assertRaises(AdapterConfigError):
            validate_config(
                {
                    "mapping": {
                        "id": "id", "booked": "d", "currency": "cur",
                        "debit": "db", "credit": "cr",
                    },
                    "memo": {"patterns": [{"field": "payer", "pattern": "x"}]},
                }
            )

    def test_missing_mapped_label_fails_with_position(self):
        html = (
            "<html><table><tr><th>id</th><th>d</th></tr>"
            '<tr><td>X</td><td>2026-11-01</td></tr></table></html>'
        )
        config = {
            "mapping": {
                "id": "id", "booked": "d", "currency": "cur",
                "debit": "db", "credit": "cr",
            }
        }
        lines, failures, _ = parse_statement(html, config)
        self.assertEqual(lines, [])
        self.assertTrue(all(item["code"] == "adapter" for item in failures))
        self.assertEqual(
            {item["details"]["field"] for item in failures},
            {"currency", "debit", "credit"},
        )

    def test_grid_expansion_shares_cells(self):
        from receivables.importers.htmladapter import _collect_table

        tables, _ = _collect_table(
            "<html><table><tr><td rowspan='2'>A</td><td>B</td></tr>"
            "<tr><td>C</td></tr></table></html>"
        )
        grid = expand_grid(tables[0])
        self.assertEqual(len(grid), 2)
        self.assertIs(grid[0][0], grid[1][0])
        self.assertEqual([cell.text for cell in grid[1]], ["A", "C"])

    def test_duplicate_leaf_headers_require_full_path(self):
        html = (
            "<table><tr><th colspan='2'>A</th><th colspan='2'>B</th></tr>"
            "<tr><th>x</th><th>y</th><th>x</th><th>y</th></tr>"
            "<tr><td>1</td><td>2</td><td>3</td><td>4</td></tr></table>"
        )
        ambiguous = {
            "mapping": {
                "id": "x", "booked": "y", "currency": "B / x",
                "debit": "A / y", "credit": "B / y",
            }
        }
        _, failures, _ = parse_statement(html, ambiguous)
        self.assertEqual(
            [item["details"]["field"] for item in failures], ["id", "booked"]
        )
        self.assertTrue(
            all("duplicate header" in item["message"] for item in failures)
        )
        explicit = {
            "mapping": {
                "id": "A / x", "booked": "A / y", "currency": "B / x",
                "debit": "B / y", "credit": "cr",
            }
        }
        _, path_failures, _ = parse_statement(html, explicit)
        self.assertEqual(
            [item["details"]["field"] for item in path_failures], ["credit"]
        )

    def test_orphan_and_multi_row_continuation(self):
        config = {
            "mapping": {
                "id": "id", "booked": "d", "currency": "cur",
                "debit": "db", "credit": "cr",
            }
        }
        orphan = (
            "<table><tr><th>id</th><th>d</th><th>cur</th><th>db</th><th>cr</th></tr>"
            "<tr><td colspan='5'>orphan memo</td></tr>"
            "<tr><td>X</td><td>2026-11-01</td><td>CNY</td><td></td><td>1.00</td></tr>"
            "</table>"
        )
        lines, failures, _ = parse_statement(orphan, config)
        self.assertEqual([line["tx_id"] for line in lines], ["X"])
        self.assertEqual(failures[0]["code"], "memo")
        self.assertEqual(failures[0]["location"]["table_row"], 2)
        multi = (
            "<table><tr><th>id</th><th>d</th><th>cur</th><th>db</th><th>cr</th></tr>"
            "<tr><td>X</td><td>2026-11-01</td><td>CNY</td><td></td><td>1.00</td></tr>"
            "<tr><td colspan='5'>note one</td></tr>"
            "<tr><td colspan='5'>note two</td></tr>"
            "<tr><td>Y</td><td>2026-11-02</td><td>CNY</td><td>2.00</td><td></td></tr>"
            "</table>"
        )
        lines, failures, _ = parse_statement(multi, config)
        self.assertEqual(failures, [])
        self.assertEqual(lines[0]["memo"], "note one；note two")
        self.assertEqual(lines[1]["memo"], "")

    def test_nested_table_and_blank_spanning_row_are_ignored(self):
        config = {
            "mapping": {
                "id": "id", "booked": "d", "currency": "cur",
                "debit": "db", "credit": "cr",
            }
        }
        html = (
            "<table><tr><th>id</th><th>d</th><th>cur</th><th>db</th><th>cr</th></tr>"
            "<tr><td>N1</td><td>2026-11-01</td><td>CNY</td><td></td><td>1.00</td></tr>"
            "<tr><td colspan='5'><table><tr><td>junk</td></tr></table></td></tr>"
            "<tr><td>N2</td><td>2026-11-02</td><td>CNY</td><td>2.00</td><td></td></tr>"
            "</table>"
        )
        lines, failures, audit = parse_statement(html, config)
        self.assertEqual([line["tx_id"] for line in lines], ["N1", "N2"])
        self.assertEqual(failures, [])
        self.assertTrue(
            all(item["reason"] == "blank_spanning_row" for item in audit["skipped"])
        )


class BankStatementWorkflowCase(unittest.TestCase):
    def setUp(self):
        self.w = Workbench()
        self.addCleanup(self.w.close)

    def _b64(self, raw):
        return base64.b64encode(raw).decode()

    def _save_a(self):
        return self.w.statement_adapters.save_adapter("portal-a", CFG_A)

    def _stage_a(self, run_id="run-a", content=FIXTURE_A):
        return self.w.statement_adapters.stage(
            run_id, "portal", "local-bank", "portal-a",
            content_base64=self._b64(content),
        )

    def test_adapter_versioning_and_optimistic_lock(self):
        first = self._save_a()
        self.assertEqual(first["version"], 1)
        with self.assertRaises(BookError):
            self.w.statement_adapters.save_adapter("portal-a", CFG_A)
        second = self.w.statement_adapters.save_adapter(
            "portal-a", CFG_A, expected_version=1
        )
        self.assertEqual(second["version"], 2)
        fetched = self.w.statement_adapters.get_adapter("portal-a")
        self.assertEqual(fetched["digest"], second["digest"])

    def test_preview_confirm_binds_source_and_version(self):
        self._save_a()
        staged = self._stage_a()
        self.assertEqual(staged["state"], "staged")
        self.assertEqual(staged["adapter_version"], 1)
        preview = self.w.statement_adapters.preview("run-a")
        self.assertEqual(len(preview["lines"]), 5)
        self.assertTrue(preview["review_digest"])
        result = self.w.statement_adapters.confirm(
            "run-a", 1, preview["review_digest"], "finance"
        )
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(result["reviewer"], "finance")
        self.assertEqual(result["result"]["inserted"], 5)
        self.assertEqual(result["result"]["adapter_version"], 1)
        self.assertEqual(
            result["result"]["source_digest"], staged["source_digest"]
        )
        stored = self.w.bank.line("portal:local-bank:TX-001")
        self.assertEqual(stored["direction"], "credit")
        self.assertEqual(stored["reference"], "INV-001")
        self.assertEqual(stored["payer"], "合成工厂")
        debit = self.w.bank.line("portal:local-bank:TX-002")
        self.assertEqual(debit["direction"], "debit")
        # Payload digest includes direction; audit chain stays intact.
        self.assertTrue(self.w.controls.require_valid()["ok"])

    def test_debit_lines_are_kept_out_of_receipt_candidates(self):
        self._save_a()
        self._stage_a()
        preview = self.w.statement_adapters.preview("run-a")
        self.w.statement_adapters.confirm(
            "run-a", 1, preview["review_digest"], "finance"
        )
        debit_id = "portal:local-bank:TX-002"
        report = self.w.bank.candidates(debit_id)
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["excluded"], "debit_line_is_not_receipt")
        with self.assertRaises(BookError) as caught:
            self.w.bank.link(debit_id, "p", "op")
        self.assertEqual(caught.exception.code, "bank")
        self.assertEqual(self.w.db.rows("SELECT * FROM bank_links"), [])
        unmatched = {row["id"] for row in self.w.bank.unmatched()}
        self.assertNotIn(debit_id, unmatched)
        self.assertIn(debit_id, {row["id"] for row in self.w.bank.outgoing()})
        queue_ids = {
            item["id"]
            for item in self.w.queue.exceptions("2026-12-31")
            if item["type"] == "unmatched_bank"
        }
        self.assertNotIn(debit_id, queue_ids)

    def test_stale_review_digest_blocks_confirmation(self):
        self._save_a()
        self._stage_a()
        preview = self.w.statement_adapters.preview("run-a")
        with self.assertRaises(BookError) as caught:
            self.w.statement_adapters.confirm(
                "run-a", 1, "0" * 64, "finance"
            )
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(
            self.w.statement_adapters.get("run-a")["state"], "staged"
        )
        self.assertEqual(self.w.db.rows("SELECT * FROM bank_lines"), [])

    def test_changed_store_between_preview_and_confirm_forces_new_review(self):
        self._save_a()
        self._stage_a()
        preview = self.w.statement_adapters.preview("run-a")
        # Simulate the same business flow being booked by another channel
        # before finance confirms: the preview evidence is now stale.
        self.w.bank.add(
            {
                "id": "portal:local-bank:TX-001",
                "account": "local-bank",
                "currency": "CNY",
                "amount": "999.99",
                "booked": "2026-11-01",
                "reference": "OTHER",
                "payer": "someone",
                "direction": "credit",
            }
        )
        refreshed = self.w.statement_adapters.preview("run-a")
        self.assertNotEqual(
            refreshed["review_digest"], preview["review_digest"]
        )
        conflict = [
            item
            for item in refreshed["quarantine"]
            if item["ref"] == "TX-001" and item["code"] == "conflict"
        ]
        self.assertTrue(conflict)
        with self.assertRaises(BookError):
            self.w.statement_adapters.confirm(
                "run-a", 1, preview["review_digest"], "finance"
            )

    def test_adapter_change_after_stage_requires_new_review(self):
        self._save_a()
        self._stage_a()
        self.w.statement_adapters.preview("run-a")
        self.w.statement_adapters.save_adapter(
            "portal-a", CFG_A, expected_version=1
        )
        with self.assertRaises(BookError) as caught:
            self.w.statement_adapters.preview("run-a")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertIn("current_version", caught.exception.details)

    def test_quarantine_is_durable_and_blocks_booking(self):
        self.w.statement_adapters.save_adapter("portal-b", CFG_B)
        self.w.statement_adapters.stage(
            "run-b", "portal", "local-bank", "portal-b",
            content_base64=self._b64(FIXTURE_B),
        )
        preview = self.w.statement_adapters.preview("run-b")
        self.assertEqual(
            [line["tx_id"] for line in preview["lines"]], ["TX-001"]
        )
        self.assertTrue(preview["quarantine"])
        with self.assertRaises(BookError) as caught:
            self.w.statement_adapters.confirm(
                "run-b", 1, preview["review_digest"], "finance"
            )
        self.assertEqual(caught.exception.code, "quarantine")
        run = self.w.statement_adapters.get("run-b")
        self.assertEqual(run["state"], "staged")
        self.assertEqual(len(run["quarantine"]), len(preview["quarantine"]))
        self.assertIsNone(
            self.w.db.one("SELECT id FROM bank_lines WHERE id LIKE '%TX-003'")
        )

    def test_same_business_flow_is_booked_once_across_runs(self):
        self._save_a()
        self._stage_a("run-11")
        first = self.w.statement_adapters.preview("run-11")
        self.w.statement_adapters.confirm(
            "run-11", 1, first["review_digest"], "finance"
        )
        # Same channel/account statement delivered again under a new run id:
        # month and row position differ but business ids are identical.
        self._stage_a("run-12")
        second = self.w.statement_adapters.preview("run-12")
        self.assertTrue(all(item["status"] == "existing" for item in second["lines"]))
        again = self.w.statement_adapters.confirm(
            "run-12", 1, second["review_digest"], "finance"
        )
        self.assertEqual(again["result"]["inserted"], 0)
        self.assertEqual(again["result"]["existing"], 5)
        self.assertEqual(
            len(self.w.db.rows("SELECT id FROM bank_lines")), 5
        )

    def test_conflicting_replay_of_same_id_is_quarantined(self):
        self._save_a()
        self._stage_a("run-21")
        preview = self.w.statement_adapters.preview("run-21")
        self.w.statement_adapters.confirm(
            "run-21", 1, preview["review_digest"], "finance"
        )
        changed = FIXTURE_A.replace(b"1,234.50", b"1,235.50")
        self._stage_a("run-22", changed)
        again = self.w.statement_adapters.preview("run-22")
        conflict = [
            item
            for item in again["quarantine"]
            if item["ref"] == "TX-001" and item["code"] == "conflict"
        ]
        self.assertTrue(conflict)
        with self.assertRaises(BookError):
            self.w.statement_adapters.confirm(
                "run-22", 1, again["review_digest"], "finance"
            )
        # Original evidence stays untouched.
        self.assertEqual(self.w.bank.line("portal:local-bank:TX-001")["amount"], 123450)

    def test_reject_and_pending_queue(self):
        self._save_a()
        self._stage_a()
        rejected = self.w.statement_adapters.reject(
            "run-a", 1, "wrong month file", "finance"
        )
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(rejected["revision"], 2)
        self.assertEqual(self.w.statement_adapters.pending()["rows"], [])
        with self.assertRaises(BookError):
            self.w.statement_adapters.confirm("run-a", 2, "x", "finance")

    def test_stage_is_idempotent_and_identity_checked(self):
        self._save_a()
        first = self._stage_a()
        second = self._stage_a()
        self.assertEqual(first["revision"], second["revision"])
        with self.assertRaises(BookError) as caught:
            self.w.statement_adapters.stage(
                "run-a", "portal", "local-bank", "portal-a",
                content_base64=self._b64(FIXTURE_B),
            )
        self.assertEqual(caught.exception.code, "conflict")

    def test_text_and_base64_inputs_and_encoding_roundtrip(self):
        self._save_a()
        self.w.statement_adapters.stage(
            "run-text", "portal", "local-bank", "portal-a",
            content=FIXTURE_A.decode("utf-8"),
        )
        run = self.w.statement_adapters.get("run-text")
        self.assertEqual(run["encoding"], "utf-8")
        preview = self.w.statement_adapters.preview("run-text")
        self.assertEqual(len(preview["lines"]), 5)
        with self.assertRaises(BookError):
            self.w.statement_adapters.stage(
                "run-bad", "portal", "local-bank", "portal-a",
                content_base64="not-base64!!!",
            )

    def test_dispatch_exposes_adapter_actions(self):
        result = self.w.dispatch(
            {
                "action": "bank_adapter.save_adapter",
                "arguments": {"adapter_id": "p", "config": CFG_A},
            }
        )
        self.assertEqual(result["version"], 1)
        self.w.dispatch(
            {
                "action": "bank_statement.stage",
                "arguments": {
                    "run_id": "r",
                    "channel": "portal",
                    "account": "local-bank",
                    "adapter_id": "p",
                    "content": FIXTURE_A.decode("utf-8"),
                },
            }
        )
        preview = self.w.dispatch(
            {"action": "bank_statement.preview", "arguments": {"run_id": "r"}}
        )
        self.assertEqual(len(preview["lines"]), 5)
        confirmed = self.w.dispatch(
            {
                "action": "bank_statement.confirm",
                "arguments": {
                    "run_id": "r",
                    "expected_revision": 1,
                    "review_digest": preview["review_digest"],
                    "reviewer": "finance",
                },
            }
        )
        self.assertEqual(confirmed["state"], "confirmed")

    def test_legacy_bank_lines_default_to_credit(self):
        with tempfile.TemporaryDirectory() as folder:
            import sqlite3

            path = Path(folder) / "legacy.sqlite3"
            con = sqlite3.connect(path)
            con.executescript(
                """
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
INSERT INTO meta VALUES('version','1');
CREATE TABLE bank_lines(id TEXT PRIMARY KEY,account TEXT NOT NULL,currency TEXT NOT NULL,
 amount INTEGER NOT NULL,booked TEXT NOT NULL,reference TEXT NOT NULL,payer TEXT NOT NULL,
 payload TEXT NOT NULL,digest TEXT NOT NULL);
"""
            )
            payload = {
                "id": "b", "account": "cash", "currency": "CNY", "amount": "60.00",
                "booked": "2026-01-11", "reference": "R", "payer": "A Ltd",
            }
            con.execute(
                "INSERT INTO bank_lines VALUES(?,?,?,?,?,?,?,?,?)",
                ("b", "cash", "CNY", 6000, "2026-01-11", "R", "A Ltd",
                 canonical(payload), digest(payload)),
            )
            con.commit()
            con.close()
            with Workbench(path) as reopened:
                self.assertEqual(reopened.bank.line("b")["direction"], "credit")
                replay = reopened.bank.add(dict(payload))
                self.assertTrue(replay["existing"])
                self.assertEqual(replay["direction"], "credit")
                self.assertTrue(reopened.controls.require_valid()["ok"])


class ReconcileShapeCase(unittest.TestCase):
    def setUp(self):
        self.w = Workbench()
        self.addCleanup(self.w.close)

    def test_reconcile_separates_inflow_and_outflow(self):
        for ident, direction, amount in [
            ("in1", "credit", "100.00"),
            ("in2", "credit", "50.00"),
            ("out1", "debit", "20.00"),
        ]:
            self.w.bank.add(
                {
                    "id": ident,
                    "account": "cash",
                    "currency": "CNY",
                    "amount": amount,
                    "booked": "2026-11-10",
                    "reference": "R",
                    "payer": "P",
                    "direction": direction,
                }
            )
        report = self.w.bank.reconcile(
            "cash", "2026-11-01", "2026-12-01"
        )
        cny = report["currencies"]["CNY"]
        self.assertEqual(cny["inflow"], "150.00")
        self.assertEqual(cny["outflow"], "20.00")
        self.assertEqual(cny["total"], "150.00")  # legacy inbound-only alias
        self.assertEqual(cny["unmatched"], "150.00")
        self.assertEqual(cny["debit_lines"], 1)
        self.assertEqual(cny["lines"], 3)


if __name__ == "__main__":
    unittest.main()

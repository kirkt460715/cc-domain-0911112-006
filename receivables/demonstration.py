from tempfile import TemporaryDirectory
from pathlib import Path
from .service import Workbench
from .primitives import BookError


def run():
    with TemporaryDirectory() as folder:
        with Workbench(Path(folder) / "book.sqlite3") as work:
            work.ledger.register_party("factory", "合成仪器厂", ["Instrument Factory"])
            for ident, kind, amount, due in [
                ("inv1", "invoice", "120.50", "2026-02-01"),
                ("inv2", "invoice", "80.00", "2026-02-10"),
                ("pay", "payment", "150.00", None),
            ]:
                row = {
                    "id": ident,
                    "kind": kind,
                    "party": "factory",
                    "currency": "CNY",
                    "amount": amount,
                    "issued": "2026-01-10",
                    "reference": "LOCAL-001",
                }
                if due:
                    row["due"] = due
                work.ledger.post(row)
            plan = work.matching.propose("plan", "pay", "2026-02-11", "oldest_due")
            assert len(plan["allocations"]) == 2
            work.matching.confirm("plan", "reviewer")
            assert work.ledger.balance("inv2") == 5050
            work.disputes.open("dispute", "inv2", "10.00", "2026-02-12", "合成质量争议")
            statement = work.statements.freeze("statement", "factory", "2026-02-28")
            assert statement["aging"]["groups"][0]["collectible_minor"] == 4050
            work.bank.add(
                {
                    "id": "bank1",
                    "account": "cash",
                    "currency": "CNY",
                    "amount": "150.00",
                    "booked": "2026-01-11",
                    "reference": "LOCAL-001",
                    "payer": "Instrument Factory",
                }
            )
            work.bank.link("bank1", "pay", "reviewer")
            statement_html = (
                '<html><meta charset="utf-8"><table id="statement">'
                '<tr><th rowspan="2">流水号</th><th rowspan="2">日期</th>'
                '<th colspan="2">发生额</th><th rowspan="2">币种</th></tr>'
                '<tr><th>借方</th><th>贷方</th></tr>'
                '<tr><td>WEB-1</td><td>2026-01-11</td><td></td>'
                '<td>150.00</td><td>CNY</td></tr>'
                '<tr class="memo"><td colspan="5">付款人：Instrument Factory；附言：LOCAL-001</td></tr>'
                '<tr><td>WEB-2</td><td>2026-01-12</td><td>8.00</td>'
                '<td></td><td>CNY</td></tr>'
                '<tr class="total"><td colspan="3">合计</td><td>142.00</td><td>CNY</td></tr>'
                "</table></html>"
            )
            work.statement_adapters.save_adapter(
                "portal",
                {
                    "table_id": "statement",
                    "mapping": {
                        "id": "流水号",
                        "booked": "日期",
                        "debit": "发生额 / 借方",
                        "credit": "发生额 / 贷方",
                        "currency": "币种",
                    },
                    "date_formats": ["%Y-%m-%d"],
                    "skip_classes": ["total"],
                    "skip_patterns": [r"期初余额", r"分页"],
                    "memo": {
                        "continuation_classes": ["memo"],
                        "patterns": [
                            {"field": "payer", "pattern": r"付款人：(?P<value>[^；]+)"},
                            {"field": "reference", "pattern": r"附言：(?P<value>[^；\s]+)"},
                        ],
                    },
                },
            )
            work.statement_adapters.stage(
                "web-jan", "portal", "cash", "portal", content=statement_html
            )
            web_preview = work.statement_adapters.preview("web-jan")
            assert not web_preview["quarantine"]
            assert [line["direction"] for line in web_preview["lines"]] == [
                "credit",
                "debit",
            ]
            confirmed = work.statement_adapters.confirm(
                "web-jan", 1, web_preview["review_digest"], "reviewer"
            )
            assert confirmed["result"]["inserted"] == 2
            inbound = work.bank.line("portal:cash:WEB-1")
            assert inbound["reference"] == "LOCAL-001"
            assert work.bank.candidates("portal:cash:WEB-2")["candidates"] == []
            # Re-delivering the same statement never books the flow twice.
            work.statement_adapters.stage(
                "web-jan-copy", "portal", "cash", "portal", content=statement_html
            )
            replay_preview = work.statement_adapters.preview("web-jan-copy")
            assert all(line["status"] == "existing" for line in replay_preview["lines"])
            work.statement_adapters.confirm(
                "web-jan-copy", 1, replay_preview["review_digest"], "reviewer"
            )
            work.periods.create("feb", "2026-02-01", "2026-03-01")
            preview = work.periods.preview("feb")
            work.periods.close("feb", preview["digest"], "controller")
            try:
                work.ledger.reverse_allocation(
                    "invalid", "plan:0", "2026-02-20", "late correction"
                )
            except BookError as exc:
                assert exc.code == "period"
            else:
                raise AssertionError("closed-period write accepted")
            work.ledger.reverse_allocation(
                "reverse", "plan:0", "2026-03-01", "valid subsequent correction"
            )
            assert work.periods.verify("feb")["verified"]
            work.statements.export("statement", Path(folder) / "statement.json")
            assert work.statements.verify(Path(folder) / "statement.json")["verified"]
            assert work.controls.require_valid()["ok"]
            return {
                "status": "ok",
                "historical_open_minor": 5050,
                "current_open_minor": work.ledger.balance("inv1")
                + work.ledger.balance("inv2"),
                "closed_period_verified": True,
                "offline": True,
            }

"""Versionable HTML statement adaptation.

The module is deliberately free of database dependencies: it turns an
authorized HTML response into normalized bank evidence rows plus structured
failures that carry the original table position.  Only the Python standard
library is used so the adapter runs in the offline runtime.
"""

import re
from html.parser import HTMLParser
from datetime import datetime

from ..primitives import currency, money, display, day, identifier, BookError

SUPPORTED_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%Y%m%d",
    "%Y年%m月%d日",
)

REQUIRED_MAPPING = ("id", "booked", "currency", "debit", "credit")
OPTIONAL_MAPPING = ("payer", "reference", "memo")
KNOWN_FIELDS = REQUIRED_MAPPING + OPTIONAL_MAPPING

_CHARSET_PATTERN = re.compile(
    rb"""charset\s*=\s*["']?\s*([A-Za-z0-9][A-Za-z0-9_.\-]{0,40})""",
    re.IGNORECASE,
)
_AMOUNT_STRIP = " 　 \t\r\n"
_PLAIN_AMOUNT = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_GROUPED_AMOUNT = re.compile(r"^[+-]?\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?$")


class AdapterConfigError(BookError):
    pass


def validate_config(config):
    """Structural validation of an adapter configuration.

    Label resolution against a concrete table happens during parsing so the
    same versioned adapter can be saved before a sample arrives.
    """
    if not isinstance(config, dict):
        raise AdapterConfigError("input", "adapter config must be an object")
    unknown = set(config) - {
        "table_id",
        "mapping",
        "date_formats",
        "skip_classes",
        "skip_patterns",
        "memo",
    }
    if unknown:
        raise AdapterConfigError(
            "input", "unknown adapter config fields", fields=sorted(unknown)
        )
    mapping = config.get("mapping")
    if not isinstance(mapping, dict) or not set(REQUIRED_MAPPING) <= set(mapping):
        raise AdapterConfigError(
            "input", "mapping requires id/booked/currency/debit/credit labels"
        )
    if set(mapping) - set(KNOWN_FIELDS):
        raise AdapterConfigError("input", "unknown mapping field")
    labels = list(mapping.values())
    if not all(isinstance(label, str) and label.strip() for label in labels):
        raise AdapterConfigError("input", "mapping labels must be nonempty text")
    if len(set(labels)) != len(labels):
        raise AdapterConfigError("input", "mapping labels must be unique")
    formats = config.get("date_formats", ["%Y-%m-%d"])
    if (
        not isinstance(formats, list)
        or not formats
        or any(fmt not in SUPPORTED_DATE_FORMATS for fmt in formats)
    ):
        raise AdapterConfigError("input", "unsupported date format list")
    for key in ("skip_classes",):
        values = config.get(key, [])
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise AdapterConfigError("input", "skip classes must be nonempty strings")
    patterns = config.get("skip_patterns", [])
    if not isinstance(patterns, list):
        raise AdapterConfigError("input", "skip patterns must be a list")
    for pattern in patterns:
        _compile_pattern(pattern)
    memo = config.get("memo", {})
    if memo is None:
        memo = {}
    if not isinstance(memo, dict):
        raise AdapterConfigError("input", "memo config must be an object")
    if set(memo) - {
        "continuation_classes",
        "continuation_full_width",
        "patterns",
    }:
        raise AdapterConfigError("input", "unknown memo config fields")
    if not all(
        isinstance(value, str) and value.strip()
        for value in memo.get("continuation_classes", [])
    ):
        raise AdapterConfigError(
            "input", "memo continuation classes must be nonempty strings"
        )
    if not isinstance(memo.get("continuation_full_width", True), bool):
        raise AdapterConfigError("input", "continuation_full_width must be boolean")
    extractors = memo.get("patterns", [])
    if not isinstance(extractors, list):
        raise AdapterConfigError("input", "memo patterns must be a list")
    for extractor in extractors:
        if (
            not isinstance(extractor, dict)
            or extractor.get("field") not in ("payer", "reference")
            or not isinstance(extractor.get("pattern"), str)
        ):
            raise AdapterConfigError(
                "input", "memo pattern needs field payer/reference and regex pattern"
            )
        compiled = _compile_pattern(extractor["pattern"])
        if "value" not in compiled.groupindex:
            raise AdapterConfigError(
                "input", "memo pattern must capture a group named value"
            )
    table_id = config.get("table_id")
    if table_id is not None and (
        not isinstance(table_id, str) or not table_id.strip()
    ):
        raise AdapterConfigError("input", "table id must be nonempty text")
    return {
        "table_id": table_id,
        "mapping": dict(mapping),
        "date_formats": formats,
        "skip_classes": list(config.get("skip_classes", [])),
        "skip_patterns": list(patterns),
        "memo": {
            "continuation_classes": list(memo.get("continuation_classes", [])),
            "continuation_full_width": memo.get("continuation_full_width", True),
            "patterns": list(extractors),
        },
    }


def _compile_pattern(pattern):
    if not isinstance(pattern, str) or not pattern.strip():
        raise AdapterConfigError("input", "regex pattern must be nonempty text")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise AdapterConfigError(
            "input", "invalid regular expression", pattern=pattern, reason=str(exc)
        )


def decode_source(content):
    """Decode authorized HTML bytes using BOM/meta charset declarations."""
    if isinstance(content, str):
        return content, "utf-8"
    if not isinstance(content, bytes):
        error("input", "HTML source must be text or bytes")
    if content.startswith(b"\xef\xbb\xbf"):
        return content[3:].decode("utf-8"), "utf-8-sig"
    if content.startswith(b"\xff\xfe") or content.startswith(b"\xfe\xff"):
        return content.decode("utf-16"), "utf-16"
    declared = _CHARSET_PATTERN.search(content[:8192])
    codec = declared.group(1).decode("ascii").lower() if declared else "utf-8"
    try:
        return content.decode(codec), codec
    except (LookupError, UnicodeDecodeError):
        try:
            return content.decode("utf-8"), "utf-8"
        except UnicodeDecodeError as exc:
            error("encoding", "HTML source encoding cannot be decoded", reason=str(exc))


class _Cell:
    __slots__ = ("text", "rowspan", "colspan", "tag", "classes", "row_classes",
                 "row", "line", "ordinal")

    def __init__(self, tag, classes, row_classes, row, line, ordinal):
        self.text = ""
        self.rowspan = 1
        self.colspan = 1
        self.tag = tag
        self.classes = classes
        self.row_classes = row_classes
        self.row = row
        self.line = line
        self.ordinal = ordinal


class _TableCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.table_ids = []
        self._depth = 0
        self._current = None
        self._row = None
        self._row_classes = []
        self._cell = None
        self._row_index = -1
        self._cell_ordinal = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._current = []
                self.tables.append(self._current)
                self.table_ids.append(attributes.get("id"))
                self._row_index = -1
            else:
                # Nested tables are not statement data; suspend capture.
                self._row = None
                self._cell = None
        elif tag == "tr" and self._current is not None and self._depth == 1:
            self._row = []
            self._row_classes = dict(attrs).get("class", "").split()
            self._current.append(self._row)
            self._row_index += 1
            self._cell_ordinal = 0
        elif tag in ("td", "th") and self._row is not None and self._depth == 1:
            classes = attributes.get("class", "").split()
            self._cell = _Cell(
                tag, classes, list(self._row_classes),
                self._row_index, self.getpos()[0], self._cell_ordinal
            )
            self._cell.rowspan = _span(attributes.get("rowspan"), self._cell)
            self._cell.colspan = _span(attributes.get("colspan"), self._cell)
            self._row.append(self._cell)
            self._cell_ordinal += 1
        elif tag == "br" and self._cell is not None:
            self._cell.text += " "

    def handle_endtag(self, tag):
        if tag == "table" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                self._current = None
            self._row = None
            self._cell = None
        elif tag == "tr" and self._depth == 1:
            self._row = None
            self._cell = None
        elif tag in ("td", "th") and self._depth == 1:
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.text += data


def _span(value, cell):
    if value is None:
        return 1
    try:
        span = int(value.strip())
    except (AttributeError, ValueError):
        raise AdapterConfigError(
            "input",
            "rowspan/colspan must be an integer",
            location={"table_row": cell.row + 1, "source_line": cell.line},
        )
    if not 1 <= span <= 100:
        raise AdapterConfigError(
            "input",
            "rowspan/colspan outside supported range",
            location={"table_row": cell.row + 1, "source_line": cell.line},
        )
    return span


def _collect_table(text):
    collector = _TableCollector()
    try:
        collector.feed(text)
        collector.close()
    except AdapterConfigError:
        raise
    except Exception as exc:  # HTMLParser raises concrete subclasses on bad markup
        raise AdapterConfigError("input", "malformed HTML table", reason=str(exc))
    if not collector.tables:
        raise AdapterConfigError("input", "HTML contains no table")
    return collector.tables, collector.table_ids


def expand_grid(raw_rows):
    """Expand rowspan/colspan into a rectangular grid sharing cell objects."""
    grid = []
    occupied = {}
    for raw in raw_rows:
        if not raw:
            continue
        ordered = []
        col = 0
        for cell in raw:
            while (len(grid), col) in occupied:
                ordered.append(occupied[(len(grid), col)])
                col += 1
            for row_offset in range(cell.rowspan):
                for col_offset in range(cell.colspan):
                    occupied[(len(grid) + row_offset, col + col_offset)] = cell
            for _ in range(cell.colspan):
                ordered.append(cell)
            col += cell.colspan
        while (len(grid), col) in occupied:
            ordered.append(occupied[(len(grid), col)])
            col += 1
        if ordered:
            grid.append(ordered)
    width = max((len(row) for row in grid), default=0)
    for row in grid:
        row.extend([None] * (width - len(row)))
    return grid


def _norm_label(value):
    return re.sub(r"\s+", " ", value or "").strip()


def build_columns(grid):
    """Return column descriptors and the index of the first data row."""
    header_rows = []
    first_data = 0
    for index, row in enumerate(grid):
        cells = [cell for cell in row if cell is not None]
        if cells and all(cell.tag == "th" for cell in cells):
            header_rows.append(index)
            first_data = index + 1
        else:
            break
    if not header_rows:
        raise AdapterConfigError("input", "statement table has no header row")
    width = len(grid[0])
    columns = []
    for col in range(width):
        path = []
        for header_index in header_rows:
            cell = grid[header_index][col]
            if cell is not None:
                label = _norm_label(cell.text)
                if label and (not path or path[-1] != label):
                    path.append(label)
        leaf = path[-1] if path else ""
        columns.append(
            {
                "index": col,
                "leaf": leaf,
                "path": " / ".join(path),
                "header_line": grid[header_rows[-1]][col].line
                if grid[header_rows[-1]][col] is not None
                else grid[header_rows[-1]][0].line,
            }
        )
    return columns, first_data, set(header_rows)


def _resolve_columns(columns, mapping):
    resolved = {}
    failures = []
    for field, label in mapping.items():
        wanted = _norm_label(label)
        exact_path = [c for c in columns if c["path"] == wanted]
        exact_leaf = [c for c in columns if c["leaf"] == wanted]
        if len(exact_path) == 1:
            resolved[field] = exact_path[0]["index"]
        elif len(exact_leaf) == 1:
            resolved[field] = exact_leaf[0]["index"]
        elif len(exact_leaf) > 1:
            failures.append(
                {
                    "ref": None,
                    "code": "adapter",
                    "message": "duplicate header label, use the full header path",
                    "location": {
                        "table_row": 1,
                        "source_line": exact_leaf[0]["header_line"],
                        "cell": exact_leaf[0]["index"] + 1,
                    },
                    "details": {"field": field, "label": wanted},
                }
            )
        else:
            failures.append(
                {
                    "ref": None,
                    "code": "adapter",
                    "message": "mapped header label not found",
                    "location": {
                        "table_row": 1,
                        "source_line": columns[0]["header_line"],
                        "cell": 1,
                    },
                    "details": {"field": field, "label": wanted},
                }
            )
    return resolved, failures


def _cell_text(row, col):
    cell = row[col] if col < len(row) else None
    if cell is None:
        return "", None
    return _norm_label(cell.text), cell


def parse_amount(text):
    """Return (signed decimal text, negative flag) honoring grouping/brackets."""
    cleaned = text.translate({ord(ch): None for ch in _AMOUNT_STRIP})
    if not cleaned:
        return None, False
    negative = False
    if len(cleaned) >= 2 and cleaned.startswith("(") and cleaned.endswith(")"):
        negative = True
        cleaned = cleaned[1:-1]
    if cleaned.startswith("-"):
        negative = not negative
        cleaned = cleaned[1:]
    elif cleaned.startswith("+"):
        cleaned = cleaned[1:]
    cleaned = cleaned.replace(",", "").replace("，", "")
    if not _PLAIN_AMOUNT.match(cleaned):
        raise ValueError("not a numeric amount")
    if negative:
        cleaned = "-" + cleaned
    return cleaned, negative


def _parse_date(text, formats):
    candidates = set()
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.strftime(fmt) != text:
            continue
        candidates.add(parsed.date().isoformat())
    if not candidates:
        return None
    if len(candidates) > 1:
        return "ambiguous"
    value = candidates.pop()
    try:
        day(value)
    except BookError:
        return None
    return value


def _is_header_row(row):
    cells = []
    seen = set()
    for cell in row:
        if cell is not None and id(cell) not in seen:
            seen.add(id(cell))
            cells.append(cell)
    return bool(cells) and all(cell.tag == "th" for cell in cells)


def _unique_cells(row):
    cells = []
    seen = set()
    for cell in row:
        if cell is not None and id(cell) not in seen:
            seen.add(id(cell))
            cells.append(cell)
    return cells


def _row_classes(row):
    classes = set()
    for cell in row:
        if cell is not None:
            classes.update(cell.classes)
            classes.update(cell.row_classes)
    return classes


def _row_text(row):
    parts = []
    seen = set()
    for cell in row:
        if cell is not None and id(cell) not in seen:
            seen.add(id(cell))
            text = _norm_label(cell.text)
            if text:
                parts.append(text)
    return " ".join(parts)


def _extract_memo_fields(target, memo_text, memo_cfg):
    for extractor in memo_cfg["patterns"]:
        match = re.search(extractor["pattern"], memo_text)
        if not match:
            continue
        value = _norm_label(match.group("value"))
        field = extractor["field"]
        if not target.get(field):
            target[field] = value


def parse_statement(text, config):
    """Parse decoded HTML into evidence lines, failures and skip audit data."""
    config = validate_config(config)
    tables, table_ids = _collect_table(text)
    target = 0
    if config["table_id"] is not None:
        try:
            target = table_ids.index(config["table_id"])
        except ValueError:
            raise AdapterConfigError(
                "input", "configured table id not found", table=config["table_id"]
            )
    grid = expand_grid(tables[target])
    if len(grid) > 10000 or sum(len(row) for row in grid) > 100000:
        raise AdapterConfigError("capacity", "statement table exceeds supported size")
    columns, first_data, _ = build_columns(grid)
    resolved, failures = _resolve_columns(columns, config["mapping"])
    if failures:
        return [], failures, {"skipped": [], "duplicates": []}

    skip_res = [re.compile(pattern) for pattern in config["skip_patterns"]]
    memo_cfg = config["memo"]
    lines = []
    skipped = []
    duplicates = []
    seen = {}
    # Memo continuation rows follow the transaction they describe.
    current_line = None
    last_row_failed = False

    def location(cell, row):
        if cell is None:
            cell = next((item for item in row if item is not None), None)
        return {
            "table_row": cell.row + 1 if cell else None,
            "source_line": cell.line if cell else None,
            "cell": cell.ordinal + 1 if cell else None,
        }

    for row_index in range(first_data, len(grid)):
        row = grid[row_index]
        cells = _unique_cells(row)
        if not cells:
            continue
        if _is_header_row(row):
            skipped.append(
                {"reason": "repeated_header", "text": _row_text(row),
                 "location": location(cells[0], row)}
            )
            continue
        classes = _row_classes(row)
        joined = _row_text(row)
        if classes.intersection(config["skip_classes"]) or any(
            pattern.search(joined) for pattern in skip_res
        ):
            skipped.append(
                {"reason": "excluded_row", "text": joined,
                 "location": location(cells[0], row)}
            )
            continue
        spanning_cell = cells[0]
        full_width = len(cells) == 1 and spanning_cell.colspan == len(
            [item for item in row if item is not None]
        )
        if classes.intersection(memo_cfg["continuation_classes"]) or (
            memo_cfg["continuation_full_width"] and full_width
        ):
            if not joined:
                skipped.append(
                    {"reason": "blank_spanning_row", "text": "",
                     "location": location(cells[0], row)}
                )
            elif current_line is not None and not last_row_failed:
                parts = [part for part in (current_line["memo"], joined) if part]
                current_line["memo"] = "；".join(parts)[:1024]
                _extract_memo_fields(current_line, joined, memo_cfg)
            else:
                failures.append(
                    {
                        "ref": None,
                        "code": "memo",
                        "message": "memo continuation row without a transaction",
                        "location": location(cells[0], row),
                    }
                )
            continue

        raw = {field: _cell_text(row, col) for field, col in resolved.items()}
        anchor = raw["id"][1] or cells[0]
        row_failures = []
        tx_id = raw["id"][0].strip()
        if not tx_id:
            row_failures.append(
                {
                    "ref": None,
                    "code": "missing_field",
                    "message": "transaction identifier is empty",
                    "location": location(anchor, row),
                    "details": {"field": "id"},
                }
            )

        def require(field):
            value, cell = raw[field]
            if not value:
                row_failures.append(
                    {
                        "ref": tx_id or None,
                        "code": "missing_field",
                        "message": "required field is empty",
                        "location": location(cell, row),
                        "details": {"field": field},
                    }
                )
            return value

        booked_text = require("booked")
        currency_text = require("currency")
        debit_text, debit_cell = raw["debit"]
        credit_text, credit_cell = raw["credit"]
        memo_parts = []
        if "memo" in resolved:
            memo_value, _ = raw["memo"]
            if memo_value:
                memo_parts.append(memo_value)
        memo_text = "；".join(part for part in memo_parts if part)

        booked = None
        if booked_text:
            booked = _parse_date(booked_text, config["date_formats"])
            if booked is None:
                row_failures.append(
                    {
                        "ref": tx_id or None,
                        "code": "date",
                        "message": "date cannot be parsed",
                        "location": location(raw["booked"][1], row),
                        "details": {"value": booked_text},
                    }
                )
            elif booked == "ambiguous":
                row_failures.append(
                    {
                        "ref": tx_id or None,
                        "code": "date",
                        "message": "date is ambiguous across configured formats",
                        "location": location(raw["booked"][1], row),
                        "details": {"value": booked_text},
                    }
                )
                booked = None

        code = None
        if currency_text:
            try:
                code = currency(currency_text.strip().upper())
            except BookError as exc:
                row_failures.append(
                    {
                        "ref": tx_id or None,
                        "code": "currency",
                        "message": str(exc),
                        "location": location(raw["currency"][1], row),
                        "details": {"value": currency_text},
                    }
                )

        direction = None
        amount_text = None
        amount_cell = None
        if debit_text and credit_text:
            row_failures.append(
                {
                    "ref": tx_id or None,
                    "code": "direction",
                    "message": "debit and credit columns are both populated",
                    "location": location(debit_cell, row),
                    "details": {"debit": debit_text, "credit": credit_text,
                                "credit_location": location(credit_cell, row)},
                }
            )
        elif not debit_text and not credit_text:
            row_failures.append(
                {
                    "ref": tx_id or None,
                    "code": "missing_field",
                    "message": "debit and credit columns are both empty",
                    "location": location(debit_cell or credit_cell or anchor, row),
                    "details": {"field": "amount"},
                }
            )
        else:
            nominal = "debit" if debit_text else "credit"
            amount_text = debit_text or credit_text
            amount_cell = debit_cell or credit_cell

        amount_minor = None
        if amount_text and code:
            try:
                signed, _ = parse_amount(amount_text)
            except ValueError:
                row_failures.append(
                    {
                        "ref": tx_id or None,
                        "code": "amount",
                        "message": "amount cannot be parsed",
                        "location": location(amount_cell, row),
                        "details": {"value": amount_text},
                    }
                )
            else:
                if signed is None:
                    row_failures.append(
                        {
                            "ref": tx_id or None,
                            "code": "missing_field",
                            "message": "amount is empty",
                            "location": location(amount_cell, row),
                            "details": {"field": "amount"},
                        }
                    )
                else:
                    negative = signed.startswith("-")
                    try:
                        amount_minor = money(signed.lstrip("-"), code)
                    except BookError as exc:
                        row_failures.append(
                            {
                                "ref": tx_id or None,
                                "code": exc.code,
                                "message": str(exc),
                                "location": location(amount_cell, row),
                                "details": {"value": amount_text, **exc.details},
                            }
                        )
                    else:
                        if amount_minor == 0:
                            row_failures.append(
                                {
                                    "ref": tx_id or None,
                                    "code": "amount",
                                    "message": "zero amount is not a transaction",
                                    "location": location(amount_cell, row),
                                    "details": {"value": amount_text},
                                }
                            )
                            amount_minor = None
                        else:
                            direction = "debit" if nominal == "debit" else "credit"
                            if negative:
                                # Bracketed/minus amounts invert the column meaning.
                                direction = (
                                    "credit" if direction == "debit" else "debit"
                                )

        payer = raw["payer"][0] if "payer" in resolved else ""
        reference = raw["reference"][0] if "reference" in resolved else ""
        if memo_text:
            placeholder = {"payer": payer, "reference": reference}
            _extract_memo_fields(placeholder, memo_text, memo_cfg)
            payer, reference = placeholder["payer"], placeholder["reference"]

        if row_failures:
            failures.extend(row_failures)
            last_row_failed = True
            continue

        line = {
            "tx_id": tx_id,
            "booked": booked,
            "currency": code,
            "amount": display(amount_minor, code),
            "direction": direction,
            "reference": reference[:512],
            "payer": payer[:512],
            "memo": memo_text[:1024],
            "location": location(anchor, row),
        }
        if tx_id in seen:
            previous = seen[tx_id]
            if _line_identity(previous) == _line_identity(line):
                duplicates.append(
                    {
                        "ref": tx_id,
                        "message": "duplicate transaction row ignored",
                        "first_location": previous["location"],
                        "location": line["location"],
                    }
                )
            else:
                last_row_failed = True
                failures.append(
                    {
                        "ref": tx_id,
                        "code": "conflict",
                        "message": "same transaction identifier with different content",
                        "location": line["location"],
                        "details": {
                            "first_location": previous["location"],
                            "current": _line_identity(line),
                            "previous": _line_identity(previous),
                        },
                    }
                )
                failures.append(
                    {
                        "ref": tx_id,
                        "code": "conflict",
                        "message": "same transaction identifier with different content",
                        "location": previous["location"],
                        "details": {"later_location": line["location"]},
                    }
                )
            continue
        seen[tx_id] = line
        lines.append(line)
        last_row_failed = False
        current_line = line

    return lines, failures, {"skipped": skipped, "duplicates": duplicates}


def _line_identity(line):
    return {
        key: line[key]
        for key in ("booked", "currency", "amount", "direction", "reference", "payer")
    }


def compose_line_id(channel, account, tx_id):
    ident = "{}:{}:{}".format(channel, account, tx_id)
    return identifier(ident)

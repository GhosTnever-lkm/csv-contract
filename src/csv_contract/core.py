"""Dependency-free CSV contracts and bounded streaming validation."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys


class InputError(ValueError):
    """The contract or source cannot be processed reliably."""


@dataclass(frozen=True)
class Limits:
    file_bytes: int = 100 * 1024 * 1024
    field_chars: int = 1024 * 1024
    line_chars: int = 8 * 1024 * 1024
    rows: int = 250_000
    findings: int = 1_000
    report_bytes: int = 2 * 1024 * 1024

    def __post_init__(self):
        if any(type(v) is not int or not 0 < v <= sys.maxsize for v in self.__dict__.values()):
            raise InputError("All limits must be positive integers within the platform size range.")


@dataclass(frozen=True)
class Column:
    kind: str
    required: bool = True
    nullable: bool = False
    nonempty: bool = False
    choices: tuple | None = None
    minimum: Decimal | None = None
    maximum: Decimal | None = None


@dataclass(frozen=True)
class Contract:
    columns: dict[str, Column]
    allow_extra_columns: bool
    unique: tuple[tuple[str, ...], ...]


KINDS = {"string", "integer", "decimal", "boolean", "date"}
INTEGER = re.compile(r"[+-]?[0-9]+\Z")
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise InputError("Numeric bounds must be finite JSON numbers.")
    result = Decimal(str(value))
    if not result.is_finite():
        raise InputError("Numeric bounds must be finite JSON numbers.")
    return result


def _enum_value(value, kind):
    if kind == "string" and isinstance(value, str):
        return value
    if kind == "integer" and type(value) is int:
        return value
    if kind == "decimal":
        return _number(value)
    if kind == "boolean" and type(value) is bool:
        return value
    if kind == "date" and isinstance(value, str) and DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise InputError("Enum entries must match the column type; null is controlled by nullable.")


def parse_contract(data) -> Contract:
    if not isinstance(data, dict):
        raise InputError("Contract root must be an object.")
    if set(data) - {"version", "columns", "allow_extra_columns", "unique"}:
        raise InputError("Unknown contract setting.")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise InputError("Contract version must be 1.")
    source = data.get("columns")
    if not isinstance(source, dict) or not 1 <= len(source) <= 256:
        raise InputError("columns must contain between 1 and 256 column definitions.")
    extra = data.get("allow_extra_columns", False)
    if type(extra) is not bool:
        raise InputError("allow_extra_columns must be a boolean.")
    columns = {}
    for name, rule in source.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 256:
            raise InputError("Column names must be nonblank strings of at most 256 characters.")
        if not isinstance(rule, dict) or set(rule) - {"type", "required", "nullable", "nonempty", "enum", "min", "max"}:
            raise InputError("Each column must be an object with recognized rule settings.")
        kind = rule.get("type")
        if not isinstance(kind, str) or kind not in KINDS:
            raise InputError("Column type must be string, integer, decimal, boolean, or date.")
        flags = {k: rule.get(k, default) for k, default in (("required", True), ("nullable", False), ("nonempty", False))}
        if any(type(v) is not bool for v in flags.values()):
            raise InputError("required, nullable, and nonempty must be booleans.")
        if flags["nonempty"] and kind != "string":
            raise InputError("nonempty is only supported for string columns.")
        minimum = _number(rule["min"]) if "min" in rule else None
        maximum = _number(rule["max"]) if "max" in rule else None
        if (minimum is not None or maximum is not None) and kind not in {"integer", "decimal"}:
            raise InputError("min and max are only supported for numeric columns.")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise InputError("min cannot exceed max.")
        choices = None
        if "enum" in rule:
            values = rule["enum"]
            if not isinstance(values, list) or not 1 <= len(values) <= 10_000:
                raise InputError("enum must contain between 1 and 10000 values.")
            choices = tuple(_enum_value(v, kind) for v in values)
            if len(set(choices)) != len(choices):
                raise InputError("enum must not contain duplicate values.")
        columns[name] = Column(kind, choices=choices, minimum=minimum, maximum=maximum, **flags)
    unique = data.get("unique", [])
    if not isinstance(unique, list) or len(unique) > 32:
        raise InputError("unique must be a list of at most 32 column lists.")
    keys = []
    for key in unique:
        if not isinstance(key, list) or not key or any(not isinstance(n, str) or n not in columns for n in key):
            raise InputError("Each unique key must be a nonempty list of defined column names.")
        if len(set(key)) != len(key) or tuple(key) in keys:
            raise InputError("unique must not repeat columns or key definitions.")
        keys.append(tuple(key))
    return Contract(columns, extra, tuple(keys))


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("Contract JSON contains a duplicate object key.")
        result[key] = value
    return result


def load_contract(path: Path) -> Contract:
    try:
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise InputError("Contract exceeds 1 MiB.")
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_json_object, parse_float=Decimal,
                          parse_constant=lambda _: (_ for _ in ()).throw(InputError("Non-finite JSON numbers are unsupported.")))
        return parse_contract(data)
    except (OSError, UnicodeError) as exc:
        raise InputError("Cannot read the contract as UTF-8.") from exc
    except (json.JSONDecodeError, RecursionError, InvalidOperation) as exc:
        raise InputError("Contract JSON is invalid or too deeply nested.") from exc


def _value(cell: str, column: Column):
    if cell == "":
        if column.nullable:
            return None, None
        return None, "EMPTY_VALUE"
    if column.kind == "string":
        if column.nonempty and not cell.strip():
            return None, "BLANK_VALUE"
        return cell, None
    text = cell.strip()
    try:
        if column.kind == "integer" and INTEGER.fullmatch(text):
            return int(text), None
        if column.kind == "decimal" and NUMBER.fullmatch(text):
            result = Decimal(text)
            if result.is_finite():
                return result, None
        if column.kind == "boolean" and text.lower() in {"true", "false"}:
            return text.lower() == "true", None
        if column.kind == "date" and DATE.fullmatch(text):
            return date.fromisoformat(text), None
    except (ValueError, InvalidOperation, OverflowError):
        pass
    return None, "TYPE_MISMATCH"


def _checked_lines(stream, limits, has_bom, delimiter):
    total = 3 if has_bom else 0
    state = "start"
    while True:
        line = stream.readline(limits.line_chars + 1)
        if not line:
            break
        if len(line) > limits.line_chars:
            raise InputError("CSV physical line exceeds its character limit.")
        total += len(line.encode("utf-8"))
        if total > limits.file_bytes:
            raise InputError("CSV exceeds its byte limit.")
        for char in line:
            if state == "quoted":
                if char == '"':
                    state = "closed"
            elif state == "closed":
                if char == '"':
                    state = "quoted"
                elif char == delimiter or char in "\r\n":
                    state = "start"
                else:
                    raise InputError("CSV quoting is invalid after a closing quote.")
            elif char == '"':
                if state != "start":
                    raise InputError("CSV contains a quote inside an unquoted field.")
                state = "quoted"
            elif char == delimiter or char in "\r\n":
                state = "start"
            else:
                state = "unquoted"
        yield line
    if state == "quoted":
        raise InputError("CSV has an unterminated quoted field.")


MESSAGES = {
    "BLANK_HEADER": "Column header is blank.", "DUPLICATE_HEADER": "Column header is repeated.",
    "MISSING_COLUMN": "A required column is absent.", "EXTRA_COLUMN": "Column is not defined by the contract.",
    "ROW_WIDTH": "Record has a different number of fields than the header.",
    "EMPTY_VALUE": "An empty cell is not allowed.", "BLANK_VALUE": "A nonblank string is required.",
    "TYPE_MISMATCH": "Cell does not match the declared type.", "ENUM_MISMATCH": "Cell is not in the declared enum.",
    "BELOW_MIN": "Cell is below the declared minimum.", "ABOVE_MAX": "Cell exceeds the declared maximum.",
    "DUPLICATE_KEY": "Unique key duplicates an earlier record.",
}


def validate_csv(path: Path, contract: Contract, *, delimiter=",", limits=Limits()) -> dict:
    if delimiter not in {",", ";", "\t", "|"}:
        raise InputError("Unsupported delimiter.")
    findings, count, kept_bytes = [], 0, 512
    def add(code, row, column=None):
        nonlocal count, kept_bytes
        count += 1
        finding = {"code": code, "row": row, "column": column, "message": MESSAGES[code]}
        size = len(json.dumps(finding, ensure_ascii=True).encode("utf-8")) + 2
        if len(findings) < limits.findings and kept_bytes + size <= limits.report_bytes:
            findings.append(finding)
            kept_bytes += size
    old_limit = csv.field_size_limit()
    try:
        if path.stat().st_size > limits.file_bytes:
            raise InputError("CSV exceeds its byte limit.")
        with path.open("rb") as probe:
            has_bom = probe.read(3) == b"\xef\xbb\xbf"
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            csv.field_size_limit(limits.field_chars)
            reader = csv.reader(_checked_lines(stream, limits, has_bom, delimiter), delimiter=delimiter, strict=True)
            header = next(reader, None)
            if header is None:
                raise InputError("CSV has no header record.")
            if len(header) > 256:
                raise InputError("CSV header exceeds 256 columns.")
            if any(len(name) > 256 for name in header):
                raise InputError("CSV column name exceeds 256 characters.")
            seen = set()
            for name in header:
                if not name.strip():
                    add("BLANK_HEADER", 1, name)
                if name in seen:
                    add("DUPLICATE_HEADER", 1, name)
                seen.add(name)
            for name, rule in contract.columns.items():
                if rule.required and name not in seen:
                    add("MISSING_COLUMN", 1, name)
            if not contract.allow_extra_columns:
                for name in header:
                    if name not in contract.columns:
                        add("EXTRA_COLUMN", 1, name)
            usable = len(header) == len(seen) and all(n.strip() for n in header)
            tracked = [set() for _ in contract.unique]
            rows = 0
            for record in reader:
                rows += 1
                if rows > limits.rows:
                    raise InputError("CSV exceeds its record limit.")
                row = rows + 1
                if len(record) != len(header):
                    add("ROW_WIDTH", row)
                    continue
                if not usable:
                    continue
                parsed = {}
                for name, cell in zip(header, record):
                    column = contract.columns.get(name)
                    if column is None:
                        continue
                    value, error = _value(cell, column)
                    if error:
                        add(error, row, name)
                        continue
                    parsed[name] = value
                    if value is None:
                        continue
                    if column.choices is not None and value not in column.choices:
                        add("ENUM_MISMATCH", row, name)
                    if column.minimum is not None and value < column.minimum:
                        add("BELOW_MIN", row, name)
                    if column.maximum is not None and value > column.maximum:
                        add("ABOVE_MAX", row, name)
                for i, key in enumerate(contract.unique):
                    if any(name not in parsed or parsed[name] is None for name in key):
                        continue
                    values = tuple(parsed[name] for name in key)
                    if values in tracked[i]:
                        add("DUPLICATE_KEY", row, list(key))
                    else:
                        tracked[i].add(values)
        return {"schema_version": 1, "valid": count == 0, "records": rows,
                "finding_count": count, "truncated": count > len(findings), "findings": findings}
    except (OSError, UnicodeError) as exc:
        raise InputError("Cannot read CSV input as UTF-8.") from exc
    except csv.Error as exc:
        raise InputError("CSV syntax or field-size limit is invalid.") from exc
    finally:
        csv.field_size_limit(old_limit)

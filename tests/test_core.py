from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from csv_contract.core import InputError, Limits, load_contract, parse_contract, validate_csv


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "data.csv"

    def validate(self, text, columns=None, **kwargs):
        self.path.write_text(text, encoding="utf-8", newline="")
        contract = parse_contract({"version": 1, "columns": columns or {"id": {"type": "integer"}}, **kwargs.pop("schema", {})})
        return validate_csv(self.path, contract, **kwargs)

    def test_valid_types_and_ranges(self):
        rules = {"id": {"type": "integer", "min": 1}, "p": {"type": "decimal", "min": 0, "max": 10},
                 "b": {"type": "boolean"}, "d": {"type": "date"}, "s": {"type": "string", "enum": ["ok"]}}
        report = self.validate("id,p,b,d,s\n+2,1.20,TRUE,2024-02-29,ok\n", rules)
        self.assertTrue(report["valid"])
        self.assertEqual(report["records"], 1)

    def test_type_errors_do_not_report_values(self):
        report = self.validate("id\nSECRET_VALUE_9038\n")
        self.assertEqual(report["findings"][0]["code"], "TYPE_MISMATCH")
        self.assertNotIn("SECRET_VALUE_9038", json.dumps(report))

    def test_optional_headers_and_extra_column_policy(self):
        self.assertTrue(self.validate("id,extra\n1,ok\n", {"id": {"type": "integer"}, "optional": {"type": "date", "required": False}}, schema={"allow_extra_columns": True})["valid"])
        codes = [f["code"] for f in self.validate("other\nx\n")["findings"]]
        self.assertEqual(codes, ["MISSING_COLUMN", "EXTRA_COLUMN"])

    def test_empty_nullable_and_whitespace_strings(self):
        rules = {"id": {"type": "string", "nullable": True, "nonempty": True}}
        self.assertTrue(self.validate('id\n""\n', rules)["valid"])
        self.assertEqual(self.validate("id\n   \n", rules)["findings"][0]["code"], "BLANK_VALUE")
        self.assertEqual(self.validate('id\n""\n')["findings"][0]["code"], "EMPTY_VALUE")

    def test_numeric_bounds_and_enum(self):
        report = self.validate("id\n0\n10\n2\n", {"id": {"type": "integer", "min": 1, "max": 5, "enum": [1, 2, 3]}})
        self.assertEqual([f["code"] for f in report["findings"]], ["ENUM_MISMATCH", "BELOW_MIN", "ENUM_MISMATCH", "ABOVE_MAX"])

    def test_invalid_dates_and_nonfinite_numbers(self):
        rules = {"id": {"type": "date"}, "p": {"type": "decimal"}, "b": {"type": "boolean"}}
        report = self.validate("id,p,b\n2026-02-30,NaN,1\n20261031,Infinity,yes\n", rules)
        self.assertEqual(report["finding_count"], 6)

    def test_single_and_composite_unique_keys(self):
        report = self.validate("id,b\n1,x\n1,y\n1,x\n", {"id": {"type": "integer"}, "b": {"type": "string"}}, schema={"unique": [["id"], ["id", "b"]]})
        self.assertEqual([f["row"] for f in report["findings"]], [3, 4, 4])
        self.assertEqual(report["findings"][-1]["column"], ["id", "b"])

    def test_null_or_invalid_keys_do_not_create_duplicate_findings(self):
        report = self.validate('id\n""\n""\nx\nx\n', {"id": {"type": "integer", "nullable": True}}, schema={"unique": [["id"]]})
        self.assertEqual([f["code"] for f in report["findings"]], ["TYPE_MISMATCH", "TYPE_MISMATCH"])

    def test_duplicate_blank_headers_are_reported_without_ambiguous_cells(self):
        report = self.validate("id,id, \n1,2,3\n")
        self.assertEqual([f["code"] for f in report["findings"]], ["DUPLICATE_HEADER", "BLANK_HEADER", "EXTRA_COLUMN"])

    def test_short_and_long_rows(self):
        report = self.validate("id,b\n1\n1,x,extra\n", {"id": {"type": "integer"}, "b": {"type": "string"}})
        self.assertEqual([f["row"] for f in report["findings"]], [2, 3])
        self.assertTrue(all(f["code"] == "ROW_WIDTH" for f in report["findings"]))

    def test_bom_multiline_and_escaped_quotes(self):
        report = self.validate('\ufeffid;name\r\n1;"line 1\r\nline ""two"""\r\n', {"id": {"type": "integer"}, "name": {"type": "string"}}, delimiter=";")
        self.assertTrue(report["valid"])

    def test_malformed_quotes_rejected(self):
        for text in ['id\n"unfinished\n', 'id\nbad"quote\n', 'id\n"valid"bad\n']:
            with self.subTest(text=text), self.assertRaises(InputError):
                self.validate(text, {"id": {"type": "string"}})

    def test_empty_and_invalid_utf8_inputs(self):
        with self.assertRaises(InputError):
            self.validate("")
        self.path.write_bytes(b"id\n\xff\n")
        with self.assertRaises(InputError):
            validate_csv(self.path, parse_contract({"version": 1, "columns": {"id": {"type": "string"}}}))

    def test_file_and_field_boundaries(self):
        self.assertTrue(self.validate("id\n1\n", limits=Limits(file_bytes=5))["valid"])
        with self.assertRaises(InputError):
            self.validate("id\n1\n", limits=Limits(file_bytes=4))
        self.assertTrue(self.validate("id\n123\n", limits=Limits(field_chars=3))["valid"])
        with self.assertRaises(InputError):
            self.validate("id\n1234\n", limits=Limits(field_chars=3))

    def test_record_and_line_limits(self):
        self.assertTrue(self.validate("id\n1\n", limits=Limits(rows=1))["valid"])
        with self.assertRaises(InputError):
            self.validate("id\n1\n2\n", limits=Limits(rows=1))
        with self.assertRaises(InputError):
            self.validate("id\n123456\n", limits=Limits(line_chars=5))

    def test_finding_count_survives_detail_truncation(self):
        report = self.validate("id\na\nb\nc\n", limits=Limits(findings=1))
        self.assertEqual(report["finding_count"], 3)
        self.assertEqual(len(report["findings"]), 1)
        self.assertTrue(report["truncated"])

    def test_report_byte_budget(self):
        report = self.validate("id\n" + "bad\n" * 100, limits=Limits(report_bytes=1000))
        self.assertLess(len(json.dumps(report).encode()), 1000)
        self.assertEqual(report["finding_count"], 100)
        self.assertTrue(report["truncated"])

    def test_report_is_deterministic(self):
        self.assertEqual(self.validate("id\nx\n"), self.validate("id\nx\n"))

    def test_schema_rejects_unknown_and_wrong_types(self):
        bad = [{}, {"version": True, "columns": {"x": {"type": "string"}}},
               {"version": 1, "columns": {}}, {"version": 1, "columns": {"x": {"type": "oops"}}},
               {"version": 1, "columns": {"x": {"type": "string", "required": "yes"}}},
               {"version": 1, "columns": {"x": {"type": "string", "typo": True}}},
               {"version": 1, "columns": {"x": {"type": "string"}}, "typo": 1}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(InputError):
                parse_contract(value)

    def test_schema_rejects_bad_ranges_enums_and_unique_keys(self):
        cases = [{"type": "string", "min": 1}, {"type": "integer", "nonempty": True},
                 {"type": "decimal", "min": 2, "max": 1}, {"type": "decimal", "min": float("inf")},
                 {"type": "integer", "enum": [True]}, {"type": "date", "enum": ["2026-02-30"]},
                 {"type": "string", "enum": ["x", "x"]}]
        for rule in cases:
            with self.subTest(rule=rule), self.assertRaises(InputError):
                parse_contract({"version": 1, "columns": {"x": rule}})
        for keys in [[[]], [["unknown"]], [["x", "x"]], [["x"], ["x"]], "x"]:
            with self.subTest(keys=keys), self.assertRaises(InputError):
                parse_contract({"version": 1, "columns": {"x": {"type": "string"}}, "unique": keys})

    def test_contract_loader_rejects_duplicate_json_keys_and_invalid_json(self):
        for raw in ['{"version":1,"version":1,"columns":{}}', '{"version":NaN}', '{oops', '{"x":']:
            self.path.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaises(InputError):
                load_contract(self.path)

    def test_contract_loader_controls_excessively_large_numeric_exponent(self):
        self.path.write_text('{"version":1,"columns":{"x":{"type":"decimal","min":1e999999999999999999999999999}}}', encoding="utf-8")
        with self.assertRaises(InputError):
            load_contract(self.path)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.data = self.base / "input.csv"
        self.contract = self.base / "contract.json"
        self.contract.write_text(json.dumps({"version": 1, "columns": {"id": {"type": "integer"}}}), encoding="utf-8")
        self.data.write_text("id\n1\n", encoding="utf-8")

    def run_cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-m", "csv_contract", str(self.data), "--contract", str(self.contract), *map(str, args)], env=env, text=True, encoding="utf-8", capture_output=True)

    def test_valid_and_invalid_exit_statuses(self):
        result = self.run_cli("--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])
        self.data.write_text("id\nSECRET_EXAMPLE\n", encoding="utf-8")
        result = self.run_cli("--format", "json")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("SECRET_EXAMPLE", result.stdout + result.stderr)

    def test_malformed_source_does_not_replace_existing_reports(self):
        output = self.base / "report.json"
        output.write_text("keep me", encoding="utf-8")
        other = self.base / "report.txt"
        self.data.write_text('id\n"unterminated\n', encoding="utf-8")
        result = self.run_cli("--json-out", output, "--text-out", other)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(output.read_text(), "keep me")
        self.assertFalse(other.exists())
        self.assertEqual(result.stdout, "")

    def test_writes_deterministic_reports_for_invalid_data(self):
        self.data.write_text("id\nx\n", encoding="utf-8")
        output, text = self.base / "out.json", self.base / "out.txt"
        result = self.run_cli("--json-out", output, "--text-out", text)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(text.read_text(), result.stdout)
        self.assertEqual(json.loads(output.read_text())["finding_count"], 1)
        self.assertFalse(list(self.base.glob(".csv-contract-*")))

    def test_refuses_overwrite_of_input_or_contract(self):
        for path in [self.data, self.contract, self.base / "." / "input.csv"]:
            before = path.read_bytes()
            result = self.run_cli("--json-out", path)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(path.read_bytes(), before)

    def test_refuses_hardlink_alias_of_input(self):
        alias = self.base / "alias.csv"
        try:
            os.link(self.data, alias)
        except OSError:
            self.skipTest("Hard links unavailable")
        result = self.run_cli("--text-out", alias)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.data.read_text(), "id\n1\n")

    def test_refuses_colliding_report_paths(self):
        output = self.base / "report"
        result = self.run_cli("--json-out", output, "--text-out", output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())

    def test_input_and_config_errors_are_status_two(self):
        self.data.unlink()
        self.assertEqual(self.run_cli().returncode, 2)
        self.data.write_text("id\n1\n")
        self.contract.write_text('{"version":2,"columns":{}}')
        self.assertEqual(self.run_cli().returncode, 2)

    def test_negative_limits_and_missing_output_parent(self):
        self.assertEqual(self.run_cli("--max-bytes", "-1").returncode, 2)
        self.assertEqual(self.run_cli("--max-field-chars", str(sys.maxsize + 1)).returncode, 2)
        self.assertEqual(self.run_cli("--json-out", self.base / "missing" / "out.json").returncode, 2)

    def test_extreme_contract_number_is_a_controlled_error(self):
        self.contract.write_text('{"version":1,"columns":{"id":{"type":"decimal","min":1e999999999999999999999999999}}}', encoding="utf-8")
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_examples_have_documented_status(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
        for name, code in [("valid", 0), ("invalid", 1)]:
            result = subprocess.run([sys.executable, "-m", "csv_contract", str(ROOT / "examples" / f"{name}.csv"), "--contract", str(ROOT / "examples" / "contract.json")], env=env, capture_output=True)
            self.assertEqual(result.returncode, code, result.stderr)


if __name__ == "__main__":
    unittest.main()

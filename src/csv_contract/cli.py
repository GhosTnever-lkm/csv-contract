"""Command-line entry point. Validation finishes before reports are written."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from . import __version__
from .core import InputError, Limits, load_contract, validate_csv


def render_text(report):
    lines = [f"CSV Contract: {'VALID' if report['valid'] else 'INVALID'}",
             f"Records: {report['records']}; findings: {report['finding_count']}"]
    for finding in report["findings"]:
        column = json.dumps(finding["column"], ensure_ascii=True)
        lines.append(f"record {finding['row']} | {finding['code']} | {column} | {finding['message']}")
    if report["truncated"]:
        lines.append("Finding details were truncated; finding_count includes all detected findings.")
    return "\n".join(lines) + "\n"


def _same_path(left, right):
    if left.resolve() == right.resolve():
        return True
    try:
        return left.samefile(right)
    except (FileNotFoundError, OSError):
        return False


def _check_outputs(outputs, inputs):
    for i, target in enumerate(outputs):
        if any(_same_path(target, source) for source in inputs):
            raise InputError("A report must not overwrite the CSV input or contract.")
        if any(_same_path(target, other) for other in outputs[:i]):
            raise InputError("JSON and text reports must use different output paths.")
        if not target.parent.is_dir() or target.is_dir():
            raise InputError("Report output requires an existing parent directory and a file path.")


def _write_reports(outputs):
    staged = []
    try:
        for path, content in outputs:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=path.parent, prefix=".csv-contract-", delete=False) as stream:
                staged.append((Path(stream.name), path))
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for temporary, path in staged:
            os.replace(temporary, path)
    except OSError as exc:
        raise InputError("Cannot write report output.") from exc
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate CSV data against an explicit JSON contract; cell values are never reported.")
    parser.add_argument("input", type=Path, help="UTF-8 CSV input")
    parser.add_argument("--contract", required=True, type=Path, help="JSON contract (version 1)")
    parser.add_argument("--delimiter", choices=["comma", "semicolon", "tab", "pipe"], default="comma")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="stdout format")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--text-out", type=Path)
    parser.add_argument("--max-bytes", type=int, default=Limits.file_bytes)
    parser.add_argument("--max-field-chars", type=int, default=Limits.field_chars)
    parser.add_argument("--max-rows", type=int, default=Limits.rows)
    parser.add_argument("--max-findings", type=int, default=Limits.findings)
    parser.add_argument("--version", action="version", version=f"csv-contract {__version__}")
    args = parser.parse_args(argv)
    try:
        limits = Limits(file_bytes=args.max_bytes, field_chars=args.max_field_chars,
                        rows=args.max_rows, findings=args.max_findings)
        targets = [p for p in (args.json_out, args.text_out) if p is not None]
        _check_outputs(targets, [args.input, args.contract])
        contract = load_contract(args.contract)
        delimiter = {"comma": ",", "semicolon": ";", "tab": "\t", "pipe": "|"}[args.delimiter]
        report = validate_csv(args.input, contract, delimiter=delimiter, limits=limits)
        json_text = json.dumps(report, ensure_ascii=True, separators=(",", ":")) + "\n"
        text = render_text(report)
        outputs = []
        if args.json_out is not None:
            outputs.append((args.json_out, json_text))
        if args.text_out is not None:
            outputs.append((args.text_out, text))
        _write_reports(outputs)
        sys.stdout.write(json_text if args.format == "json" else text)
        return 0 if report["valid"] else 1
    except (InputError, ValueError, RecursionError, OSError) as exc:
        # Messages are controlled; input cell values and parser snippets are excluded.
        message = str(exc) if isinstance(exc, InputError) else "Input could not be processed reliably."
        print(f"csv-contract: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

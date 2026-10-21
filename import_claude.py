#!/usr/bin/env python3
"""Import explicitly supplied Claude API cost CSV exports into data/claude.json."""
import argparse
import csv
import datetime as dt
import hashlib
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

import billing


NAME = re.compile(r"claude_api_cost_(\d{4}_\d{2}_\d{2})_to_(\d{4}_\d{2}_\d{2})(?: \(\d+\))?\.csv$")
DIMENSIONS = ("usage_date_utc", "workspace", "api_key_id", "model", "usage_type",
              "context_window", "token_type", "cost_type", "inference_geo", "speed")


def import_reports(inputs):
    paths = []
    for source in inputs:
        path = Path(source).expanduser()
        if path.is_dir():
            paths.extend(sorted(path.glob("claude_api_cost_*.csv")))
        elif path.is_file():
            paths.append(path)
        else:
            raise ValueError(f"Claude input not found: {path}")
    paths = sorted(set(paths))
    if not paths:
        raise ValueError("No Claude API cost CSVs found in the supplied input")
    buckets = {}
    report_months = set()
    hashes = set()
    duplicates = []
    for path in paths:
        match = NAME.fullmatch(path.name)
        if not match:
            raise ValueError(f"Unexpected Claude filename: {path.name}")
        first = dt.date.fromisoformat(match[1].replace("_", "-"))
        last = dt.date.fromisoformat(match[2].replace("_", "-"))
        if last < first:
            raise ValueError(f"Invalid report date range: {path.name}")
        cursor = first.replace(day=1)
        while cursor <= last:
            report_months.add(cursor.strftime("%Y-%m"))
            cursor = billing.month_shift(cursor, 1)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest in hashes:
            duplicates.append(path.name)
            continue
        hashes.add(digest)
        file_buckets = defaultdict(Decimal)
        for row in csv.DictReader(content.decode("utf-8-sig").splitlines()):
            try:
                usage = dt.date.fromisoformat(row["usage_date_utc"])
                amount = Decimal(row["cost_usd"])
            except (KeyError, TypeError, InvalidOperation, ValueError) as error:
                raise ValueError(f"Invalid Claude cost row in {path.name}: {error}") from error
            if not first <= usage <= last or not amount.is_finite():
                raise ValueError(f"Invalid Claude date or amount in {path.name}")
            key = tuple(row.get(d, "") or "" for d in DIMENSIONS)
            file_buckets[key] += amount
        for key, amount in file_buckets.items():
            if key in buckets and buckets[key] != amount:
                raise ValueError(f"Overlapping Claude exports disagree for {key[0]}, workspace {key[1]}, model {key[3]}")
            buckets[key] = amount
    daily = defaultdict(Decimal)
    for key, amount in buckets.items():
        date, workspace, _, model, usage_type, _, _, cost_type, _, _ = key
        service = model if model and model != "--" else (usage_type or cost_type or "Other usage")
        daily[(date, workspace or "__unallocated__", service)] += amount
    daily_rows = [dict(usage_date=date, month=date[:7], entity_id=workspace, entity_name=workspace,
                       service=service, currency="USD", amount=str(amount))
                  for (date, workspace, service), amount in sorted(daily.items())]
    months = sorted(report_months)
    start = months[0] + "-01"
    end = billing.month_shift(dt.date.fromisoformat(months[-1] + "-01"), 1).isoformat()
    records = billing.normalize(daily_rows, "claude", months)
    snapshot = dict(schema_version=1, provider="claude", start=start, end=end, months=months,
                    extracted_at=dt.datetime.now(dt.timezone.utc).isoformat(), records=records,
                    daily_records=daily_rows,
                    meta=dict(source="Claude API cost CSV", metric="cost_usd", estimated_months=[],
                              covered_months=months, missing_months=[], files=[p.name for p in paths],
                              duplicate_files_skipped=duplicates))
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="CSV file(s) and/or directories containing Claude API cost CSVs")
    args = parser.parse_args()
    try:
        snapshot = import_reports(args.inputs)
        saved = {p: json.loads((billing.DATA / f"{p}.json").read_text()) for p in billing.CLOUD_PROVIDERS
                 if (billing.DATA / f"{p}.json").exists()}
        payload = billing.assemble({**saved, "claude": snapshot})
        billing.atomic_write(billing.DATA / "claude.json", json.dumps(snapshot, ensure_ascii=False, allow_nan=False) + "\n")
        billing.build(payload)
        print(f"Imported {len(snapshot['records'])} Claude monthly rows from {len(snapshot['meta']['files'])} files")
    except (ValueError, KeyError, OSError, UnicodeError, json.JSONDecodeError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()

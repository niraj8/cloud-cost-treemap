#!/usr/bin/env python3
"""Extract monthly cloud costs and build an offline HTML dashboard; stdlib only."""
import argparse
import csv
import datetime as dt
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from decimal import Decimal

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CLOUD_PROVIDERS = ("aws", "gcp")
PROVIDERS = (*CLOUD_PROVIDERS, "claude")


def month_shift(date, offset):
    index = date.year * 12 + date.month - 1 + offset
    return dt.date(index // 12, index % 12 + 1, 1)


def window(count, end=None, today=None):
    today = today or dt.datetime.now(dt.timezone.utc).date()
    end = dt.date.fromisoformat(end) if end else today
    if not 1 <= count <= 36:
        raise ValueError("months must be between 1 and 36")
    if end > today:
        raise ValueError("end must not be in the future (it is an exclusive UTC date)")
    # With no override include the current month, even on its first day.
    last = today.replace(day=1) if end == today else (end - dt.timedelta(days=1)).replace(day=1)
    start = month_shift(last, 1 - count)
    return start.isoformat(), end.isoformat(), [month_shift(start, i).strftime("%Y-%m") for i in range(count)]


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        f.write(content)
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def run_json(command):
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=True,
                                env={**os.environ, "AWS_PAGER": ""}, timeout=900)
    except FileNotFoundError:
        raise ValueError(f"Missing CLI: {command[0]}. See README.md for setup.") from None
    except subprocess.TimeoutExpired:
        raise ValueError(f"{command[0]} timed out after 15 minutes; existing snapshots are unchanged.") from None
    except subprocess.CalledProcessError as e:
        raise ValueError(f"{command[0]} failed:\n{e.stderr.strip() or e.stdout.strip()}") from None
    return json.loads(result.stdout)


def normalize(rows, provider, months):
    grouped = defaultdict(Decimal)
    names = {}
    for r in rows:
        month = str(r["month"])
        amount = Decimal(str(r["amount"]))
        if month not in months or not amount.is_finite():
            raise ValueError(f"Invalid {provider} record: month or amount")
        currency = str(r["currency"])
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError(f"Unsupported currency {currency!r}; expected an ISO currency code")
        entity = str(r["entity_id"])
        service = str(r["service"])
        names[entity] = str(r.get("entity_name") or entity)
        grouped[(month, entity, service, currency)] += amount
    output = []
    for (month, entity, service, currency), amount in sorted(grouped.items()):
        value = float(amount)
        if not math.isfinite(value):
            raise ValueError("Cost exceeds supported numeric range")
        output.append(dict(provider=provider, month=month, entity_id=entity,
                           entity_name=names[entity], service=service, currency=currency, amount=value))
    return output


def aws_extract(args, start, end, months):
    rows, estimated, names = [], set(), {}
    token = None
    seen = set()
    while True:
        cmd = ["aws", "ce", "get-cost-and-usage", "--region", "us-east-1", "--output", "json",
               "--no-cli-pager", "--no-paginate", "--time-period", f"Start={start},End={end}",
               "--granularity", "MONTHLY", "--metrics", args.aws_metric, "--group-by",
               "Type=DIMENSION,Key=LINKED_ACCOUNT", "Type=DIMENSION,Key=SERVICE"]
        if args.aws_profile:
            cmd += ["--profile", args.aws_profile]
        if token:
            cmd += ["--next-page-token", token]
        page = run_json(cmd)
        for attr in page.get("DimensionValueAttributes", []):
            if attr.get("Attributes", {}).get("description"):
                names[attr["Value"]] = attr["Attributes"]["description"]
        for period in page.get("ResultsByTime", []):
            month = period["TimePeriod"]["Start"][:7]
            if period.get("Estimated"):
                estimated.add(month)
            for group in period.get("Groups", []):
                metric = group["Metrics"][args.aws_metric]
                rows.append(dict(month=month, entity_id=group["Keys"][0], service=group["Keys"][1],
                                 currency=metric["Unit"], amount=metric["Amount"]))
        token = page.get("NextPageToken")
        if not token:
            break
        if token in seen:
            raise ValueError("AWS returned a repeated pagination token")
        seen.add(token)
    for row in rows:
        row["entity_name"] = names.get(row["entity_id"], row["entity_id"])
    return normalize(rows, "aws", months), {"metric": args.aws_metric, "estimated_months": sorted(estimated),
                                           "source": "AWS Cost Explorer", "profile": args.aws_profile or "default CLI credentials"}


def gcp_sql(table, start, end):
    if not table or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+", table):
        raise ValueError("Set GCP_TABLE=project.dataset.gcp_billing_export_v1_… (one standard or detailed export table)")
    return f"""SELECT
      FORMAT_TIMESTAMP('%Y-%m', usage_start_time, 'UTC') AS month,
      COALESCE(NULLIF(project.id, ''), '__unallocated__') AS entity_id,
      COALESCE(NULLIF(project.name, ''), NULLIF(project.id, ''), 'Unallocated / billing-level') AS entity_name,
      COALESCE(service.description, 'Other / adjustments') AS service,
      currency,
      CAST(SUM(CAST(cost AS NUMERIC) +
        IFNULL((SELECT SUM(CAST(c.amount AS NUMERIC)) FROM UNNEST(credits) c), 0)) AS STRING) AS amount
    FROM `{table}`
    WHERE usage_start_time >= TIMESTAMP('{start}')
      AND usage_start_time < TIMESTAMP('{end}')
    GROUP BY month, entity_id, entity_name, service, currency
    ORDER BY month, entity_id, service, currency"""


def gcp_extract(args, start, end, months):
    sql = gcp_sql(args.gcp_table, start, end)
    cmd = ["bq", "--format=json"]
    if args.gcp_project:
        cmd += [f"--project_id={args.gcp_project}"]
    if args.gcp_location:
        cmd += [f"--location={args.gcp_location}"]
    cmd += ["query", "--use_legacy_sql=false", "--max_rows=100000",
            f"--maximum_bytes_billed={args.gcp_max_bytes}", sql]
    rows = run_json(cmd)
    if len(rows) >= 100000:
        raise ValueError("GCP result reached the 100,000 row limit; refusing a potentially truncated snapshot")
    return normalize(rows, "gcp", months), {"metric": "cost + credits", "estimated_months": [],
                                           "source": args.gcp_table}


def sample(count=6):
    start, end, months = window(count)
    rows = []
    specs = {
        "aws": ([('111111111111', 'Production'), ('222222222222', 'Staging'), ('333333333333', 'Shared services')],
                [('Amazon EC2', 9400), ('Amazon RDS', 5700), ('Amazon S3', 2100), ('Amazon CloudFront', 1800),
                 ('Amazon EKS', 1450), ('Amazon CloudWatch', 820), ('AWS Lambda', 620), ('AWS Support', 490)]),
        "gcp": ([('platform-prod', 'Platform production'), ('analytics-prod', 'Analytics'), ('platform-dev', 'Development')],
                [('Compute Engine', 6700), ('BigQuery', 4300), ('Cloud SQL', 2900), ('Cloud Storage', 1300),
                 ('Kubernetes Engine', 1100), ('Cloud Run', 790), ('Cloud Logging', 510)])}
    for provider, (entities, services) in specs.items():
        for i, month in enumerate(months):
            for j, (entity, name) in enumerate(entities):
                for k, (service, base) in enumerate(services):
                    amount = round(base * [1, .24, .13][j] * (1 + i * (.035 + k * .008)) * (1 + ((i + k) % 3) * .013), 2)
                    rows.append(dict(provider=provider, month=month, entity_id=entity, entity_name=name,
                                     service=service, currency="USD", amount=amount))
            rows.append(dict(provider=provider, month=month, entity_id=entities[0][0], entity_name=entities[0][1],
                             service="Credits / adjustments", currency="USD", amount=-350 - i * 25))
    return dict(schema_version=1, demo=True, start=start, end=end, months=months, records=rows,
                generated_at=dt.datetime.now(dt.timezone.utc).isoformat(), providers={
                    "aws": {"metric": "UnblendedCost", "source": "Sample data", "estimated_months": [months[-1]]},
                    "gcp": {"metric": "cost + credits", "source": "Sample data", "estimated_months": []}})


def merge_snapshot(previous, fresh):
    """Replace refreshed months and retain the earlier, contiguous history."""
    if previous is None:
        return fresh
    if any(previous.get(k) != fresh.get(k) for k in ("schema_version", "provider")) or previous.get("demo"):
        raise ValueError("Cannot append to an incompatible snapshot")
    for key in ("metric", "source", "profile"):
        if previous["meta"].get(key) != fresh["meta"].get(key):
            raise ValueError(f"Billing {key} changed; use a full extraction instead of --append")
    if not previous["start"] <= fresh["start"] <= previous["end"] or fresh["end"] < previous["end"]:
        raise ValueError("Append must refresh the latest months or continue the saved window without gaps or an earlier cutoff")
    retained = [m for m in previous["months"] if m < fresh["start"][:7]]
    estimated = sorted((set(previous["meta"].get("estimated_months", [])) & set(retained)) |
                       set(fresh["meta"].get("estimated_months", [])))
    checked = {m: previous["meta"].get("month_extracted_at", {}).get(m, previous["extracted_at"])
               for m in retained}
    checked.update({m: fresh["extracted_at"] for m in fresh["months"]})
    return {**fresh, "start": previous["start"], "months": retained + fresh["months"],
            "records": [r for r in previous["records"] if r["month"] in retained] + fresh["records"],
            "meta": {**fresh["meta"], "estimated_months": estimated, "month_extracted_at": checked}}


def assemble(snapshots):
    first = next((s for p, s in snapshots.items() if p in CLOUD_PROVIDERS), next(iter(snapshots.values())))
    rows = []
    metadata = {}
    for provider, snapshot in snapshots.items():
        if snapshot.get("schema_version") != 1 or snapshot.get("provider") != provider:
            raise ValueError(f"Invalid snapshot for {provider}")
        if provider in CLOUD_PROVIDERS and any(snapshot[key] != first[key] for key in ("start", "end", "months")):
            raise ValueError("Provider snapshots cover different date windows. Extract all saved providers together to align them.")
        if snapshot.get("demo"):
            raise ValueError("Sample data cannot be merged into live snapshots")
        meta = {**snapshot["meta"], "extracted_at": snapshot["extracted_at"]}
        if provider == "claude":
            covered = set(meta.get("covered_months", [])) & set(first["months"])
            meta["covered_months"] = sorted(covered)
            meta["missing_months"] = [m for m in first["months"] if m not in covered]
            source = snapshot.get("daily_records", snapshot["records"])
            source = [r for r in source if first["start"] <= r.get("usage_date", r["month"] + "-01") < first["end"]]
        else:
            source = snapshot["records"]
        rows.extend(normalize(source, provider, first["months"]))
        metadata[provider] = meta
    return dict(schema_version=1, demo=False, start=first["start"], end=first["end"], months=first["months"],
                generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                providers=metadata, records=rows)


def build(payload):
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    # Keep names containing HTML or script terminators inert inside the data block.
    embedded = serialized.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    html = (HERE / "dashboard.template.html").read_text()
    html = html.replace("__CSS__", (HERE / "dashboard.css").read_text())
    html = html.replace("__JS__", (HERE / "dashboard.js").read_text()).replace("__DATA__", embedded)
    atomic_write(HERE / "cloud-cost-treemap.html", html)
    atomic_write(DATA / "costs.json", serialized + "\n")
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=["provider", "month", "entity_id", "entity_name", "service", "currency", "amount"])
    writer.writeheader()
    for row in payload["records"]:
        # Guard spreadsheet formula injection in provider-controlled names.
        writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v
                         for k, v in row.items()})
    atomic_write(DATA / "costs.csv", stream.getvalue())
    print(f"Built cloud-cost-treemap.html · {len(payload['records'])} rows · {'SAMPLE' if payload['demo'] else 'LIVE'} data")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="Rebuild HTML from local snapshots only")
    b.add_argument("--demo", action="store_true")
    b.add_argument("--months", type=int, default=6)
    e = sub.add_parser("extract", help="Fetch billing data, save snapshots, then build HTML")
    e.add_argument("--providers", default="aws,gcp")
    e.add_argument("--months", type=int, default=6)
    e.add_argument("--append", action="store_true", help="Replace queried months while preserving earlier saved history")
    e.add_argument("--end", help="Exclusive UTC end date; default today, with current month included")
    e.add_argument("--aws-profile")
    e.add_argument("--aws-metric", default="UnblendedCost", choices=["UnblendedCost", "NetUnblendedCost", "AmortizedCost", "NetAmortizedCost"])
    e.add_argument("--gcp-table", default=os.getenv("GCP_TABLE"))
    e.add_argument("--gcp-project")
    e.add_argument("--gcp-location")
    e.add_argument("--gcp-max-bytes", type=int, default=10000000000)
    args = parser.parse_args()
    try:
        snapshots = {p: json.loads((DATA / f"{p}.json").read_text()) for p in PROVIDERS if (DATA / f"{p}.json").exists()}
        if args.command == "build":
            build(sample(args.months) if args.demo or not snapshots else assemble(snapshots))
            return
        providers = list(dict.fromkeys(args.providers.split(",")))
        if not providers or any(p not in CLOUD_PROVIDERS for p in providers):
            raise ValueError("providers must be a comma-separated list of aws, gcp")
        start, end, months = window(args.months, args.end)
        if start >= end:
            raise ValueError("The selected window has no completed UTC days; retry tomorrow or choose an earlier end")
        if "gcp" in providers:
            gcp_sql(args.gcp_table, start, end)  # Validate before making any paid query.
            if args.gcp_max_bytes <= 0:
                raise ValueError("gcp-max-bytes must be positive")
        # Check merge compatibility and provider alignment before paid cloud queries.
        previews = {}
        for provider in providers:
            meta = ({"metric": args.aws_metric, "source": "AWS Cost Explorer",
                     "profile": args.aws_profile or "default CLI credentials"} if provider == "aws" else
                    {"metric": "cost + credits", "source": args.gcp_table})
            preview = dict(schema_version=1, provider=provider, start=start, end=end, months=months,
                           extracted_at=dt.datetime.now(dt.timezone.utc).isoformat(), records=[], meta=meta)
            previews[provider] = merge_snapshot(snapshots.get(provider), preview) if args.append else preview
        assemble({**snapshots, **previews})
        pending = {}
        for provider in providers:
            print(f"Extracting {provider.upper()} · {start} ≤ usage < {end} …", flush=True)
            rows, meta = (aws_extract if provider == "aws" else gcp_extract)(args, start, end, months)
            pending[provider] = dict(schema_version=1, provider=provider, start=start, end=end, months=months,
                                     extracted_at=dt.datetime.now(dt.timezone.utc).isoformat(), records=rows, meta=meta)
            if args.append:
                pending[provider] = merge_snapshot(snapshots.get(provider), pending[provider])
        # Validate all results before replacing any snapshots. Failed queries preserve prior data.
        payload = assemble({**snapshots, **pending})
        for provider, snapshot in pending.items():
            atomic_write(DATA / f"{provider}.json", json.dumps(snapshot, ensure_ascii=False, allow_nan=False) + "\n")
        build(payload)
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()

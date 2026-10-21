#!/usr/bin/env python3
"""Run account-scoped Thrifty checks and assemble local review reports."""
import concurrent.futures
import csv
import datetime as dt
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "data/thrifty/reports/all-accounts"
CLEANUP_CHECKS = {
    "unattached_ebs_volumes", "ebs_volumes_on_stopped_instances",
    "ebs_snapshot_max_age", "ebs_unused_snapshots", "rds_unused_snapshots",
    "ec2_application_lb_unused", "ec2_classic_lb_unused",
    "ec2_gateway_lb_unused", "ec2_network_lb_unused",
    "unattached_eips", "vpc_nat_gateway_unused",
    "ecr_repository_unused_images", "secretsmanager_secret_unused",
    "route53_health_check_unused",
}


def controls(group):
    yield from group.get("controls") or []
    for child in group.get("groups") or []:
        yield from controls(child)


def scan(account):
    name = account["profile"]
    print(f"Starting {name} ({account['account_id']})", flush=True)
    start = dt.datetime.now(dt.timezone.utc).isoformat()
    result = {**account, "started_at": start}
    try:
        with (REPORTS / f"{name}.json").open("w") as output, \
                (REPORTS / f"{name}.log").open("w") as errors:
            proc = subprocess.run(
                ["sh", str(ROOT / "thrifty.sh"), "powerpipe", "benchmark", "run",
                 "account_review", "--search-path", account["connection"],
                 "--output", "json", "--progress=false", "--input=false",
                 "--mod-install=false", "--max-parallel", "4",
                 "--query-timeout", "300"],
                cwd=ROOT, stdout=output, stderr=errors, timeout=1800,
            )
        result["exit_code"] = proc.returncode
        data = json.loads((REPORTS / f"{name}.json").read_text())
        account_controls = list(controls(data))
        result["controls"] = len(account_controls)
        result["status"] = data["summary"]["status"]
        result["query_errors"] = [
            {"control": c["control_id"], "error": c["run_error"]}
            for c in account_controls if c.get("run_error")
        ]
        result["report_valid"] = True
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, KeyError) as exc:
        result["report_valid"] = False
        result["failure"] = str(exc)
    result["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    (REPORTS / f"{name}-status.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"Finished {name}: {result.get('status', result.get('failure'))}", flush=True)
    return result


def write_csv(name, rows):
    columns = ["profile", "account_id", "region", "control", "title", "status",
               "resource", "reason"]
    with (REPORTS / name).open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def summarize(results):
    rows = []
    failures = []
    for result in results:
        if not result.get("report_valid"):
            failures.append(result)
            continue
        data = json.loads((REPORTS / f"{result['profile']}.json").read_text())
        for control in controls(data):
            for row in control.get("results") or []:
                dimensions = {v["key"]: v["value"] for v in row.get("dimensions") or []}
                rows.append({
                    "profile": result["profile"], "account_id": result["account_id"],
                    "region": dimensions.get("region", ""),
                    "control": control["control_id"], "title": control["title"],
                    "status": row["status"], "resource": row["resource"],
                    "reason": row["reason"],
                })
    alarms = [r for r in rows if r["status"] == "alarm"]
    cleanup = [r for r in alarms if r["control"].split(".")[-1] in CLEANUP_CHECKS]
    unknowns = [r for r in rows if r["status"] == "error"]
    write_csv("findings.csv", alarms)
    write_csv("cleanup-review.csv", cleanup)
    write_csv("unknowns.csv", unknowns)
    (REPORTS / "scan-status.json").write_text(json.dumps(results, indent=2) + "\n")
    lines = [
        "# All-account AWS Thrifty review", "",
        f"Generated {dt.datetime.now(dt.timezone.utc).isoformat()}.", "",
        "One profile per distinct AWS account; region coverage follows the local connection configuration. "
        "Each account was scanned independently; check counts are listed below. No AWS resources were modified.", "",
        "| Profile | Account | Checks | Alarm rows | Unique flagged resources | Cleanup review resources | Unknown rows | Query failures |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        name = result["profile"]
        subset = [r for r in alarms if r["profile"] == name]
        candidates = [r for r in cleanup if r["profile"] == name]
        incomplete_rows = [r for r in unknowns if r["profile"] == name]
        status = result.get("status", {})
        lines.append(
            f"| {name} | {result['account_id']} | {result.get('controls', 0)} | "
            f"{status.get('alarm', '?')} | {len({r['resource'] for r in subset})} | "
            f"{len({r['resource'] for r in candidates})} | {len(incomplete_rows)} | "
            f"{len(result.get('query_errors', []))} |"
        )
    lines += ["", "## Cleanup review candidates", "",
              "These are hypotheses for review, not deletion instructions or confirmed savings. "
              "Snapshots may be required backups, unused load balancers may be deployment/failover resources, "
              "and ECR's no-pull check also flags empty repositories that cost $0. "
              "NAT traffic evidence spans the most recent five days.", "",
              "| Profile | Check | Flagged resources |", "|---|---|---:|"]
    grouped = {}
    for row in cleanup:
        grouped.setdefault((row["profile"], row["title"]), set()).add(row["resource"])
    for (profile, title), resources in sorted(grouped.items()):
        lines.append(f"| {profile} | {title} | {len(resources)} |")
    lines += ["", "## Coverage and interpretation", "",
              "Alarm counts are check-result rows, not distinct resources or savings estimates. "
              "Logs can dominate these counts. The shortlist excludes log-stream age, "
              "rightsizing, Graviton, lifecycle/retention and billing-trend suggestions.", "",
              "Unknown rows indicate missing metrics or other incomplete evidence. "
              "Control-level query failures are listed separately below. "
              "Neither should be interpreted as zero usage. "
              "When a profile accesses the management account, its Cost Explorer "
              "trend check can reflect organization billing rather than just that account.", ""]
    for result in results:
        for error in result.get("query_errors", []):
            lines += [f"### {result['profile']}: {error['control']}", "",
                      "```text", error["error"], "```", ""]
    for failure in failures:
        lines += [f"Account scan failed: {failure['profile']}: {failure['failure']}", ""]
    lines += ["Files: cleanup-review.csv, findings.csv, unknowns.csv, scan-status.json "
              "and per-account JSON/log files. Everything is local under ignored data/.", ""]
    (REPORTS / "review.md").write_text("\n".join(lines))
    for path in REPORTS.iterdir():
        if path.is_file():
            path.chmod(0o600)
    print(f"Saved combined report: {len(alarms)} alarm rows; "
          f"{len({(r['profile'], r['resource']) for r in cleanup})} cleanup review resources; "
          f"{len(unknowns)} unknown rows; {len(failures)} account scan failures.", flush=True)


if __name__ == "__main__":
    accounts = json.loads((REPORTS / "identities.json").read_text())
    assert len({a["account_id"] for a in accounts}) == len(accounts), "Duplicate accounts"
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(scan, accounts))
    summarize(results)

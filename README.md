# Cloud Cost Atlas

A self-contained HTML dashboard for AWS, GCP, and direct Claude API costs. It shows service-cost treemaps by AWS account, GCP project, or Claude workspace, plus monthly trends. Python 3.10+ is the only runtime dependency for building the page; cloud extraction additionally needs the AWS CLI and Google Cloud `bq` CLI.

## Quick start

```sh
git clone https://github.com/niraj8/cloud-cost-treemap.git
cd cloud-cost-treemap
make demo
make open
```

The demo uses synthetic accounts, projects, and costs. No credentials or cloud
requests are needed. To use your own organization, configure billing access below.
AWS and GCP can be used independently with `PROVIDERS=aws` or `PROVIDERS=gcp`;
Claude CSV imports can also be used on their own. The dashboard runs in a modern
browser. `make open` is a macOS shortcut; elsewhere open the generated HTML directly
or run `make serve` and browse `http://localhost:8000/cloud-cost-treemap.html`.

## Refresh data

Create an ignored `billing.local.mk` with your billing settings:

```make
AWS_PROFILE := your-management-account-profile
GCP_TABLE := billing-project.dataset.gcp_billing_export_v1_ABCDEF
GCP_PROJECT := billing-project
GCP_LOCATION := US
```

The AWS profile should access the organization payer/management account with `ce:GetCostAndUsage`. One central Cost Explorer query groups by linked account and service; iterating all AWS profiles could double-count costs. `GCP_TABLE` should name one standard *or* detailed Cloud Billing export table for the billing account. The GCP identity needs permission to read that table and create BigQuery query jobs. Billing export data is grouped by project and service; projects without direct `gcloud` visibility can still appear.

Authenticate with the appropriate AWS and GCP CLIs, then run:

```sh
make extract                  # refresh AWS + GCP, then rebuild HTML
make open                     # open the saved HTML on macOS
```

For routine month-to-date updates, fetch only the current month and keep saved history:

```sh
make extract-month            # AWS + GCP, current month only, then rebuild HTML
# Reproducible October cutoff (exclusive UTC):
make extract-month END=2026-10-15
```

This replaces the queried month's rows, including removing rows no longer returned,
so repeated updates do not double-count costs. Earlier months keep their saved data
and per-month extraction dates. Use `make extract MONTHS=2 APPEND=1` to refresh the
latest two months while keeping older history, for example to capture delayed billing
adjustments after month-end. Appends reject gaps, earlier cutoffs, and changes to the
billing source/profile/metric. A full `make extract` remains available to re-query
historical costs; older saved months are not refreshed by `extract-month`.

`make extract` defaults to six months including the current month (month-to-date). It can incur AWS Cost Explorer API and BigQuery query charges; run it only when you want a refresh. GCP net cost includes credits (`cost + credits.amount`), so it need not match an invoice gross total. The default AWS metric is `UnblendedCost`; override with `AWS_METRIC=NetAmortizedCost` or another supported metric if needed. Currencies are kept separate, never converted. The end date is exclusive UTC and defaults to today; delayed cloud data may later change recent months.

On a later day, refresh AWS and GCP together so their windows align. On the same UTC day you may refresh one provider with `make extract PROVIDERS=aws` or `PROVIDERS=gcp`. For a reproducible cutoff use, for example, `make extract END=2026-10-01`; for more history use `MONTHS=12`. Extraction fails without replacing snapshots if a cloud query fails. `GCP_MAX_BYTES` defaults to 10 GB as a BigQuery billed-bytes cap.

## Import Claude CSVs manually

Download Claude API cost exports from your provider, then pass the file or directory explicitly:

```sh
make import-claude CLAUDE_INPUT=/path/to/claude-reports
# or: make import-claude CLAUDE_INPUT=/path/to/claude_api_cost_2026_08_01_to_2026_08_31.csv
```

This command makes **no cloud requests**. The standalone `import_claude.py` reads `claude_api_cost_*.csv` reports, writes `data/claude.json`, and rebuilds the dashboard. It preserves all months in the supplied reports even when the dashboard's current six-month window is narrower. Re-import with the complete set of reports you want retained; an import replaces the previous Claude snapshot. Exact duplicate files and identical overlapping billing rows are counted once, while conflicting overlaps cause an error. API key identifiers are used only for deduplication and are not written to `data/` or the HTML. A month without a Claude report is marked unavailable rather than zero.

Direct Claude API costs are distinct from Claude usage billed through AWS or GCP. Reports may not include subscriptions, credit purchases, refunds, or invoice adjustments unless those amounts appear in the CSV.

## Iterate on the dashboard

```sh
make html                     # rebuild from data/ snapshots, no network
make open                     # rebuild and open the HTML on macOS
make serve PORT=8000          # optional localhost-only static server
make demo                     # build a sample preview, without changing snapshots
make test                     # run unit tests
```

Edit `dashboard.template.html`, `dashboard.css`, or `dashboard.js`, then run `make html`. The generated `cloud-cost-treemap.html` embeds all assets and data; a web server is optional. When no live snapshots exist, `make html` builds labeled sample data. Run `make html` after `make demo` to return to saved live data.

All persistent billing snapshots and normalized output are under ignored `data/`: `aws.json`, `gcp.json`, `claude.json`, `costs.json`, and `costs.csv`. The generated HTML and local Make configuration are also ignored. They contain sensitive billing names, IDs, and costs, so don't force-add or publish them. `git check-ignore -v data/costs.json cloud-cost-treemap.html billing.local.mk` verifies the protections.

## Optional AWS resource review with Thrifty

`thrifty.sh` and `run_thrifty_accounts.py` are optional helpers for an independently
configured [Steampipe](https://steampipe.io/) and
[Powerpipe AWS Thrifty](https://hub.powerpipe.io/mods/turbot/aws_thrifty) installation.
They are not required for the billing dashboard. Tool binaries, plugins,
connections, benchmarks, account manifests, and scan results are local inputs;
this repository does not install or provision them.

To use the helpers, prepare this ignored layout:

```text
data/thrifty/
  bin/steampipe
  bin/powerpipe
  steampipe/             # AWS plugin and your connection configuration
  powerpipe/             # Powerpipe state
  mod/                   # Thrifty mod and an account_review benchmark
  reports/all-accounts/identities.json
```

Configure AWS connections for your own profiles and intended regions. Define
`account_review` in the local mod using the Thrifty checks you want to run.
Create `identities.json` with one entry per distinct AWS account, using each
configured connection's name (replace the synthetic example values):

```json
[
  {"profile": "example-billing", "account_id": "111111111111", "connection": "aws_example"}
]
```

Use simple file-safe profile names because the runner uses them as report
filenames. Use dedicated AWS roles with the read permissions required by your
chosen checks. AWS API usage, including CloudWatch metrics and Cost Explorer
requests, can incur charges.

```sh
sh thrifty.sh steampipe service start --database-listen local
python3 run_thrifty_accounts.py
sh thrifty.sh steampipe service stop
```

The runner scans each account with an explicit connection search path, two
accounts at a time, to avoid cross-account joins. Combined findings, cleanup
review candidates, missing-evidence rows, and coverage details are saved under
`data/thrifty/reports/all-accounts/`. Inspect `scan-status.json` and `review.md`:
an exit code of 1 from Powerpipe can indicate incomplete evidence, and the
runner's own exit status does not guarantee every scan succeeded.

These checks read metadata and utilization; they do not change AWS resources.
Alarms are candidates for review, not deletion instructions or confirmed savings.
Old snapshots may be intentional backups; absent metrics do not prove inactivity.
Management-account billing checks can include the whole organization. Region and
check coverage depend on your local configuration.

The wrapper keeps tool state inside ignored `data/`, disables basic telemetry and
automatic update checks, and keeps reports local. Package installation still
contacts upstream registries. Avoid `login`, `--share`, `--snapshot`, or Turbot
Pipes connections when reports must remain local. For interactive exploration,
run `sh thrifty.sh powerpipe server --listen local`, browse
`http://localhost:9033`, and select an individual account connection.

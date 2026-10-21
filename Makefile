-include billing.local.mk

PYTHON ?= python3
PORT ?= 8000
MONTHS ?= 6
PROVIDERS ?= aws,gcp
AWS_PROFILE ?=
AWS_METRIC ?= UnblendedCost
GCP_TABLE ?=
GCP_PROJECT ?=
GCP_LOCATION ?=
GCP_MAX_BYTES ?= 10000000000
CLAUDE_INPUT ?=
END ?=
APPEND ?=

.PHONY: all html demo extract extract-month import-claude serve open test
all: html

# Rebuild from saved snapshots. Uses sample data only when no snapshots exist.
html:
	$(PYTHON) billing.py build

# Explicit preview: never changes real provider snapshots.
demo:
	$(PYTHON) billing.py build --demo --months $(MONTHS)

# Network access happens only here. Extraction also rebuilds the HTML.
extract:
	$(PYTHON) billing.py extract --providers "$(PROVIDERS)" --months $(MONTHS) $(if $(filter 1,$(APPEND)),--append) $(if $(END),--end "$(END)") $(if $(AWS_PROFILE),--aws-profile "$(AWS_PROFILE)") --aws-metric "$(AWS_METRIC)" $(if $(GCP_TABLE),--gcp-table "$(GCP_TABLE)") $(if $(GCP_PROJECT),--gcp-project "$(GCP_PROJECT)") $(if $(GCP_LOCATION),--gcp-location "$(GCP_LOCATION)") --gcp-max-bytes $(GCP_MAX_BYTES)

# Fetch only the latest month, retaining earlier saved months.
extract-month:
	$(MAKE) extract MONTHS=1 APPEND=1

# Import explicitly supplied CSVs; no cloud requests.
import-claude:
	@test -n "$(CLAUDE_INPUT)" || (echo "Set CLAUDE_INPUT to a CSV file or directory" >&2; exit 1)
	$(PYTHON) import_claude.py "$(CLAUDE_INPUT)"

serve: html
	$(PYTHON) -m http.server $(PORT) --bind 127.0.0.1

open: html
	open cloud-cost-treemap.html

test:
	$(PYTHON) -m unittest discover -s tests -v

#!/bin/sh
# Keep Thrifty tools, caches, configuration and findings in ignored local data/.
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
task_data="$task_root/data/thrifty"
export STEAMPIPE_INSTALL_DIR="$task_data/steampipe"
export POWERPIPE_INSTALL_DIR="$task_data/powerpipe"
export STEAMPIPE_TELEMETRY=none
export POWERPIPE_TELEMETRY=none
export STEAMPIPE_UPDATE_CHECK=false
export POWERPIPE_UPDATE_CHECK=false
export CHECKPOINT_DISABLE=1
export PATH="$task_data/bin:$PATH"
case "${1:-}" in
  steampipe)
    shift
    exec "$task_data/bin/steampipe" --install-dir "$task_data/steampipe" "$@"
    ;;
  powerpipe)
    shift
    exec "$task_data/bin/powerpipe" --install-dir "$task_data/powerpipe" --mod-location "$task_data/mod" "$@"
    ;;
  *)
    echo 'Usage: sh thrifty.sh {steampipe|powerpipe} [arguments...]' >&2
    exit 2
    ;;
esac

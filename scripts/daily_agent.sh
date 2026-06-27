#!/usr/bin/env bash
# Morning research agent — logs to logs/agent.log
# Run from anywhere: ./scripts/daily_agent.sh
# Override the interpreter if needed: PYTHON=/path/to/python ./scripts/daily_agent.sh

set -euo pipefail

# Resolve the project root from this script's location, so it works on any machine.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(dirname "$SCRIPT_DIR")"
PYTHON="${PYTHON:-python3}"
LOG_DIR="$PROJECT/logs"

mkdir -p "$LOG_DIR"
cd "$PROJECT"

echo "=== $(date -Iseconds) ===" >> "$LOG_DIR/agent.log"
"$PYTHON" agent.py >> "$LOG_DIR/agent.log" 2>&1
"$PYTHON" scripts/export_site.py >> "$LOG_DIR/agent.log" 2>&1
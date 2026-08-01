#!/usr/bin/env bash
#
# Run the GetMyStuff test suite with coverage, inside the app container.
#
# The container is not a preference. The local venv is Python 3.10 and
# app/services/deep_agents/ requires >= 3.11, so a host run cannot even import
# part of the codebase — any coverage number it produced would be a lie. The
# repo is bind-mounted at /app, so reports written here land on the host.
#
# Usage:  run_coverage.sh [extra pytest args...]
# Exit code is pytest's own, so a failing suite can never be reported as a pass.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "$REPO_ROOT" || exit 1

REPORT_DIR="tests/reports"
SERVICE="app"

mkdir -p "$REPORT_DIR"

# --- Preflight -------------------------------------------------------------
if ! docker compose ps --status running --services 2>/dev/null | grep -qx "$SERVICE"; then
    echo "==> '$SERVICE' container is not running; starting it"
    docker compose up -d "$SERVICE" || {
        echo "ERROR: could not start the '$SERVICE' service." >&2
        exit 2
    }
fi

if ! docker compose exec -T "$SERVICE" python -m pytest --version >/dev/null 2>&1; then
    echo "==> pytest missing in the container; installing requirements-dev.txt"
    docker compose exec -T "$SERVICE" pip install -q -r requirements-dev.txt || {
        echo "ERROR: could not install test dependencies." >&2
        exit 2
    }
fi

# --- Run -------------------------------------------------------------------
# DATABASE_URL is forced to in-memory SQLite so the suite can never touch a real
# database. load_dotenv() does not override an already-set variable, so this
# reliably beats the committed .env.
echo "==> Running test suite"
docker compose exec -T \
    -e DATABASE_URL="sqlite+aiosqlite://" \
    -e PYTHONDONTWRITEBYTECODE=1 \
    "$SERVICE" \
    python -m pytest tests/ \
        --cov=app \
        --cov=main \
        --cov-report="json:${REPORT_DIR}/.coverage.json" \
        --cov-report="html:htmlcov" \
        --cov-report=term-missing \
        --junitxml="${REPORT_DIR}/.junit.xml" \
        -q \
        "$@"

PYTEST_EXIT=$?

if [ "$PYTEST_EXIT" -ne 0 ]; then
    echo "==> pytest exited with status ${PYTEST_EXIT} (failures are detailed in the report)"
fi

exit "$PYTEST_EXIT"

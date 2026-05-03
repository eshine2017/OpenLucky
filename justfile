set shell := ["bash", "-uc"]
set dotenv-load

python  := ".venv/bin/python3"
service := "openlucky"

# Show available commands
[private]
default:
    @just --list

# ── Environment ────────────────────────────────────────────────────────────────

# Create virtualenv and install all dependencies
venv:
    [ -d .venv ] || python3 -m venv .venv
    {{python}} -m pip install --upgrade pip
    {{python}} -m pip install -r requirements.txt -r requirements-dev.txt

# Fail fast with a clear message if .venv is missing
[private]
venv-check:
    @[ -f {{python}} ] || (echo "ERROR: .venv not found — run 'just venv' first" && exit 1)

# Remove all cache and build artifacts
clean:
    find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache -o -name .ruff_cache -o -name "*.egg-info" \) -exec rm -rf {} + 2>/dev/null || true

# ── Quality ────────────────────────────────────────────────────────────────────

# Run linter
lint: venv-check
    {{python}} -m ruff check app/ tests/

# Format code and auto-fix lint issues
format: venv-check
    {{python}} -m ruff check --fix app/ tests/
    {{python}} -m ruff format app/ tests/

alias fmt := format

# Run type checker
typecheck: venv-check
    {{python}} -m mypy app/

# ── Tests ──────────────────────────────────────────────────────────────────────

# Run tests
test: venv-check
    {{python}} -m pytest

# Run a specific test file: just test-file tests/test_bootstrap.py
test-file file: venv-check
    {{python}} -m pytest {{file}} -v

# Run tests with coverage (fails below 80%)
test-cov: venv-check
    {{python}} -m pytest --cov=app --cov-report=term-missing --cov-fail-under=80

# ── CI ─────────────────────────────────────────────────────────────────────────

# Run all checks: lint + typecheck + tests with coverage (mirrors CI)
ci: lint typecheck test-cov

# ── Dev service ────────────────────────────────────────────────────────────────

# Start dev service in foreground
dev: venv-check
    CONFIG_FILE=config/settings.dev.yaml {{python}} -m app.main

# Clear dev runtime state (db, jobs, logs, workspace) and recreate dir structure
dev-reset:
    rm -rf data-dev/jobs data-dev/logs data-dev/workspace data-dev/app.db
    mkdir -p data-dev/jobs data-dev/logs data-dev/workspace
    @echo "Dev state cleared."

# Open a SQLite shell on the dev database
db-shell:
    sqlite3 data-dev/app.db

# ── Prod service (systemd) ─────────────────────────────────────────────────────

# Install and enable the systemd service
service-install:
    sudo cp openlucky.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable {{service}}
    sudo systemctl start {{service}}
    @echo "Service installed and started."

# Restart the service after code changes
service-reload:
    sudo systemctl daemon-reload
    sudo systemctl restart {{service}}

# Stop the service
service-stop:
    sudo systemctl stop {{service}}

# Disable and remove the systemd service
service-uninstall:
    sudo systemctl stop {{service}} || true
    sudo systemctl disable {{service}} || true
    sudo rm -f /etc/systemd/system/{{service}}.service
    sudo systemctl daemon-reload
    @echo "Service uninstalled."

# Show service status
service-status:
    systemctl status {{service}}

# Follow live service logs
service-logs:
    journalctl -u {{service}} -f

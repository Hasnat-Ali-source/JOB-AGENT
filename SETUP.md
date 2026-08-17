# Phase 0 — Environment & Infrastructure Setup Guide

**Status:** Ready for implementation  
**Estimated Duration:** 1–2 hours (fully automated by `setup.py`)

## Overview

Phase 0 sets up the local development environment with all dependencies, database, and configuration needed to begin Phase 1 (Session Manager).

This includes:
- Python 3.11+ virtual environment
- Playwright + browser binaries
- SQLite database initialization
- macOS Keychain integration test
- Profile directories for platform authentication
- Configuration files

## Prerequisites

- **macOS** 10.15 or later (Catalina+)
- **Python 3.11+** (check with `python3 --version`)
- **Git** (for cloning the repo)
- **Xcode Command Line Tools** (for compilation): `xcode-select --install`
- **macOS Keychain** (standard on all Macs, no setup needed)

## Automated Setup

The easiest way to set up Phase 0:

```bash
cd /path/to/JOB-AGENT
python3 scripts/setup.py
```

This script will:
1. ✓ Verify Python 3.11+
2. ✓ Verify macOS
3. ✓ Create `.venv/` virtual environment
4. ✓ Install all dependencies from `requirements.txt`
5. ✓ Install Playwright browser binaries (chromium, firefox)
6. ✓ Create profile directories
7. ✓ Test Keychain access
8. ✓ Initialize SQLite database
9. ✓ Print success or errors

**Exit codes:**
- `0` = All steps passed, ready for Phase 1
- `1` = One or more steps failed, check output for details

## Manual Setup (if automated script fails)

### 1. Create Virtual Environment

```bash
cd /path/to/JOB-AGENT
python3 -m venv .venv
source .venv/bin/activate
```

You should see `(.venv)` in your shell prompt.

Python 3.11+ is required; the pinned dependency set is verified on 3.14.

### 2. Upgrade pip

```bash
pip install --upgrade pip
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

For tests and linting, add the dev set:

```bash
pip install -r requirements-dev.txt
```

This installs:
- `fastapi` + `uvicorn` (web dashboard)
- `sqlmodel` + `sqlalchemy` (database ORM)
- `playwright` (browser automation)
- `keyring` (macOS Keychain integration)
- `rapidfuzz` (job deduplication)
- `ollama` (optional, for local LLM)
- `python-docx`, `pypdf`, `weasyprint`, `reportlab` (document generation, Phase 4)
- And ~15 others (see `requirements.txt`)

### 3b. Install PDF system libraries (recommended)

WeasyPrint is the preferred PDF engine and needs native libraries:

```bash
brew install pango cairo gdk-pixbuf libffi
```

Without them the agent falls back to ReportLab automatically — PDFs still generate, with
slightly plainer typography. Nothing breaks either way.

### 4. Install Playwright Browsers

```bash
playwright install chromium firefox
```

This downloads the browser binaries (300–400 MB total). Required only once.

### 5. Create Profile Directories

```bash
mkdir -p ~/Library/Application\ Support/job-agent/profiles
```

This is where Playwright persistent browser contexts will store profile data per platform.

### 6. Test Keychain Access

```bash
python3 -c "from job_agent.utils import test_keychain_access; test_keychain_access()"
```

Expected output:
```
2026-08-14 14:32:15,123 [INFO] job_agent.utils.keychain: Keychain access test passed
```

If this fails, check that:
- You're on macOS (not Linux/Windows)
- Keychain is not locked
- Try unlocking Keychain manually (Keychain Access app)

### 7. Initialize Database

```bash
python3 scripts/init_db.py
```

Expected output:
```
2026-08-14 14:32:15 [INFO] Initializing database at: /Users/user/Library/Application Support/job-agent/job_agent.db
...
✓ Phase 0 Initialization Complete!
```

This creates:
- SQLite database: `~/Library/Application Support/job-agent/job_agent.db`
- Tables: `search_profiles`, `platform_accounts`, `jobs`, `applications`, `audit_log`, `email_threads`

## Verification Checklist

After setup, verify everything is working:

```bash
# 1. Verify venv is active
echo $VIRTUAL_ENV
# Output: /path/to/JOB-AGENT/.venv

# 2. Verify Python version
python --version
# Output: Python 3.11.x or higher

# 3. Verify database exists
ls -la ~/Library/Application\ Support/job-agent/job_agent.db
# Output: (file should exist and be non-empty)

# 4. Verify Playwright is installed
playwright --version
# Output: Version x.y.z

# 5. Test importing job_agent
python -c "from job_agent.models import SearchProfile; print('✓ job_agent imports OK')"

# 6. Test CLI
python -m job_agent version
# Output: Job Agent v0.1.0
```

## Configuration

### .env File (Optional)

Copy `.env.example` to `.env` and customize settings:

```bash
cp .env.example .env
# Edit .env with your preferred settings
```

Key settings:
- `USE_OLLAMA=true` — Use local Ollama for fit scoring (default)
- `OLLAMA_URL=http://localhost:11434` — Ollama server location
- `DASHBOARD_PORT=8000` — Dashboard port (change if 8000 is in use)
- `LOG_LEVEL=INFO` — Logging verbosity

If you don't create `.env`, all defaults are used.

### Ollama Setup (Optional, for Phase 4+)

If you want to use local LLM for job fit scoring:

1. **Install Ollama:** https://ollama.ai/download
2. **Start Ollama:** `ollama serve`
3. **Pull a model:** `ollama pull llama2` (or `ollama pull mistral`, etc.)
4. **Test:** `curl http://localhost:11434/api/tags`

Alternatively, uncomment `ANTHROPIC_API_KEY` in `.env` to use Claude API (requires paid account, but higher quality).

## Directory Structure After Setup

```
~/Library/Application Support/job-agent/
├── job_agent.db                  # SQLite database
└── profiles/
    ├── linkedin/                 # Platform profiles (created later in Phase 1)
    ├── greenhouse/
    ├── indeed/
    └── [others...]
```

```
JOB-AGENT/
├── .venv/                        # Python virtual environment
├── job_agent/                    # Package root
│   ├── models/                   # Database models ✓
│   ├── utils/                    # Keychain, browser utils ✓
│   ├── connectors/               # (Phase 2+)
│   ├── core/                     # Orchestrator, session mgr (Phase 1+)
│   ├── dashboard/                # FastAPI app (Phase 10+)
│   └── config.py                 # Settings ✓
├── scripts/
│   ├── init_db.py                # Database setup ✓
│   ├── setup.py                  # Full Phase 0 setup ✓
│   └── [Phase 1+...]
├── requirements.txt              # Python dependencies ✓
├── .env.example                  # Config template ✓
├── .gitignore                    # Git ignore rules ✓
└── README.md                     # Project overview ✓
```

## Troubleshooting

### Python version error: "Python 3.11+ required"

Install Python 3.11+ from:
- **macOS official:** https://www.python.org/downloads/
- **Homebrew:** `brew install python@3.11`
- **Conda:** `conda install python=3.11`

Then run setup again.

### "pip: command not found" or "ModuleNotFoundError: No module named pip"

Re-create the venv:

```bash
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Playwright browser installation fails

Try:

```bash
playwright install chromium --with-deps
```

Or manually:

```bash
python -m playwright install
```

If still failing, check network connectivity or try behind a proxy.

### Keychain access test fails

```bash
# Unlock Keychain via GUI
open /Applications/Utilities/Keychain\ Access.app
# (Keychain → Unlock Keychain)

# Or from CLI
security unlock-keychain ~/Library/Keychains/login.keychain-db
```

Then re-run test.

### Database initialization fails

Check file permissions:

```bash
mkdir -p ~/Library/Application\ Support/job-agent
chmod 755 ~/Library/Application\ Support/job-agent
```

Then re-run:

```bash
python scripts/init_db.py
```

### "address already in use" when starting dashboard

The default port 8000 is in use. Either:

1. Kill the process using port 8000:
   ```bash
   lsof -i :8000 | grep -v PID | awk '{print $2}' | xargs kill -9
   ```

2. Or use a different port:
   ```bash
   python -m job_agent dashboard --port 8001
   ```

## Next Steps

Once Phase 0 is complete, proceed to **Phase 1: Session Manager**.

See [PHASES.md](PHASES.md#phase-1-session-manager--account-connection) for Phase 1 setup.

## Useful Commands Reference

```bash
# Activate venv
source .venv/bin/activate

# Run Phase 0 setup
python scripts/setup.py

# Initialize or reset database
python scripts/init_db.py

# Start dashboard (Phase 10+)
python -m job_agent dashboard

# Run job pipeline (Phase 8+)
python -m job_agent run --profile "Senior Backend"

# Check version
python -m job_agent version

# Help
python -m job_agent --help

# Python shell with job_agent available
python -c "from job_agent.models import *; print('ready')"
```

## Support & Issues

If you encounter issues:

1. Check this guide's Troubleshooting section
2. Check [progress.md](progress.md) for current Phase status
3. Review [PHASES.md](PHASES.md) for architecture details
4. Look at logs: `tail -f /path/to/log/file` (if LOG_FILE is set in .env)

## What's Included in Phase 0

✓ **Python environment** — .venv, dependencies, Playwright  
✓ **Database** — SQLite schema with 6 tables, alembic-ready migrations  
✓ **Security** — Keychain integration for non-sensitive metadata  
✓ **Configuration** — Settings module + .env template  
✓ **CLI** — Entry point with `init-db`, `dashboard`, `run`, `version` commands  
✓ **Dashboard stub** — FastAPI app with `/health` and `/api/v1/status` endpoints (GUI in Phase 10)  

## What's NOT in Phase 0

✗ Connector implementations (Phase 2+)  
✗ Job search pipeline (Phase 3)  
✗ Browser session management (Phase 1)  
✗ Application filling/submission (Phase 5–6)  
✗ Email integration (Phase 6b)  
✗ Full dashboard UI (Phase 10)  

---

**Phase 0 is complete once `setup.py` exits with code 0 or all steps in the verification checklist pass.**

Ready for Phase 1! 🚀

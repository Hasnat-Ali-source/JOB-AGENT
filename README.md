# Local Job Search & Application Agent

A fully local, macOS-based agent for searching jobs and managing applications across multiple platforms, with zero cloud dependencies or password storage.

## Project Overview

This agent provides:
- **Multi-platform job search** (LinkedIn, Indeed, Greenhouse, etc.) with local deduplication
- **Automated application filling** with mandatory human review gates
- **Local LLM-powered fit scoring** (Ollama) or API-based (Claude)
- **Secure credential handling** via macOS Keychain (no passwords stored)
- **Email-based application support** via Mail.app or IMAP/SMTP
- **Dashboard GUI** for account management, review queues, and job history
- **Audit logging** and automated rate-limiting

## Quick Start

```bash
# 1. Set up environment
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Initialize database
python scripts/init_db.py

# 3. Start the dashboard
python -m job_agent.dashboard

# 4. Visit http://localhost:8000
```

## Architecture

- **Backend**: Python 3.11+ with FastAPI + SQLModel
- **Browser Automation**: Playwright (persistent contexts per platform)
- **Local DB**: SQLite (jobs, applications, audit log)
- **Secrets**: macOS Keychain via `keyring` package
- **LLM Scoring**: Ollama or Anthropic API
- **Email**: Mail.app AppleScript + IMAP/SMTP fallback
- **Scheduling**: macOS launchd
- **GUI**: FastAPI + HTMX/React frontend

## Key Design Constraints

✓ No passwords stored or logged  
✓ No CAPTCHA/MFA bypass  
✓ Manual review gate for first N submissions per platform  
✓ Consumer boards default to read-only (Search & analyze)  
✓ ATS-hosted forms can auto-submit after proving clean  
✓ Paced, human-like automation with randomized delays  
✓ All actions logged to audit_log  

## Phases

See [PHASES.md](PHASES.md) for detailed build roadmap (11 phases total).

## Project Structure

```
job_agent/
├── connectors/           # Platform-specific connectors
│   ├── base.py          # ConnectedPlatformConnector base class
│   ├── generic_ats.py   # Generic ATS/career site fallback
│   └── [platform].py    # LinkedIn, Indeed, Greenhouse, etc.
├── core/
│   ├── session_manager.py
│   ├── orchestrator.py
│   ├── document_gen.py
│   └── fit_scorer.py
├── models/
│   └── database.py       # SQLModel schema
├── dashboard/
│   └── main.py          # FastAPI app + routes
├── utils/
│   ├── keychain.py
│   ├── browser.py
│   └── dedup.py
└── scripts/
    ├── init_db.py
    └── [helpers]
```

## Status & Next Steps

- Phase 0: Environment setup (in progress)
- Phase 1: Session manager
- Phase 2–11: Connectors, pipeline, automation, scheduling

See [progress.md](progress.md) for detailed phase tracking.

"""
Package entry point.

Makes `python -m job_agent ...` work, which is how the launchd job invokes a
scheduled run (see core/scheduler.py). Without this the scheduled command
fails with "No module named job_agent.__main__" — and it would fail silently,
in a log file, at 9 AM.
"""

from job_agent.main import cli

if __name__ == "__main__":
    cli()

"""
Main CLI entry point for Job Agent.

Usage:
    python -m job_agent init-db       # Initialize database
    python -m job_agent dashboard      # Start web dashboard
    python -m job_agent run            # Run job search & application pipeline
"""

import sys
import logging
import click

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


@click.group()
def cli():
    """Job Agent — Local macOS job search and application automation."""
    pass


@cli.command()
def init_db():
    """Initialize the SQLite database."""
    from scripts.init_db import main as init_db_main
    sys.exit(init_db_main())


@cli.command()
@click.option(
    "--host",
    default="127.0.0.1",
    help="Host to bind to (default: 127.0.0.1)"
)
@click.option(
    "--port",
    default=8000,
    type=int,
    help="Port to bind to (default: 8000)"
)
@click.option(
    "--reload",
    is_flag=True,
    help="Enable auto-reload on code changes (development only)"
)
def dashboard(host: str, port: int, reload: bool):
    """Start the web dashboard (FastAPI)."""
    try:
        import uvicorn
        from job_agent.dashboard.main import app
        
        logger.info(f"Starting dashboard on {host}:{port}")
        logger.info(f"Open http://{host}:{port} in your browser")
        
        uvicorn.run(
            app,
            host=host,
            port=port,
            reload=reload,
            log_level="info"
        )
    except ImportError:
        logger.error("FastAPI/Uvicorn not installed. Run: pip install fastapi uvicorn")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to start dashboard: {e}")
        sys.exit(1)


@cli.command()
@click.option(
    "--platform",
    multiple=True,
    help="Platform(s) to run search on (can specify multiple times)"
)
@click.option(
    "--profile",
    help="Search profile name to use (defaults to the first active profile)"
)
@click.option(
    "--documents",
    is_flag=True,
    help="Also tailor documents for jobs that pass the filters"
)
@click.option(
    "--queue",
    is_flag=True,
    help="Also fill application forms and queue them for review (never submits)"
)
@click.option(
    "--scheduled",
    is_flag=True,
    help="Mark this run as scheduled (used by the launchd job)"
)
def run(platform: tuple, profile: str, documents: bool, queue: bool, scheduled: bool):
    """
    Run the job search pipeline across connected platforms.

    Never submits applications — a run prepares work and stops. Submission is
    a separate, user-initiated act through the review queue.
    """
    import asyncio

    from job_agent.core.orchestrator import RunOrchestrator
    from job_agent.dashboard.deps import get_engine
    from job_agent.models.database import SearchProfile
    from sqlmodel import Session

    async def main() -> int:
        with Session(get_engine()) as session:
            query = session.query(SearchProfile).filter(SearchProfile.is_active == True)  # noqa: E712

            if profile:
                query = query.filter(SearchProfile.name == profile)

            search_profile = query.order_by(SearchProfile.created_at).first()

            if not search_profile:
                logger.error(
                    f"No active search profile{f' named {profile!r}' if profile else ''} — "
                    f"create one before running"
                )
                return 1

            logger.info(f"Running profile '{search_profile.name}'")

            agent_run = await RunOrchestrator(session).run(
                search_profile,
                platforms=list(platform) or None,
                trigger="scheduled" if scheduled else "manual",
                generate_documents=documents,
                queue_applications=queue,
            )

            click.echo(RunOrchestrator.summarize(agent_run))

            for note in agent_run.interruptions:
                click.echo(f"  ! {note['kind']}: {note['guidance']}")

            for error in agent_run.errors:
                click.echo(f"  ! {error}")

            return 0 if not agent_run.errors else 2

    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        logger.info("Run cancelled")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Run failed: {e}")
        sys.exit(1)


@cli.group()
def schedule():
    """Manage scheduled runs (launchd)."""


@schedule.command("install")
@click.argument("times", nargs=-1, required=True)
@click.option("--profile", help="Search profile the scheduled run should use")
def schedule_install(times: tuple, profile: str):
    """
    Install a schedule, without enabling it.

    Example: job_agent schedule install 09:00 18:00
    """
    from job_agent.core.scheduler import LaunchdScheduler, ScheduleTime

    try:
        parsed = [ScheduleTime.parse(t) for t in times]
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    scheduler = LaunchdScheduler()
    path = scheduler.install(parsed, profile_name=profile)

    click.echo(f"Installed {path}")
    click.echo(f"  Times: {', '.join(str(t) for t in parsed)}")
    click.echo("  Not yet enabled — run: python -m job_agent schedule enable")


@schedule.command("enable")
def schedule_enable():
    """Start running on the installed schedule."""
    from job_agent.core.scheduler import LaunchdScheduler

    success, message = LaunchdScheduler().enable()
    click.echo(message)
    sys.exit(0 if success else 1)


@schedule.command("disable")
def schedule_disable():
    """Stop running on schedule, keeping the schedule installed."""
    from job_agent.core.scheduler import LaunchdScheduler

    success, message = LaunchdScheduler().disable()
    click.echo(message)
    sys.exit(0 if success else 1)


@schedule.command("status")
def schedule_status():
    """Show whether runs are installed and enabled."""
    from job_agent.core.scheduler import LaunchdScheduler

    status = LaunchdScheduler().status()

    click.echo(f"Installed: {status['installed']}")
    click.echo(f"Enabled:   {status['enabled']}")

    if status["times"]:
        click.echo(f"Times:     {', '.join(status['times'])}")
    if status["profile"]:
        click.echo(f"Profile:   {status['profile']}")

    click.echo(f"Plist:     {status['plist_path']}")
    click.echo(f"Logs:      {status['log_dir']}")


@cli.command()
def version():
    """Show version information."""
    from job_agent import __version__
    click.echo(f"Job Agent v{__version__}")


if __name__ == "__main__":
    cli()

"""
launchd Scheduler (Phase 8).

Writes, loads and unloads the launchd agent that runs the job search on a
schedule.

**Nothing is scheduled without an explicit action.** `install()` writes the
plist but does not load it; `enable()` is a separate call. An agent that
started applying for jobs because a config file appeared would be a bad
surprise, so the plist existing and the plist running are two different states,
and `status()` reports both.

The generated job runs `python -m job_agent run --scheduled`, which performs
one pass and exits. It does not submit applications — see RunOrchestrator.
"""

import logging
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

LABEL = "net.jobagent.scheduler"
PLIST_NAME = f"{LABEL}.plist"


@dataclass
class ScheduleTime:
    """A time of day to run at."""

    hour: int
    minute: int = 0

    def __post_init__(self):
        if not 0 <= self.hour <= 23:
            raise ValueError(f"hour must be 0-23, got {self.hour}")
        if not 0 <= self.minute <= 59:
            raise ValueError(f"minute must be 0-59, got {self.minute}")

    def to_dict(self) -> dict:
        """launchd's StartCalendarInterval entry."""
        return {"Hour": self.hour, "Minute": self.minute}

    def __str__(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    @classmethod
    def parse(cls, value: str) -> "ScheduleTime":
        """
        Parse "HH:MM" or "HH".

        Args:
            value: Time string

        Returns:
            ScheduleTime

        Raises:
            ValueError: If the string isn't a time
        """
        parts = value.strip().split(":")

        try:
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            raise ValueError(f"'{value}' is not a time — use HH:MM")

        return cls(hour=hour, minute=minute)


class LaunchdScheduler:
    """Manages the launchd agent for scheduled runs."""

    def __init__(
        self,
        label: str = LABEL,
        agents_dir: Optional[Path] = None,
        project_dir: Optional[Path] = None,
    ):
        """
        Initialize the scheduler.

        Args:
            label: launchd job label
            agents_dir: LaunchAgents directory (tests point this at a temp dir)
            project_dir: Project root the job runs from
        """
        self.label = label
        self.agents_dir = agents_dir or (Path.home() / "Library" / "LaunchAgents")
        self.project_dir = project_dir or Path(__file__).resolve().parent.parent.parent

    @property
    def plist_path(self) -> Path:
        """Where the plist lives."""
        return self.agents_dir / f"{self.label}.plist"

    @property
    def log_dir(self) -> Path:
        """Where scheduled runs write their output."""
        return Path.home() / "Library" / "Logs" / "job-agent"

    # ------------------------------------------------------------------
    # Plist
    # ------------------------------------------------------------------

    def build_plist(
        self,
        times: List[ScheduleTime],
        profile_name: Optional[str] = None,
        python_executable: Optional[str] = None,
    ) -> dict:
        """
        Build the launchd job definition.

        Args:
            times: Times of day to run
            profile_name: Search profile to use
            python_executable: Interpreter to run with (defaults to the current
                one, so a venv install keeps working)

        Returns:
            The plist as a dict

        Raises:
            ValueError: If no times were given
        """
        if not times:
            raise ValueError("A schedule needs at least one run time")

        arguments = [
            python_executable or sys.executable,
            "-m", "job_agent", "run", "--scheduled",
        ]

        if profile_name:
            arguments += ["--profile", profile_name]

        return {
            "Label": self.label,
            "ProgramArguments": arguments,
            "WorkingDirectory": str(self.project_dir),
            "StartCalendarInterval": [t.to_dict() for t in times],
            # Don't fire a missed run the moment the Mac wakes up: the user may
            # be mid-something, and a browser window opening unprompted is
            # exactly the surprise this project avoids elsewhere.
            "RunAtLoad": False,
            "StandardOutPath": str(self.log_dir / "scheduled.out.log"),
            "StandardErrorPath": str(self.log_dir / "scheduled.err.log"),
            "ProcessType": "Background",
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        }

    def install(
        self,
        times: List[ScheduleTime],
        profile_name: Optional[str] = None,
        python_executable: Optional[str] = None,
    ) -> Path:
        """
        Write the plist, without loading it.

        Args:
            times: Times of day to run
            profile_name: Search profile to use
            python_executable: Interpreter to run with

        Returns:
            Path to the written plist
        """
        plist = self.build_plist(times, profile_name, python_executable)

        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        with open(self.plist_path, "wb") as handle:
            plistlib.dump(plist, handle)

        logger.info(
            f"Wrote {self.plist_path} for {', '.join(str(t) for t in times)} "
            f"(not yet enabled)"
        )

        return self.plist_path

    def read_plist(self) -> Optional[dict]:
        """
        Read the installed plist.

        Returns:
            The plist dict, or None if it isn't installed
        """
        if not self.plist_path.exists():
            return None

        try:
            with open(self.plist_path, "rb") as handle:
                return plistlib.load(handle)
        except Exception as e:
            logger.error(f"Could not read {self.plist_path}: {e}")
            return None

    def scheduled_times(self) -> List[ScheduleTime]:
        """
        The times the installed job runs at.

        Returns:
            Schedule times, empty when nothing is installed
        """
        plist = self.read_plist()

        if not plist:
            return []

        return [
            ScheduleTime(hour=entry.get("Hour", 0), minute=entry.get("Minute", 0))
            for entry in plist.get("StartCalendarInterval", [])
        ]

    # ------------------------------------------------------------------
    # launchctl
    # ------------------------------------------------------------------

    def enable(self) -> Tuple[bool, str]:
        """
        Load the job so it starts running on schedule.

        Returns:
            (success, message)
        """
        if not self.plist_path.exists():
            return False, "No schedule is installed — install one first"

        success, output = self._launchctl(["load", "-w", str(self.plist_path)])

        if success:
            logger.info(f"Enabled scheduled runs ({self.label})")
            return True, "Scheduled runs enabled"

        return False, f"launchctl load failed: {output}"

    def disable(self) -> Tuple[bool, str]:
        """
        Unload the job so it stops running, leaving the plist in place.

        Returns:
            (success, message)
        """
        if not self.plist_path.exists():
            return True, "No schedule is installed"

        success, output = self._launchctl(["unload", "-w", str(self.plist_path)])

        if success:
            logger.info(f"Disabled scheduled runs ({self.label})")
            return True, "Scheduled runs disabled"

        return False, f"launchctl unload failed: {output}"

    def uninstall(self) -> Tuple[bool, str]:
        """
        Unload the job and delete the plist.

        Returns:
            (success, message)
        """
        self.disable()

        if self.plist_path.exists():
            self.plist_path.unlink()
            return True, f"Removed {self.plist_path}"

        return True, "Nothing was installed"

    def is_loaded(self) -> bool:
        """
        Whether launchd currently has the job loaded.

        Returns:
            True if loaded
        """
        success, output = self._launchctl(["list"])

        if not success:
            return False

        return any(self.label in line for line in output.splitlines())

    def status(self) -> dict:
        """
        Report both states: installed, and actually running on schedule.

        Returns:
            Schedule status
        """
        plist = self.read_plist()
        times = self.scheduled_times()

        return {
            "installed": plist is not None,
            "enabled": self.is_loaded() if plist else False,
            "plist_path": str(self.plist_path),
            "label": self.label,
            "times": [str(t) for t in times],
            "profile": self._profile_from(plist),
            "log_dir": str(self.log_dir),
            "runs_at_load": bool(plist.get("RunAtLoad")) if plist else False,
        }

    @staticmethod
    def _profile_from(plist: Optional[dict]) -> Optional[str]:
        """Extract the --profile argument from a plist, if present."""
        if not plist:
            return None

        arguments = plist.get("ProgramArguments", [])

        if "--profile" in arguments:
            index = arguments.index("--profile")
            if index + 1 < len(arguments):
                return arguments[index + 1]

        return None

    @staticmethod
    def _launchctl(arguments: List[str]) -> Tuple[bool, str]:
        """
        Run a launchctl command.

        Args:
            arguments: Arguments after "launchctl"

        Returns:
            (success, combined output)
        """
        try:
            completed = subprocess.run(
                ["launchctl", *arguments],
                capture_output=True, text=True, timeout=30,
            )

            output = (completed.stdout or "") + (completed.stderr or "")

            return completed.returncode == 0, output.strip()

        except FileNotFoundError:
            return False, "launchctl not found — scheduling is macOS-only"
        except subprocess.TimeoutExpired:
            return False, "launchctl did not respond within 30s"
        except Exception as e:
            return False, str(e)

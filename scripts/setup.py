#!/usr/bin/env python3
"""
Phase 0 Setup Script for Job Agent

Comprehensive one-time setup that:
1. Checks Python version
2. Creates venv
3. Installs dependencies
4. Installs Playwright browsers
5. Initializes database
6. Tests Keychain
7. Creates profile directories
8. Runs smoke tests

Run this after cloning the repository:
    chmod +x scripts/setup.sh
    ./scripts/setup.sh

Or directly:
    python scripts/setup.py
"""

import sys
import subprocess
import logging
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def check_python_version() -> bool:
    """Verify Python 3.11+ is installed."""
    if sys.version_info < (3, 11):
        logger.error(f"✗ Python 3.11+ required, got {sys.version}")
        return False
    logger.info(f"✓ Python {sys.version.split()[0]} OK")
    return True


def check_macos() -> bool:
    """Verify we're on macOS."""
    if sys.platform != "darwin":
        logger.error(f"✗ macOS required, got {sys.platform}")
        return False
    logger.info(f"✓ macOS detected ({sys.platform})")
    return True


# Standard virtualenv location, matching SETUP.md and .gitignore
VENV_DIR = ".venv"


def create_venv() -> bool:
    """Create Python virtual environment."""
    venv_path = Path(VENV_DIR)
    
    if venv_path.exists():
        logger.info(f"✓ venv already exists at {venv_path}")
        return True
    
    try:
        logger.info("Creating venv...")
        subprocess.run([sys.executable, "-m", "venv", VENV_DIR], check=True)
        logger.info(f"✓ venv created at {venv_path}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"✗ Failed to create venv: {e}")
        return False


def get_venv_python() -> str:
    """Get path to Python in venv."""
    venv_python = Path(VENV_DIR) / "bin" / "python"
    if not venv_python.exists():
        # Fallback to current Python if venv not created yet
        return sys.executable
    return str(venv_python)


def install_dependencies() -> bool:
    """Install Python dependencies from requirements.txt."""
    try:
        python_exe = get_venv_python()
        logger.info("Installing dependencies from requirements.txt...")
        subprocess.run(
            [python_exe, "-m", "pip", "install", "--upgrade", "pip"],
            check=True,
            capture_output=True
        )
        subprocess.run(
            [python_exe, "-m", "pip", "install", "-r", "requirements.txt"],
            check=True
        )
        logger.info("✓ Dependencies installed")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"✗ Failed to install dependencies: {e}")
        return False


def install_playwright_browsers() -> bool:
    """Install Playwright browser binaries."""
    try:
        python_exe = get_venv_python()
        logger.info("Installing Playwright browsers (chromium, firefox)...")
        subprocess.run(
            [python_exe, "-m", "playwright", "install"],
            check=True
        )
        logger.info("✓ Playwright browsers installed")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"✗ Failed to install Playwright browsers: {e}")
        return False


def setup_directories() -> bool:
    """Create necessary directories."""
    try:
        app_support = Path.home() / "Library" / "Application Support" / "job-agent"
        profiles_dir = app_support / "profiles"
        
        app_support.mkdir(parents=True, exist_ok=True)
        profiles_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"✓ Directories created at {app_support}")
        return True
    except Exception as e:
        logger.error(f"✗ Failed to create directories: {e}")
        return False


def test_keychain() -> bool:
    """Test Keychain access."""
    try:
        python_exe = get_venv_python()
        result = subprocess.run(
            [
                python_exe,
                "-c",
                "from job_agent.utils import test_keychain_access; import sys; sys.exit(0 if test_keychain_access() else 1)"
            ],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            logger.info("✓ Keychain access test passed")
            return True
        else:
            logger.error("✗ Keychain access test failed")
            logger.error(result.stderr)
            return False
    except Exception as e:
        logger.error(f"✗ Keychain test failed: {e}")
        return False


def init_database() -> bool:
    """Initialize SQLite database."""
    try:
        python_exe = get_venv_python()
        result = subprocess.run(
            [python_exe, "scripts/init_db.py"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            logger.info(result.stdout)
            return True
        else:
            logger.error("✗ Database initialization failed")
            logger.error(result.stderr)
            return False
    except Exception as e:
        logger.error(f"✗ Database initialization failed: {e}")
        return False


def main():
    """Run Phase 0 setup."""
    logger.info("=" * 70)
    logger.info("Job Agent — Phase 0 Complete Setup")
    logger.info("=" * 70)
    
    steps = [
        ("Platform check (macOS)", check_macos),
        ("Python version check", check_python_version),
        ("Create virtual environment", create_venv),
        ("Install dependencies", install_dependencies),
        ("Install Playwright browsers", install_playwright_browsers),
        ("Setup directories", setup_directories),
        ("Test Keychain access", test_keychain),
        ("Initialize database", init_database),
    ]
    
    failed = []
    for i, (step_name, step_func) in enumerate(steps, 1):
        logger.info(f"\n[{i}/{len(steps)}] {step_name}...")
        try:
            if not step_func():
                failed.append(step_name)
                logger.error(f"✗ FAILED: {step_name}")
                # Continue to next step instead of aborting
        except Exception as e:
            failed.append(step_name)
            logger.error(f"✗ FAILED: {step_name} — {e}")
    
    # Summary
    logger.info("\n" + "=" * 70)
    if not failed:
        logger.info("✓ SETUP COMPLETE — All steps passed!")
        logger.info("=" * 70)
        logger.info("\nNext steps:")
        logger.info("  1. Activate venv: source venv/bin/activate")
        logger.info("  2. Start dashboard: python -m job_agent dashboard")
        logger.info("  3. Or run: python -m job_agent --help")
        logger.info("\nPhase 0 is complete. Ready to start Phase 1: Session Manager.")
        return 0
    else:
        logger.error(f"✗ SETUP INCOMPLETE — {len(failed)} step(s) failed:")
        for step in failed:
            logger.error(f"  - {step}")
        logger.info("\nPlease fix the above errors and re-run this script.")
        return 1


if __name__ == "__main__":
    sys.exit(main())

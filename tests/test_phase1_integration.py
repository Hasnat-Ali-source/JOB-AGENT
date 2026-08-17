#!/usr/bin/env python3
"""
Integration tests for Phase 1: Session Manager & Account Connection.

Tests the complete flow:
1. SessionManager initializes Playwright
2. Browser launches for connection (test_connector)
3. User simulates login
4. Agent detects authenticated state
5. Connection metadata saved to Keychain + database
6. Disconnect workflow works (cleanup)

Usage:
    pytest tests/test_phase1_integration.py -v
    
    Or run specific test:
    pytest tests/test_phase1_integration.py::test_session_manager_init -v
"""

import time

import pytest

from job_agent.core.session_manager import SessionManager, get_session_manager, close_session_manager
from job_agent.utils.keychain import store_platform_metadata, retrieve_platform_metadata, delete_platform_metadata
from job_agent.config import settings
from job_agent.utils.dates import utcnow


class TestSessionManagerInit:
    """Test SessionManager initialization."""
    
    def test_profile_root_directory_exists(self):
        """Test that profile root directory is created."""
        manager = SessionManager()
        root = manager.profile_root
        
        assert root.exists(), f"Profile root should exist: {root}"
        assert root.is_dir(), f"Profile root should be a directory: {root}"
    
    def test_get_profile_dir_creates_platform_dir(self):
        """Test that platform profile directories are created."""
        manager = SessionManager()
        profile_dir = manager.get_profile_dir("test_connector")
        
        assert profile_dir.exists(), f"Profile dir should exist: {profile_dir}"
        assert profile_dir.is_dir(), f"Profile dir should be a directory: {profile_dir}"
        assert profile_dir.name == "test_connector"


class TestSessionManagerPlaywright:
    """Test Playwright lifecycle in SessionManager."""
    
    @pytest.mark.asyncio
    async def test_start_playwright(self):
        """Test that Playwright starts correctly."""
        manager = SessionManager()
        
        try:
            await manager.start_playwright()
            
            assert manager.playwright_instance is not None, "Playwright instance should be initialized"
        finally:
            await manager.stop_playwright()
    
    @pytest.mark.asyncio
    async def test_stop_playwright(self):
        """Test that Playwright stops correctly."""
        manager = SessionManager()
        
        await manager.start_playwright()
        assert manager.playwright_instance is not None
        
        await manager.stop_playwright()
        assert manager.playwright_instance is None, "Playwright instance should be None after stop"


class TestSessionManagerConnectionDetection:
    """Test session status detection."""
    
    @pytest.mark.asyncio
    async def test_check_session_status_no_context(self):
        """Test that status detection returns NEEDS_SIGNIN when no context exists."""
        manager = SessionManager()
        
        from job_agent.models import ConnectionStatus
        status = await manager.check_session_status("nonexistent_platform")
        
        assert status == ConnectionStatus.NEEDS_SIGNIN, "Should return NEEDS_SIGNIN for nonexistent context"


class TestPersistentSessions:
    """
    The sign-in must survive the process that created it.

    These launch a real browser, because the bug they exist to catch was a
    Playwright call that only fails when actually made: `new_context()` takes
    no user_data_dir, so every "connected" platform came back signed out and
    every run reported zero jobs found.
    """

    @pytest.fixture
    def profiles_in_tmp(self, tmp_path, monkeypatch):
        """Keep test sessions out of the real profile directory."""
        root = tmp_path / "profiles"
        root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            SessionManager, "profile_root", property(lambda self: root)
        )
        return root

    @pytest.mark.asyncio
    async def test_opening_a_persistent_context_succeeds(self, profiles_in_tmp):
        """The connect flow's browser call must be valid Playwright."""
        manager = SessionManager()

        try:
            await manager.start_playwright()
            context = await manager._open_persistent_context("acme", headless=True)

            assert context is not None
            assert context.pages, "Connectors expect a page to already be open"
        finally:
            await manager.stop_playwright()

    @pytest.mark.asyncio
    async def test_session_survives_a_restart(self, profiles_in_tmp):
        """A platform connected in one process is usable in the next."""
        manager = SessionManager()

        # A session cookie is what a real sign-in leaves behind, and it needs
        # no network to write.
        try:
            await manager.start_playwright()
            context = await manager._open_persistent_context("acme", headless=True)
            await context.add_cookies([{
                "name": "session",
                "value": "signed-in",
                "url": "https://acme.example",
                # Session cookies are dropped when the browser closes, which
                # is exactly what we are testing across — so give it an expiry.
                "expires": time.time() + 3600,
            }])
        finally:
            await manager.stop_playwright()

        # A second manager is what a scheduled run, or a restarted dashboard,
        # starts from: nothing in memory, only the profile on disk.
        restarted = SessionManager()

        try:
            await restarted.start_playwright()
            page = await restarted.get_page("acme")

            assert page is not None, "Saved session should reopen after a restart"

            cookies = await page.context.cookies("https://acme.example")
            assert [c["value"] for c in cookies if c["name"] == "session"] == [
                "signed-in"
            ], "The sign-in should survive the process that created it"
        finally:
            await restarted.stop_playwright()

    @pytest.mark.asyncio
    async def test_a_closed_browser_is_reopened(self, profiles_in_tmp):
        """
        A cached context outliving its browser must not pass for a session.

        The user closes the agent's window, or it crashes. The entry in
        `contexts` survives, `.pages` on it is empty, and every later call is
        told there is no authenticated session for a platform whose sign-in is
        sitting on disk — for as long as the dashboard stays up.
        """
        manager = SessionManager()

        try:
            await manager.start_playwright()

            first = await manager._open_persistent_context("acme", headless=True)
            await first.close()

            # The dead context is still cached at this point, which is the
            # state the bug reported as "not connected".
            assert manager.contexts.get("acme") is first

            page = await manager.get_page("acme")

            assert page is not None, "A closed browser should be reopened"
            assert manager.contexts["acme"] is not first
        finally:
            await manager.stop_playwright()

    @pytest.mark.asyncio
    async def test_a_public_board_gets_a_browser_without_a_signin(
        self, profiles_in_tmp
    ):
        """
        A board with nothing to sign into still needs a browser.

        Preparing an application on a public Greenhouse board failed with "no
        authenticated browser session" — a sign-in it neither has nor needs.
        """
        manager = SessionManager()

        try:
            await manager.start_playwright()

            assert await manager.get_page("public_board", needs_signin=False)
        finally:
            await manager.stop_playwright()

    @pytest.mark.asyncio
    async def test_unconnected_platform_has_no_page(self, profiles_in_tmp):
        """A platform never signed into stays unconnected, rather than reopening blank."""
        manager = SessionManager()

        try:
            await manager.start_playwright()

            assert await manager.get_context("never_connected") is None
            assert await manager.get_page("never_connected") is None
        finally:
            await manager.stop_playwright()

    @pytest.mark.asyncio
    async def test_connect_flow_shows_the_window(self, profiles_in_tmp, monkeypatch):
        """Sign-in needs a visible browser; it must never open headless."""
        manager = SessionManager()
        seen = {}

        async def fake_open(platform, headless):
            seen["platform"] = platform
            seen["headless"] = headless
            return "context"

        monkeypatch.setattr(manager, "_open_persistent_context", fake_open)

        assert await manager.launch_browser_for_connection("acme") == "context"
        assert seen == {"platform": "acme", "headless": False}


class TestKeychainIntegration:
    """Test Keychain metadata storage."""

    def test_store_and_retrieve_metadata(self):
        """Test storing and retrieving metadata from Keychain."""
        platform = "test_platform"
        metadata = {
            "platform": platform,
            "profile_dir": "/test/path",
            "connected_at": utcnow().isoformat(),
        }
        
        # Store
        success = store_platform_metadata(platform, metadata)
        assert success, "Should successfully store metadata"
        
        # Retrieve
        retrieved = retrieve_platform_metadata(platform)
        assert retrieved is not None, "Should retrieve metadata"
        assert retrieved["platform"] == platform
        
        # Cleanup
        delete_platform_metadata(platform)
    
    def test_delete_metadata(self):
        """Test deleting metadata from Keychain."""
        platform = "test_platform_delete"
        metadata = {"platform": platform}
        
        # Store
        store_platform_metadata(platform, metadata)
        
        # Delete
        success = delete_platform_metadata(platform)
        assert success, "Should successfully delete metadata"
        
        # Verify deletion
        retrieved = retrieve_platform_metadata(platform)
        assert retrieved is None, "Metadata should be deleted"


class TestSingletonPattern:
    """Test SessionManager singleton behavior."""
    
    @pytest.mark.asyncio
    async def test_singleton_get_session_manager(self):
        """Test that get_session_manager returns same instance."""
        manager1 = await get_session_manager()
        manager2 = await get_session_manager()
        
        assert manager1 is manager2, "Should return same instance"
        
        await close_session_manager()
    
    @pytest.mark.asyncio
    async def test_close_session_manager(self):
        """Test that close_session_manager resets singleton."""
        await get_session_manager()
        await close_session_manager()
        
        # Get new instance
        manager_new = await get_session_manager()
        assert manager_new is not None, "Should create new instance after close"
        
        await close_session_manager()


class TestProfileDirManagement:
    """Test profile directory management."""
    
    def test_profile_dir_structure(self):
        """Test that profile directory structure is correct."""
        manager = SessionManager()
        profile_dir = manager.get_profile_dir("test_platform")
        
        expected_root = settings.profile_dir
        expected_path = expected_root / "test_platform"
        
        assert str(profile_dir).endswith("test_platform")
        assert profile_dir.parent == expected_root


@pytest.mark.asyncio
async def test_full_connection_flow():
    """
    Integration test for full connection flow.
    
    This test simulates:
    1. SessionManager starts
    2. Browser launches for connection
    3. Connection metadata saved
    4. Platform disconnected (cleanup)
    """
    manager = SessionManager()
    
    try:
        # Start Playwright
        await manager.start_playwright()
        
        # Get profile directory
        platform = "test_integration"
        profile_dir = manager.get_profile_dir(platform)
        
        # Save connection metadata
        success = await manager.save_connection_metadata(platform, profile_dir)
        assert success, "Should save connection metadata"
        
        # Verify metadata in Keychain
        metadata = retrieve_platform_metadata(platform)
        assert metadata is not None
        assert metadata["platform"] == platform
        
        # Disconnect
        success = await manager.disconnect_platform(platform)
        assert success, "Should disconnect successfully"
        
        # Verify cleanup
        metadata = retrieve_platform_metadata(platform)
        assert metadata is None, "Metadata should be deleted after disconnect"
    
    finally:
        await manager.stop_playwright()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""
Keychain integration for secure storage of non-sensitive session metadata.

This module wraps the `keyring` package to store and retrieve session metadata
from macOS Keychain without ever touching passwords or sensitive credentials.

Per §3 of the spec:
- Agent stores ONLY non-sensitive metadata to Keychain: platform name, connection timestamp,
  profile directory path, last-verified timestamp.
- No cookies, tokens, or passwords ever stored.
- Playwright manages actual browser sessions in profile directories.
"""

import keyring
import json
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)

SERVICE_NAME = "job-agent"


def store_platform_metadata(platform: str, metadata: Dict[str, Any]) -> bool:
    """
    Store platform session metadata in Keychain.
    
    Args:
        platform: Platform name (e.g., "linkedin", "greenhouse")
        metadata: Dict containing: {
            "profile_dir": "/path/to/profiles/linkedin",
            "connected_at": "2026-08-14T14:32:15",
            "last_verified_at": "2026-08-14T14:32:15"
        }
    
    Returns:
        True if successful, False otherwise.
    
    Note:
        - No passwords, credentials, or tokens are stored here.
        - Playwright persistent context handles actual session data.
    """
    try:
        metadata_json = json.dumps(metadata)
        keyring.set_password(SERVICE_NAME, f"platform_{platform}", metadata_json)
        logger.info(f"Platform metadata stored for {platform}")
        return True
    except Exception as e:
        logger.error(f"Failed to store platform metadata for {platform}: {e}")
        return False


def retrieve_platform_metadata(platform: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve platform session metadata from Keychain.
    
    Args:
        platform: Platform name (e.g., "linkedin", "greenhouse")
    
    Returns:
        Dict with stored metadata, or None if not found.
    """
    try:
        metadata_json = keyring.get_password(SERVICE_NAME, f"platform_{platform}")
        if metadata_json:
            return json.loads(metadata_json)
        return None
    except Exception as e:
        logger.error(f"Failed to retrieve platform metadata for {platform}: {e}")
        return None


def delete_platform_metadata(platform: str) -> bool:
    """
    Delete platform session metadata from Keychain.
    
    Called when user disconnects a platform account (§3).
    Also typically followed by deleting the profile directory.
    
    Args:
        platform: Platform name (e.g., "linkedin", "greenhouse")
    
    Returns:
        True if successful or not found, False on error.
    """
    try:
        keyring.delete_password(SERVICE_NAME, f"platform_{platform}")
        logger.info(f"Platform metadata deleted for {platform}")
        return True
    except keyring.errors.PasswordDeleteError:
        # Not found, which is fine
        logger.debug(f"Platform metadata not found for {platform} (already deleted)")
        return True
    except Exception as e:
        logger.error(f"Failed to delete platform metadata for {platform}: {e}")
        return False


def store_app_password(service: str, username: str, app_password: str) -> bool:
    """
    Store an app-specific password for IMAP/SMTP or similar services.
    
    Used for email-based applications (§5a) when Mail.app is not available.
    
    Args:
        service: Service name (e.g., "gmail_imap", "outlook_smtp")
        username: Email address or username
        app_password: App-specific password (not the real account password)
    
    Returns:
        True if successful, False otherwise.
    
    Warning:
        - Only store app-specific passwords, never account passwords.
        - User must generate the app password in their email provider's security settings.
    """
    try:
        keyring.set_password(SERVICE_NAME, f"{service}_{username}", app_password)
        logger.info(f"App password stored for {service}/{username}")
        return True
    except Exception as e:
        logger.error(f"Failed to store app password for {service}/{username}: {e}")
        return False


def retrieve_app_password(service: str, username: str) -> Optional[str]:
    """
    Retrieve an app-specific password from Keychain.
    
    Args:
        service: Service name (e.g., "gmail_imap", "outlook_smtp")
        username: Email address or username
    
    Returns:
        App-specific password, or None if not found.
    """
    try:
        password = keyring.get_password(SERVICE_NAME, f"{service}_{username}")
        return password
    except Exception as e:
        logger.error(f"Failed to retrieve app password for {service}/{username}: {e}")
        return None


def delete_app_password(service: str, username: str) -> bool:
    """
    Delete an app-specific password from Keychain.
    
    Args:
        service: Service name (e.g., "gmail_imap", "outlook_smtp")
        username: Email address or username
    
    Returns:
        True if successful or not found, False on error.
    """
    try:
        keyring.delete_password(SERVICE_NAME, f"{service}_{username}")
        logger.info(f"App password deleted for {service}/{username}")
        return True
    except keyring.errors.PasswordDeleteError:
        logger.debug(f"App password not found for {service}/{username}")
        return True
    except Exception as e:
        logger.error(f"Failed to delete app password for {service}/{username}: {e}")
        return False


def test_keychain_access() -> bool:
    """
    Test that Keychain is accessible and working.
    
    Called during Phase 0 smoke test to ensure Keychain is properly configured.
    
    Returns:
        True if Keychain is accessible, False otherwise.
    """
    try:
        test_key = "test_job_agent"
        test_value = "keychain_accessible"
        
        # Try to store and retrieve a test value
        keyring.set_password(SERVICE_NAME, test_key, test_value)
        retrieved = keyring.get_password(SERVICE_NAME, test_key)
        
        # Clean up
        keyring.delete_password(SERVICE_NAME, test_key)
        
        if retrieved == test_value:
            logger.info("Keychain access test passed")
            return True
        else:
            logger.error("Keychain access test failed: value mismatch")
            return False
    except Exception as e:
        logger.error(f"Keychain access test failed: {e}")
        return False

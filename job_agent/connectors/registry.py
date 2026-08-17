"""
Connector Registry (Phase 2).

Central registry for all platform connectors.
Provides:
- Registration of new connectors
- Lookup by platform name
- Instantiation with configuration
- Type checking and validation

Pattern:
    # Register a connector
    register_connector("linkedin", LinkedInConnector)
    
    # Get a connector class
    connector_class = get_connector("linkedin")
    
    # Create instance
    connector = create_connector("linkedin")
"""

import logging
from typing import Dict, Type, Optional, Any

from job_agent.connectors.base import ConnectedPlatformConnector
from job_agent.connectors.test_connector import TestConnector
from job_agent.connectors.generic_ats import GenericATSConnector

logger = logging.getLogger(__name__)


class ConnectorRegistry:
    """
    Central registry for platform connectors.
    
    Manages connector registration, lookup, and instantiation.
    Designed for easy extension with new connectors.
    """
    
    def __init__(self):
        """Initialize connector registry."""
        self._connectors: Dict[str, Type[ConnectedPlatformConnector]] = {}
        self._instances: Dict[str, ConnectedPlatformConnector] = {}
        
        # Register built-in connectors
        self._register_builtin_connectors()
    
    def _register_builtin_connectors(self) -> None:
        """Register built-in connectors."""
        from job_agent.connectors.ats_connectors import ATS_CONNECTORS
        from job_agent.connectors.job_boards import BOARD_CONNECTORS

        self.register("test_connector", TestConnector)
        self.register("generic_ats", GenericATSConnector)

        # Phase 7: hosted ATS platforms, then the read-only consumer boards
        for connector_class in (*ATS_CONNECTORS, *BOARD_CONNECTORS):
            self.register(connector_class.PLATFORM, connector_class)

        logger.info(f"Registered {len(self._connectors)} built-in connectors")
    
    def register(
        self,
        platform_name: str,
        connector_class: Type[ConnectedPlatformConnector]
    ) -> None:
        """
        Register a connector for a platform.
        
        Args:
            platform_name: Platform identifier (e.g., "linkedin", "greenhouse")
            connector_class: Connector class (must extend ConnectedPlatformConnector)
        
        Raises:
            TypeError: If connector_class doesn't extend ConnectedPlatformConnector
            ValueError: If platform_name is empty
        """
        if not platform_name:
            raise ValueError("Platform name cannot be empty")
        
        if not issubclass(connector_class, ConnectedPlatformConnector):
            raise TypeError(
                f"{connector_class.__name__} must extend ConnectedPlatformConnector"
            )
        
        self._connectors[platform_name] = connector_class
        logger.info(f"Registered connector for {platform_name}: {connector_class.__name__}")
    
    def unregister(self, platform_name: str) -> None:
        """
        Unregister a connector for a platform.
        
        Args:
            platform_name: Platform identifier
        """
        if platform_name in self._connectors:
            del self._connectors[platform_name]
            logger.info(f"Unregistered connector for {platform_name}")
    
    def get_connector_class(
        self,
        platform_name: str
    ) -> Optional[Type[ConnectedPlatformConnector]]:
        """
        Get the connector class for a platform.
        
        Args:
            platform_name: Platform identifier
        
        Returns:
            Connector class, or None if not registered
        """
        return self._connectors.get(platform_name)
    
    def create_connector(
        self,
        platform_name: str,
        **kwargs: Any
    ) -> Optional[ConnectedPlatformConnector]:
        """
        Create a connector instance for a platform.
        
        Args:
            platform_name: Platform identifier
            **kwargs: Additional arguments to pass to connector constructor
        
        Returns:
            Connector instance, or None if not registered
        """
        connector_class = self.get_connector_class(platform_name)
        
        if not connector_class:
            logger.warning(f"No connector registered for {platform_name}")
            return None
        
        try:
            instance = connector_class(platform_name, **kwargs)
            logger.info(f"Created connector instance for {platform_name}")
            return instance
        except Exception as e:
            logger.error(f"Failed to create connector for {platform_name}: {e}")
            return None
    
    def get_instance(self, platform_name: str) -> Optional[ConnectedPlatformConnector]:
        """
        Get or create a singleton instance for a platform.
        
        Args:
            platform_name: Platform identifier
        
        Returns:
            Connector instance, or None if not registered
        """
        if platform_name not in self._instances:
            self._instances[platform_name] = self.create_connector(platform_name)
        
        return self._instances[platform_name]
    
    def list_connectors(self) -> Dict[str, Type[ConnectedPlatformConnector]]:
        """
        List all registered connectors.
        
        Returns:
            Dict of platform_name -> connector_class
        """
        return self._connectors.copy()
    
    def is_registered(self, platform_name: str) -> bool:
        """
        Check if a platform has a registered connector.
        
        Args:
            platform_name: Platform identifier
        
        Returns:
            True if registered, False otherwise
        """
        return platform_name in self._connectors


# Global singleton registry
_global_registry: Optional[ConnectorRegistry] = None


def create_connector_for_account(account: Any) -> Optional[ConnectedPlatformConnector]:
    """
    Build the connector that drives a particular station.

    A station the user added by pasting a careers URL has no connector named
    after it; it borrows the generic one and supplies its own search URL. Going
    through the account rather than the bare platform name is what lets those
    stations run the same pipeline as the built-in platforms.

    Args:
        account: A PlatformAccount

    Returns:
        Connector instance, or None if nothing is registered for it
    """
    kind = getattr(account, "connector_kind", None) or account.platform

    connector = create_connector(kind)

    if connector is None:
        return None

    # The pipeline addresses stations by platform name — a custom station must
    # answer to its own name, not to "generic_ats".
    connector.platform_name = account.platform

    # One generic connector drives both public boards and sites behind a login,
    # so the station is the only thing that knows which this is.
    requires_signin = getattr(account, "requires_signin", None)

    if requires_signin is not None:
        connector.capabilities.requires_manual_signin = requires_signin

    if getattr(account, "search_url", None):
        connector.set_search_url(account.search_url)

    return connector


def get_registry() -> ConnectorRegistry:
    """
    Get the global connector registry.
    
    Creates a singleton instance on first call.
    
    Returns:
        ConnectorRegistry instance
    """
    global _global_registry
    
    if _global_registry is None:
        _global_registry = ConnectorRegistry()
    
    return _global_registry


def register_connector(
    platform_name: str,
    connector_class: Type[ConnectedPlatformConnector]
) -> None:
    """
    Register a connector globally.
    
    Args:
        platform_name: Platform identifier
        connector_class: Connector class
    """
    registry = get_registry()
    registry.register(platform_name, connector_class)


def unregister_connector(platform_name: str) -> None:
    """
    Unregister a connector globally.
    
    Args:
        platform_name: Platform identifier
    """
    registry = get_registry()
    registry.unregister(platform_name)


def get_connector_class(
    platform_name: str
) -> Optional[Type[ConnectedPlatformConnector]]:
    """
    Get a connector class globally.
    
    Args:
        platform_name: Platform identifier
    
    Returns:
        Connector class, or None
    """
    registry = get_registry()
    return registry.get_connector_class(platform_name)


def create_connector(
    platform_name: str,
    **kwargs: Any
) -> Optional[ConnectedPlatformConnector]:
    """
    Create a connector instance globally.
    
    Args:
        platform_name: Platform identifier
        **kwargs: Additional arguments
    
    Returns:
        Connector instance, or None
    """
    registry = get_registry()
    return registry.create_connector(platform_name, **kwargs)


def get_connector_instance(platform_name: str) -> Optional[ConnectedPlatformConnector]:
    """
    Get or create a singleton connector instance globally.
    
    Args:
        platform_name: Platform identifier
    
    Returns:
        Connector instance, or None
    """
    registry = get_registry()
    return registry.get_instance(platform_name)


def list_connectors() -> Dict[str, Type[ConnectedPlatformConnector]]:
    """
    List all registered connectors globally.
    
    Returns:
        Dict of platform_name -> connector_class
    """
    registry = get_registry()
    return registry.list_connectors()


def is_connector_registered(platform_name: str) -> bool:
    """
    Check if a connector is registered globally.
    
    Args:
        platform_name: Platform identifier
    
    Returns:
        True if registered, False otherwise
    """
    registry = get_registry()
    return registry.is_registered(platform_name)

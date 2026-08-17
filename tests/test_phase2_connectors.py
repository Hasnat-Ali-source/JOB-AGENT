#!/usr/bin/env python3
"""
Integration tests for Phase 2: Connector Framework.

Tests:
- Generic ATS connector initialization
- Connector registry registration and lookup
- Connector instantiation
- JSON-LD parsing
- HTML fallback parsing
- Apply method detection
"""

import pytest

from job_agent.connectors.base import ConnectedPlatformConnector, PlatformCapabilities
from job_agent.connectors.generic_ats import GenericATSConnector
from job_agent.connectors.test_connector import TestConnector
from job_agent.connectors.registry import (
    ConnectorRegistry,
    get_registry,
    get_connector_class,
    create_connector,
    is_connector_registered,
    list_connectors,
)


class TestGenericATSConnector:
    """Test generic ATS connector."""
    
    def test_initialization(self):
        """Test connector initializes with correct settings."""
        connector = GenericATSConnector()
        
        assert connector.platform_name == "generic_ats"
        assert connector.capabilities is not None
        assert isinstance(connector.capabilities, PlatformCapabilities)
    
    def test_capabilities(self):
        """Test connector capabilities are set correctly."""
        connector = GenericATSConnector()
        
        assert connector.capabilities.can_search is True
        assert connector.capabilities.can_filter is True
        assert connector.capabilities.can_read_details is True
        assert connector.capabilities.can_start_application is True
        assert connector.capabilities.can_fill_standard_fields is False
        assert connector.capabilities.can_submit_automatically is False
        assert connector.capabilities.requires_manual_signin is True
    
    def test_inherits_from_base(self):
        """Test connector extends ConnectedPlatformConnector."""
        connector = GenericATSConnector()
        
        assert isinstance(connector, ConnectedPlatformConnector)
    
    def test_has_required_methods(self):
        """Test connector implements all required methods."""
        connector = GenericATSConnector()
        
        required_methods = [
            "check_session",
            "open_search",
            "apply_search_filters",
            "collect_job_links",
            "read_job_details",
            "begin_application",
            "fill_application",
            "submit_application",
        ]
        
        for method_name in required_methods:
            assert hasattr(connector, method_name), f"Missing method: {method_name}"
            assert callable(getattr(connector, method_name))
    
    def test_selectors_structure(self):
        """Test that CSS selectors are properly structured."""
        connector = GenericATSConnector()
        
        assert "job_listing" in connector.selectors
        assert "job_title" in connector.selectors
        assert "job_company" in connector.selectors
        assert "job_location" in connector.selectors
        assert "apply_button" in connector.selectors
        
        # Each should be a list of selectors
        for key, selectors in connector.selectors.items():
            assert isinstance(selectors, list)
            assert len(selectors) > 0


class TestConnectorRegistry:
    """Test connector registry functionality."""
    
    def test_registry_initialization(self):
        """Test registry initializes with built-in connectors."""
        registry = ConnectorRegistry()
        
        connectors = registry.list_connectors()
        assert "test_connector" in connectors
        assert "generic_ats" in connectors
    
    def test_register_connector(self):
        """Test registering a new connector."""
        registry = ConnectorRegistry()
        
        # Create a test connector class
        class DummyConnector(ConnectedPlatformConnector):
            async def check_session(self):
                return "connected"
            async def open_search(self, profile):
                pass
            async def apply_search_filters(self, profile):
                pass
            async def collect_job_links(self):
                return []
            async def read_job_details(self, url):
                return None
            async def begin_application(self, job):
                return None
            async def fill_application(self, session, profile, package):
                return session
            async def submit_application(self, session):
                return None
        
        registry.register("dummy_connector", DummyConnector)
        
        assert registry.is_registered("dummy_connector")
        assert registry.get_connector_class("dummy_connector") is DummyConnector
    
    def test_register_invalid_connector(self):
        """Test registering invalid connector raises error."""
        registry = ConnectorRegistry()
        
        class NotAConnector:
            pass
        
        with pytest.raises(TypeError):
            registry.register("invalid", NotAConnector)
    
    def test_register_empty_platform_name(self):
        """Test registering with empty platform name raises error."""
        registry = ConnectorRegistry()
        
        with pytest.raises(ValueError):
            registry.register("", GenericATSConnector)
    
    def test_unregister_connector(self):
        """Test unregistering a connector."""
        registry = ConnectorRegistry()
        
        assert registry.is_registered("generic_ats")
        
        registry.unregister("generic_ats")
        
        assert not registry.is_registered("generic_ats")
    
    def test_get_connector_class(self):
        """Test getting a connector class."""
        registry = ConnectorRegistry()
        
        connector_class = registry.get_connector_class("test_connector")
        
        assert connector_class is TestConnector
    
    def test_get_nonexistent_connector_class(self):
        """Test getting nonexistent connector returns None."""
        registry = ConnectorRegistry()
        
        connector_class = registry.get_connector_class("nonexistent")
        
        assert connector_class is None
    
    def test_create_connector(self):
        """Test creating a connector instance."""
        registry = ConnectorRegistry()
        
        connector = registry.create_connector("test_connector")
        
        assert connector is not None
        assert isinstance(connector, TestConnector)
    
    def test_create_nonexistent_connector(self):
        """Test creating nonexistent connector returns None."""
        registry = ConnectorRegistry()
        
        connector = registry.create_connector("nonexistent")
        
        assert connector is None
    
    def test_get_singleton_instance(self):
        """Test getting singleton instance."""
        registry = ConnectorRegistry()
        
        instance1 = registry.get_instance("test_connector")
        instance2 = registry.get_instance("test_connector")
        
        assert instance1 is instance2
    
    def test_list_connectors(self):
        """Test listing all connectors."""
        registry = ConnectorRegistry()
        
        connectors = registry.list_connectors()
        
        assert isinstance(connectors, dict)
        assert "test_connector" in connectors
        assert "generic_ats" in connectors


class TestGlobalRegistry:
    """Test global registry functions."""
    
    def test_get_global_registry(self):
        """Test getting global registry."""
        registry = get_registry()
        
        assert registry is not None
        assert isinstance(registry, ConnectorRegistry)
    
    def test_global_registry_singleton(self):
        """Test global registry is a singleton."""
        registry1 = get_registry()
        registry2 = get_registry()
        
        assert registry1 is registry2
    
    def test_register_connector_globally(self):
        """Test registering connector globally."""
        registry = get_registry()
        
        # Generic ATS should be registered
        assert is_connector_registered("generic_ats")
    
    def test_get_connector_class_globally(self):
        """Test getting connector class globally."""
        connector_class = get_connector_class("generic_ats")
        
        assert connector_class is GenericATSConnector
    
    def test_create_connector_globally(self):
        """Test creating connector globally."""
        connector = create_connector("generic_ats")
        
        assert connector is not None
        assert isinstance(connector, GenericATSConnector)
    
    def test_list_connectors_globally(self):
        """Test listing connectors globally."""
        connectors = list_connectors()
        
        assert "generic_ats" in connectors
        assert "test_connector" in connectors


class TestConnectorIntegration:
    """Test connector integration patterns."""
    
    def test_can_switch_between_connectors(self):
        """Test switching between different connectors."""
        registry = get_registry()
        
        # Get test connector
        test_connector = registry.create_connector("test_connector")
        assert test_connector.platform_name == "test_connector"
        
        # Get generic ATS connector
        generic_connector = registry.create_connector("generic_ats")
        assert generic_connector.platform_name == "generic_ats"
        
        # They are different instances
        assert test_connector is not generic_connector
    
    def test_connector_capabilities_match_interface(self):
        """Test connector capabilities match their interface."""
        registry = get_registry()
        
        # Test connector should declare manual signin
        test_connector = registry.create_connector("test_connector")
        assert test_connector.capabilities.requires_manual_signin is True
        
        # Generic ATS should also require manual signin
        generic_connector = registry.create_connector("generic_ats")
        assert generic_connector.capabilities.requires_manual_signin is True


class TestNormalizationFunctions:
    """Test normalization functions in generic connector."""
    
    def test_normalize_job_type(self):
        """Test job type normalization."""
        connector = GenericATSConnector()
        
        assert connector._normalize_job_type("FULL_TIME") == "full_time"
        assert connector._normalize_job_type("Full-time") == "full_time"
        assert connector._normalize_job_type("PART_TIME") == "part_time"
        assert connector._normalize_job_type("Contract") == "contract"
        assert connector._normalize_job_type("TEMPORARY") == "temporary"
        assert connector._normalize_job_type("unknown") == "full_time"
    
    def test_extract_salary(self):
        """Test salary extraction."""
        connector = GenericATSConnector()
        
        salary_range = {
            "minValue": 100000,
            "maxValue": 150000,
            "currency": "USD"
        }
        
        result = connector._extract_salary(salary_range)
        assert result is not None
        assert "100,000" in result
        assert "150,000" in result
    
    def test_extract_location(self):
        """Test location extraction."""
        connector = GenericATSConnector()
        
        location_data = {
            "@type": "Place",
            "address": {
                "addressLocality": "San Francisco",
                "addressRegion": "CA",
                "addressCountry": "USA"
            }
        }
        
        result = connector._extract_location(location_data)
        assert "San Francisco" in result
        assert "CA" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

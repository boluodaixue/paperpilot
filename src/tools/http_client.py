"""Shared trusted TLS configuration for external research tools."""

from __future__ import annotations

import ssl

import aiohttp
import certifi


def trusted_ssl_context() -> ssl.SSLContext:
    """Return a context that combines platform trust with Mozilla's CA bundle."""
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


def trusted_connector() -> aiohttp.TCPConnector:
    """Create an event-loop-local aiohttp connector with certificate validation."""
    return aiohttp.TCPConnector(ssl=trusted_ssl_context())

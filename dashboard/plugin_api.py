"""Dashboard discovery wrapper for the optional read-only API."""
from ..dashboard_api import health, router, status

__all__ = ["health", "router", "status"]

"""Headway Hermes plugin. Hermes imports this package and calls register(ctx)."""
if __package__:
    from .plugin_main import register  # noqa: F401
    from . import dashboard_api  # noqa: F401

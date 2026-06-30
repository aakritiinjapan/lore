"""Minimal stub of werkzeug.exceptions for the Lore demo workspace."""


class NotFound(Exception):
    """Raised when a requested resource is not found (HTTP 404)."""

    code = 404

"""Minimal stub of werkzeug.wsgi for the Lore demo workspace."""


def send_file(path, environ, **kwargs):
    """Stub: in real Werkzeug this streams the file as a WSGI response."""
    return ("200 OK", path)

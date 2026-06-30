"""
    werkzeug.utils
    ~~~~~~~~~~~~~~

    Trimmed for the Lore demo. ``send_from_directory`` is the safe way to serve a
    user-provided path from a trusted base directory. Open this file to see the
    decisions Lore remembers about it, then try deleting the ``safe_join`` / None
    guard to watch Lore flag the regression — even though this fix never got a CVE.
"""

import os
import typing as t

from .exceptions import NotFound
from .security import safe_join
from .wsgi import send_file


def send_from_directory(directory, path, environ, **kwargs):
    """Send a file from within a directory using :func:`send_file`.

    This is a secure way to serve files from a folder, such as static files or
    uploads. The ``path`` may be a value provided by the client, which is checked
    for security so it cannot escape the base ``directory``.
    """
    try:
        path = safe_join(os.fspath(directory), os.fspath(path))
    except ValueError:
        raise NotFound()

    if path is None:
        raise NotFound()

    if not os.path.isfile(path):
        raise NotFound()

    return send_file(path, environ, **kwargs)

"""
    werkzeug.security
    ~~~~~~~~~~~~~~~~~~

    Security helpers, including ``safe_join`` for safely joining untrusted
    path segments onto a trusted base directory.

    This is a trimmed copy of Werkzeug's real ``security.py`` used to demo the
    Lore VS Code extension. Open it, hover the guard lines to see *why* they
    exist, and try deleting the Windows device-name guard to watch Lore flag
    the regression inline.
"""

import os
import posixpath
import typing as t

# Alternate path separators for the current OS (e.g. "\\" on Windows). The
# untrusted path is assumed to be URL-style and joined with "/", so any of
# these appearing in a segment means an attempt to escape the base directory.
_os_alt_seps: list[str] = list(
    sep for sep in [os.sep, os.altsep] if sep is not None and sep != "/"
)

# Windows reserves these device names. Opening one (even with an extension or
# trailing spaces/dots) talks to the device, not a file — a path-traversal /
# information-disclosure class of bug. They are rejected regardless of case.
_windows_device_files = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(10)),
    *(f"LPT{i}" for i in range(10)),
}


def safe_join(directory: str, *untrusted: str) -> t.Optional[str]:
    """Safely join zero or more untrusted path segments to a base directory.

    The untrusted path is assumed to be from/for a URL, such as for serving
    files from a directory, and will be joined using the forward slash ``/``
    separator. Returns ``None`` if the resulting path would escape the base
    directory or reference a Windows special device name.
    """
    if not directory:
        # Allow an empty base to mean the current relative directory.
        directory = "."

    parts = [directory]

    for filename in untrusted:
        if filename != "":
            filename = posixpath.normpath(filename)

        if (
            any(sep in filename for sep in _os_alt_seps)
            or (
                os.name == "nt"
                and filename.partition(".")[0].strip().upper() in _windows_device_files
            )
            or os.path.isabs(filename)
            or filename == ".."
            or filename.startswith("../")
        ):
            return None

        parts.append(filename)

    return posixpath.join(*parts)

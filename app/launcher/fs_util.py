# -*- coding: utf-8 -*-
r"""fs_util.py -- tiny filesystem helpers shared by the write paths.

⛔⭐⭐ A COPY OF THE GAME FILES IS NORMALLY READ-ONLY. Files taken off a disc, an ISO mount or an
extracted GOD/XBLA container carry FILE_ATTRIBUTE_READONLY, and Windows preserves it through a
copy. Every write path here opens "r+b" or replaces a file wholesale, so on a fresh install each
one fails with a bare

    PermissionError: [Errno 13] Permission denied: '...\default.xex'

which reads like a file lock or a missing admin right and is neither -- nothing is holding the
file. The author's own tree had the flag cleared years ago, which is why none of this ever showed
up in development and every clean install hit it.

Clearing the flag is a metadata change, it is reversible with `attrib +r`, and any caller that
reaches for this is about to rewrite the file anyway. Keep it in ONE place so a new write path
cannot forget it.
"""
import os
import stat
from pathlib import Path


def ensure_writable(path) -> bool:
    """Drop the read-only attribute from `path` if it has one. True if a change was made.

    Missing files and directories are ignored; a genuine ACL/lock problem is left alone so the
    caller still fails loudly rather than being told the file is fine.
    """
    p = Path(path)
    try:
        if not p.is_file() or os.access(p, os.W_OK):
            return False
        os.chmod(p, os.stat(p).st_mode | stat.S_IWRITE)
    except OSError:
        return False
    return True


def ensure_writable_all(paths, log=None) -> list:
    """`ensure_writable` over many paths. Returns the names actually changed."""
    done = [Path(p).name for p in paths if ensure_writable(p)]
    if done and log:
        log(f"  cleared the read-only flag on {', '.join(done)}")
    return done

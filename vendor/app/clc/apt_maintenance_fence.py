"""Persistent admission guard only. No installer, cleanup or authority grant."""
import os
from pathlib import Path
MARKER=Path("/var/lib/sasocq-maintenance/apt.active")
def blocked():
    try:
        os.lstat(MARKER)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True

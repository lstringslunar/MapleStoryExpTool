import ctypes
import sys
from pathlib import Path


# noinspection protected-member
def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        base_path = Path(sys._MEIPASS)
    else:
        base_path = Path.cwd()

    return base_path / relative_path


def get_hwnd(title: str) -> int | None:
    """Returns the HWND for an exact window title match."""
    hwnd = ctypes.windll.user32.FindWindowW(None, title)
    return hwnd or None

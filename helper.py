import ctypes
import sys
from pathlib import Path

import cv2
import numpy as np


# noinspection protected-member
def get_resource_path(relative_path, as_posix=True):
    # Handles files packaged by pyinstaller -------------------------------------------------------
    if hasattr(sys, '_MEIPASS'):
        base_path = Path(sys._MEIPASS)
    else:
        base_path = Path.cwd()

    resource_path = base_path / relative_path
    return resource_path.as_posix() if as_posix else resource_path


def imread(path, flags: int):
    # Replaces cv2.imread with UTF-8 path support -------------------------------------------------
    data = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(data, flags)

    return image


def get_hwnd(title: str) -> int | None:
    # Returns the HWND for an exact window title match --------------------------------------------
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    hwnd = user32.FindWindowW(None, title)
    return hwnd or None


def supports_gcb() -> bool:
    # Returns True if supports GraphicsCaptureSession (WGC) border control ------------------------
    if sys.platform != "win32":
        return False

    # GraphicsCaptureSession.IsBorderRequired introduced in Windows 10.0.20348.0
    return sys.getwindowsversion().build >= 20348

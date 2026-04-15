"""ANSI / VT support on Windows consoles (shared by CLI prompts and ``TTYTracer``)."""

import sys

_WIN32_VT_ENABLED = False


def enable_windows_console_vt() -> None:
    """Enable escape processing on Windows 10+ (stdout). Safe to call repeatedly."""
    global _WIN32_VT_ENABLED
    if _WIN32_VT_ENABLED or sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            if kernel32.SetConsoleMode(handle, mode.value | enable_vt):
                _WIN32_VT_ENABLED = True
    except Exception:
        pass

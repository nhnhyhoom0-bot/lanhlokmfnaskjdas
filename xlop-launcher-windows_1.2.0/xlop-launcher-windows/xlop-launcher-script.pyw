#!explorer.exe
# xlop launcher - Windows entry point.
# xlop-launcher.exe (next to this file) opens THIS file with the Python installed on your PC,
# exactly like double-clicking it. You can also double-click this file directly.
import os
import sys
import traceback

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
if not FROZEN:
    sys.path.insert(0, HERE)
LOGDIR = os.path.join(os.environ.get("APPDATA") or HERE, "xlop-launcher")
LOG = os.path.join(LOGDIR, "launcher.log")


def _fatal(text):
    """Message box that works even when Tk is missing."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, "xlop launcher", 0x10)
    except Exception:
        pass


def _fix_streams():
    """pythonw / windowed exe have no console: send print() and errors to a log file."""
    if sys.stdout is None or sys.stderr is None:
        os.makedirs(LOGDIR, exist_ok=True)
        fh = open(LOG, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = fh
        if sys.stderr is None:
            sys.stderr = fh


def main():
    gui_mode = not sys.argv[1:]
    if sys.version_info < (3, 8):
        _fatal("xlop launcher needs Python 3.8 or newer.\nDownload: https://www.python.org/downloads/windows/")
        return 1
    if gui_mode:
        try:
            import tkinter  # noqa: F401
        except ImportError:
            _fatal("Tkinter is missing from this Python.\n"
                   "Re-run the Python installer > Modify > tick 'tcl/tk and IDLE'.\n"
                   "(Python from python.org includes it.)\n\n"
                   "Tkinter غير موجود في هذا البايثون. أعد تثبيت Python من python.org مع خيار tcl/tk.")
            return 1
    from xlop.cli import main as run
    return run(sys.argv[1:])


def start():
    _fix_streams()
    try:
        code = main()
    except SystemExit as exc:
        code = exc.code
    except BaseException:
        os.makedirs(LOGDIR, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(traceback.format_exc() + "\n")
        _fatal("xlop launcher crashed.\nDetails were saved to:\n%s\n\nحدث خطأ غير متوقع. التفاصيل في الملف أعلاه." % LOG)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    start()

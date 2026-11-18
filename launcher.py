"""Entry point for packaged builds (PyInstaller needs a plain script)."""
import multiprocessing
import os
import sys


def _streams():
    """Windowed builds start with no stdout/stderr. When run from a terminal with arguments
    (`Lifeline.exe scan D: ...`), write to that terminal; otherwise send output nowhere."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    if sys.platform == "win32" and len(sys.argv) > 1:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            return
    sink = open(os.devnull, "w")
    sys.stdout = sys.stdout or sink
    sys.stderr = sys.stderr or sink


if __name__ == "__main__":
    multiprocessing.freeze_support()  # must run first: worker processes start through this exe
    _streams()
    from lifeline.__main__ import main
    main()

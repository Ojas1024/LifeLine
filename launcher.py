"""Entry point for packaged builds (PyInstaller needs a plain script)."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()  # must run first: worker processes start through this exe
    from lifeline.__main__ import main
    main()

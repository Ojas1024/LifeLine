"""Start Lifeline: `python -m lifeline` opens the app, `python -m lifeline scan ...` runs the CLI."""
import multiprocessing
import sys


def main():
    multiprocessing.freeze_support()  # scan workers re-enter here in frozen builds
    if len(sys.argv) > 1 and sys.argv[1] in ("list", "scan", "load", "partitions", "trash", "image",
                                             "-h", "--help", "--version"):
        from lifeline.cli import main as cli
        sys.exit(cli())
    from lifeline.ui.main_window import run
    run()


if __name__ == "__main__":
    main()

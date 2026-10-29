"""Frozen Flet entry point — same app, alternate UI framework."""
import sys

from precision_kvm.__main__ import main


if __name__ == "__main__":
    if "gui" in sys.argv and "--ui" not in sys.argv:
        sys.argv += ["--ui", "flet"]
    elif len(sys.argv) == 1:
        sys.argv += ["gui", "--ui", "flet"]
    main()

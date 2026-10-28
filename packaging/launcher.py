"""Frozen GUI entry point; package imports also work outside a checkout."""
import sys

from precision_kvm.__main__ import main


if __name__ == "__main__":
    if len(sys.argv) == 1 and sys.platform == "darwin":
        sys.argv.append("client-gui")
    main()

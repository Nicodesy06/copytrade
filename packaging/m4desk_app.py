"""Punto di ingresso dell'eseguibile PyInstaller (app macOS)."""

import sys

from m4desk.macapp import main

if __name__ == "__main__":
    sys.exit(main())

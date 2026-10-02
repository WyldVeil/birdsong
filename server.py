#!/usr/bin/env python3
"""Birdsong - start here.

    run.bat / ./run.sh            listen and serve the page (runs setup the first time)
    run.bat setup                 change location, microphone, password, who can see it
    run.bat demo                  preview the page with made-up data
    run.bat --help                everything else

(run.bat / run.sh set up a private Python for you; if you already have
Python 3.10+ and the packages in requirements.txt, `python server.py ...`
works the same.)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if sys.version_info < (3, 10):
    sys.exit("Birdsong needs Python 3.10 or newer. Use run.bat / run.sh, which fetch one for you.")

# Never crash on a console that can't show a character (old Windows code pages).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

from birdsong.cli import main  # noqa: E402

if __name__ == "__main__":
    main()

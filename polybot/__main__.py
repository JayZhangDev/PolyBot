"""Lets ``python -m polybot health`` work the same as the installed ``polybot`` command."""

from polybot.cli import main

raise SystemExit(main())

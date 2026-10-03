"""Standalone single-day dispatch CLI (one script to hand to a user).

Runs the full pipeline **in memory** - no database, no cache:

    1 parse-hours  working_hours.xlsx      -> product -> working-hours lookup
    2 parse-workers workers.xlsx           -> workers (each with a 08:00-18:00 day)
    3 parse-tasks  tasks.xlsx + (1)        -> kept install tasks + removed/quantity reports
    4 geocode      AMap (city) or synthetic -> lat/lng for every order + worker
    5 matrix       AMap driving or euclidean-> pairwise travel-time matrix
    6 solve        OR-Tools VRPTW-with-drop -> assignment (best-fit subset)
    7 export       result.xlsx (+ 移除 + 数量警告) + console report

Usage
    python examples/dispatch_cli.py run \
        --workers docs/workers.xlsx --tasks docs/tasks.xlsx \
        --hours docs/working_hours.xlsx --out output.xlsx

Each step is also a sub-command (run them one by one; intermediate data is printed
to the console and written as JSON checkpoints into ``./_run`` so you can inspect
any single step):

    python examples/dispatch_cli.py hours
    python examples/dispatch_cli.py workers
    python examples/dispatch_cli.py tasks
    python examples/dispatch_cli.py geocode       # needs AMAP_API_KEY, else --synthetic
    python examples/dispatch_cli.py matrix        # needs AMAP_API_KEY, else --matrix euclidean
    python examples/dispatch_cli.py solve
    python examples/dispatch_cli.py export

Offline demo (no AMAP_API_KEY -> synthetic coords + euclidean matrix):
    python examples/dispatch_cli.py run --out output.xlsx
"""

import os
import sys

# make the or_engine package importable when run as a plain script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from or_engine.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

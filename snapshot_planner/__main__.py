"""支持 ``python -m snapshot_planner`` 方式运行。"""

from .report import main

if __name__ == "__main__":
    raise SystemExit(main())

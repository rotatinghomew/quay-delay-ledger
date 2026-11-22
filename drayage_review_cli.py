#!/usr/bin/env python3
"""联合复盘工具启动包装：把仓库根目录加入 sys.path 后转交包内 CLI。

可在隔离模式（python3 -I）下直接运行：
    python3 -I drayage_review_cli.py run --out out/run1
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from drayage_review.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

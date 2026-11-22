"""测试包引导。

普通运行（``python3 -m unittest discover -s tests``）时工作区根目录已在
``sys.path``；隔离模式（``python3 -I``）会剥离 cwd，这里显式补回仓库根，
保证业务包与测试夹具有稳定的导入路径。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

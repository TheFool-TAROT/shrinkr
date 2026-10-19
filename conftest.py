"""pytest 根配置：把仓库根目录加进 sys.path，让 tests/ 能 import app 包。"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
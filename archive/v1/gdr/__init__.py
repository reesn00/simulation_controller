import sys
from pathlib import Path

_pkg_root = Path(__file__).resolve().parent
_root_str = str(_pkg_root)

if _root_str not in sys.path:
    sys.path.insert(0, _root_str)

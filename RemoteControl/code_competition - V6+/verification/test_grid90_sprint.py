"""Compatibility entry: execute the current transition regression suite."""
from pathlib import Path
import runpy
runpy.run_path(str(Path(__file__).with_name('test_grid90_transition.py')), run_name='__main__')

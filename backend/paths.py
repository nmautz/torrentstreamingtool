"""The repo root, for code that keeps its files next to the server.

main.py builds paths from `Path(__file__).parent`, which is the repo root there
and backend/ in this package. Moved code uses ROOT instead. It is
`Path(__file__).parent.parent`, deliberately without `.resolve()`, so every path
string is exactly what main.py's own expression gives (relative or absolute, a
junction left as it is).
"""
from pathlib import Path

ROOT = Path(__file__).parent.parent

"""Put the repo root on sys.path so tests can `import tracer_helpers`,
`import tracer_version`, and reach `backend/app/agent.py` via
`from backend.app import agent` without needing a package install.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

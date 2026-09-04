import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["RADAR_DB_PATH"] = str(Path(__file__).resolve().parent / "_test_runtime.sqlite3")
os.environ["RADAR_SCHEDULE_ENABLED"] = "0"

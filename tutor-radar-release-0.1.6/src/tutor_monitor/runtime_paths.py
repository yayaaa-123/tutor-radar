import os
from pathlib import Path

PROJECT_ROOT = Path(os.environ["TUTOR_RADAR_HOME"]).resolve() if "TUTOR_RADAR_HOME" in os.environ else Path(__file__).resolve().parents[2]

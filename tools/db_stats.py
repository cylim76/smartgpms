import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartgpms.database import Database

print(
    Database(
        Path(__file__).resolve().parent.parent / "data" / "smartgpms.sqlite3"
    ).stats()
)

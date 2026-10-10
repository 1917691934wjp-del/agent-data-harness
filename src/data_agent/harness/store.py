"""Optional local persistence of metadata-only request records."""

import json
from pathlib import Path


def save_trace(directory: str, record: dict) -> None:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    # UUID filenames and exclusive creation prevent accidental overwrites.
    with (root / (record["run_id"] + ".json")).open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, allow_nan=False)

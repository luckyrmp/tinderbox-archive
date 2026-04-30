"""Load conversations from a ZIP's conversations.json.

conversations.json is ~239MB uncompressed. Mac Studio has 64GB RAM so
json.load() is fine. If future exports grow past ~1GB, revisit with
a streaming parser.
"""

import json
import zipfile
from typing import Iterator


def stream_conversations(zip_path: str) -> Iterator[dict]:
    """Yield one conversation dict at a time from conversations.json inside a ZIP."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        with zf.open("conversations.json") as f:
            convos = json.load(f)
    yield from convos


def list_zip_contents(zip_path: str) -> list[str]:
    """Return the list of filenames inside the ZIP."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        return [info.filename for info in zf.infolist()]

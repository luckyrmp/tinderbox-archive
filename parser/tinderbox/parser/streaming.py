"""Load conversations from a ZIP's conversations.json.

conversations.json is ~239MB uncompressed. Mac Studio has 64GB RAM so
json.load() is fine. If future exports grow past ~1GB, revisit with
a streaming parser.
"""

import json
import zipfile
from typing import Iterator


def _find_in_zip(zf: zipfile.ZipFile, filename: str) -> str:
    """Return the ZipFile member path for `filename`, searching nested dirs.

    Handles macOS double-wrap where the exporter unzipped the download and
    re-zipped it, creating a nested structure like:
        data-<uuid>/conversations.json

    Raises KeyError if not found.
    """
    names = [info.filename for info in zf.infolist()]
    # Fast path: file at root.
    if filename in names:
        return filename
    # Slow path: find it inside a single subdirectory (macOS re-zip pattern).
    for name in names:
        if name.endswith("/" + filename) and name.count("/") == 1:
            return name
    raise KeyError(f"'{filename}' not found in ZIP (checked {len(names)} entries)")


def stream_conversations(zip_path: str) -> Iterator[dict]:
    """Yield one conversation dict at a time from conversations.json inside a ZIP.

    Tolerates macOS double-wrap where the ZIP contains a single subdirectory
    wrapping all files (e.g. data-<uuid>/conversations.json).
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        member = _find_in_zip(zf, "conversations.json")
        with zf.open(member) as f:
            convos = json.load(f)
    yield from convos


def list_zip_contents(zip_path: str) -> list[str]:
    """Return the list of top-level logical filenames inside the ZIP.

    Strips a single wrapping subdirectory if the ZIP was macOS-double-wrapped,
    so callers always see bare names like 'conversations.json'.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = [info.filename for info in zf.infolist()
                 if not info.filename.startswith("__MACOSX")]
    # Detect macOS wrap: all non-directory entries share a common prefix dir.
    stripped = []
    for name in names:
        if name.endswith("/"):
            continue  # skip directory entries
        parts = name.split("/", 1)
        stripped.append(parts[-1] if len(parts) == 2 else parts[0])
    return stripped

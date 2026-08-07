"""gallery.json must never carry business or private data.

    "M:/Photo-Painting Collages/_tools/venv/Scripts/python.exe" tests/test_gallery_allowlist.py

Run against the real file, so a hand-edit or a future change to publish.py that
widens the projection fails here rather than on a public web page. The source
inventory carries Etsy prices and stock, per-post reach and follower counts,
private curation notes, and the absolute path of the working drive.
"""
from __future__ import annotations

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import publish  # noqa: E402

FORBIDDEN = ("price", "reach", "stock", "curation", "notes", "_tools",
             "Photo-Painting Collages", "listing_title", "follows")


def main():
    with open(os.path.join(REPO, "data", "gallery.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    assert set(data) == {"pieces"}, "gallery.json has an unexpected top-level key"
    allowed = set(publish.GALLERY_KEYS) | {"permalink"}
    for piece in data["pieces"]:
        extra = set(piece) - allowed
        assert not extra, f"key outside the allowlist: {extra}"
    blob = json.dumps(data)
    for word in FORBIDDEN:
        assert word.lower() not in blob.lower(), f"forbidden term in gallery.json: {word}"
    assert not re.search(r"[A-Za-z]:\\\\", blob), "a Windows drive path leaked"
    print(f"gallery.json clean: {len(data['pieces'])} pieces, no forbidden terms")


if __name__ == "__main__":
    main()

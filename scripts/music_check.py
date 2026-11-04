#!/usr/bin/env python3
"""Look a song up in Meta's licensed catalogue (the Audio API, Instagram API with Facebook Login) and print what it
finds. Posts nothing, writes nothing, commits nothing: the music-check workflow runs it so a song can be checked
before a reel that needs it is queued.

    FB_ACCESS_TOKEN=... FB_IG_USER_ID=... python scripts/music_check.py --artist "Kavinsky" --title "Nightcall"

Exit 0: the lookup ran (with or without a match). 2: no Facebook token is set. 1: the API refused the lookup.
The token is never printed.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ig_client import FB_API, IGClient, IGError  # noqa: E402
import publish  # noqa: E402


def main(argv=None, client=None) -> int:
    ap = argparse.ArgumentParser(description="Look a song up in Instagram's licensed catalogue (posts nothing).")
    ap.add_argument("--artist", required=True)
    ap.add_argument("--title", required=True)
    a = ap.parse_args(argv)
    if client is None:
        token, user = os.environ.get("FB_ACCESS_TOKEN", ""), os.environ.get("FB_IG_USER_ID", "")
        if not (token and user):
            print("FB_ACCESS_TOKEN and FB_IG_USER_ID must be set (repository secrets) for the Audio API")
            return 2
        client = IGClient(user, token, api=FB_API)
    try:
        found, exact = publish.find_music(client, a.artist, a.title)
    except IGError as err:
        print(f"the Audio API refused the lookup: {err}")
        return 1
    print(f"search: {a.artist} {a.title} -> {len(found)} result(s)")
    for r in found:
        secs = (r.get("duration_in_ms") or 0) / 1000
        print(f"  {r.get('audio_id')}  {r.get('display_artist')} - {r.get('title')}  "
              f"({r.get('audio_type') or 'music'}, {int(secs // 60)}:{int(secs % 60):02d})")
    if exact:
        print(f"EXACT MATCH: a reel with this song would publish with audio_id {exact[0]['audio_id']}"
              + (f" ({len(exact)} exact matches; the first is used)" if len(exact) > 1 else ""))
    else:
        print("NO EXACT MATCH: a reel with this song would be held (music_unavailable). Use the artist and title "
              "exactly as listed above, or choose another song.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Publish at most one due queue entry to Instagram.

Run from GitHub Actions every 15 minutes. Selects entries where status is
scheduled and scheduled_for has passed, publishes the earliest ONE, records the
outcome back into the queue entry, and appends to data/gallery.json.

One entry per run is both cheaper than crash-safe batching and the anti-
stampede guard: after an outage a backlog drains one per tick instead of firing
six at once -- the exact 2026-04-22 burst this project exists to prevent.

    python scripts/publish.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ig_client import FB_API, IGClient, IGError  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE_DIR = os.path.join(REPO, "queue")
GALLERY = os.path.join(REPO, "data", "gallery.json")
STALE_AFTER = timedelta(hours=24)
CONTAINER_POLL_SECONDS = 5
CONTAINER_POLL_ATTEMPTS = 12
# A video container (a reel, a story clip, a carousel clip) takes Meta minutes to process. Wait up to 9 minutes in
# one run; past that the error is transient, the creation_id is already pushed, and the next run picks it up.
VIDEO_POLL_SECONDS = 10
VIDEO_POLL_ATTEMPTS = 54

# What this publisher can post. queue_post.py reads this line from origin/main before it queues anything, and
# refuses a post whose type is not here (a reel queued before this publisher is deployed would fail at 19:00).
SUPPORTS = ("IMAGE", "CAROUSEL", "CAROUSEL_VIDEO", "REELS", "STORIES", "ALT_TEXT", "MUSIC")
MUSIC_HELD = "music_unavailable"
MEDIA_TYPES = ("IMAGE", "CAROUSEL", "REELS", "STORIES")

AUTH_CODES = {190, 102, 463, 467}
TRANSIENT_CODES = {1, 2, 4, 17, 32, 341}

GALLERY_KEYS = ("id", "title", "year", "series", "images", "alt", "etsy_url")


class AuthAborted(Exception):
    """The token is dead. Abort the whole run and mark nothing failed --
    otherwise one bad token permanently fails every future due post."""


def classify(err: IGError) -> str:
    if err.code in AUTH_CODES or err.type == "OAuthException":
        return "auth"
    if err.status >= 500 or err.status == 0 or err.code in TRANSIENT_CODES:
        return "transient"
    return "validation"


def select_due(entries: list[dict], now: datetime) -> list[dict]:
    due = [e for e in entries
           if e.get("status") == "scheduled"
           and e.get("scheduled_for") is not None
           and e["scheduled_for"] <= now]
    return sorted(due, key=lambda e: e["scheduled_for"])


def gallery_piece(entry: dict) -> dict:
    """Strict allowlist, built key by key -- never a record minus a denylist.

    A naive projection of inventory data publishes Etsy prices, per-post reach,
    private curation notes and the absolute working-drive path.
    """
    g = entry.get("gallery") or {}
    piece = {k: g.get(k) for k in GALLERY_KEYS}
    piece["permalink"] = (f"https://www.instagram.com/p/{entry['post_id']}/"
                          if entry.get("post_id") else None)
    return piece


def _wait_for_container(client, container_id: str, sleep=None, video: bool = False) -> None:
    """A container is not guaranteed ready the instant /media returns; a video one takes minutes."""
    sleep = sleep or time.sleep
    attempts, seconds = ((VIDEO_POLL_ATTEMPTS, VIDEO_POLL_SECONDS) if video
                         else (CONTAINER_POLL_ATTEMPTS, CONTAINER_POLL_SECONDS))
    for _ in range(attempts):
        status = client.container_status(container_id)
        if status in ("FINISHED", "PUBLISHED"):
            return
        if status in ("ERROR", "EXPIRED"):
            raise IGError(400, {"error": {"code": 100,
                                          "message": f"container {status}"}})
        sleep(seconds)
    raise IGError(0, {"error": {"code": 2, "message": "container never finished"}})


def _media_urls(entry: dict) -> list[str]:
    """Every published file's URL: media_url is the first, SHA-pinned; the rest sit beside it."""
    base = entry["media_url"].rsplit("/", 1)[0]
    return [f"{base}/{os.path.basename(m)}" for m in entry["media"]]


def _kinds(entry: dict) -> list[str]:
    kinds = entry.get("media_kinds")
    if isinstance(kinds, list) and len(kinds) == len(entry["media"]):
        return [str(k) for k in kinds]
    return ["video" if str(m).lower().endswith((".mp4", ".mov")) else "image" for m in entry["media"]]


def _alts(entry: dict) -> list:
    alts = entry.get("alt")
    if isinstance(alts, list) and len(alts) == len(entry["media"]):
        return [a if isinstance(a, str) and a.strip() else None for a in alts]
    return [None] * len(entry["media"])


def _create(entry: dict, client, checkpoint=None, sleep=None) -> str:
    """Make the post's container (and a carousel's children) -> its creation_id."""
    media_type = entry.get("media_type")
    if media_type not in MEDIA_TYPES:
        raise IGError(400, {"error": {"code": 100, "message": f"unknown media_type {media_type!r}"}})
    urls, kinds = _media_urls(entry), _kinds(entry)
    if media_type == "CAROUSEL":
        if "media_kinds" not in entry and not isinstance(entry.get("alt"), list):
            return client.create_carousel(urls, entry["caption"])     # an entry from before media_kinds
        children = entry.get("children_ids")
        if not (isinstance(children, list) and len(children) == len(urls)):
            alts = _alts(entry)
            children = [client.create_carousel_item(u, kind=k, alt_text=a if k == "image" else None)
                        for u, k, a in zip(urls, kinds, alts)]
            entry["children_ids"] = children
            if checkpoint:          # children are reused by the next run if this one dies while they process
                checkpoint(entry)
        for child, kind in zip(children, kinds):
            if kind == "video":
                _wait_for_container(client, child, sleep=sleep, video=True)
        return client.create_carousel_container(children, entry["caption"])
    if media_type == "REELS":
        cover = entry.get("cover")
        cover_url = f"{urls[0].rsplit('/', 1)[0]}/{os.path.basename(cover)}" if cover else None
        music = entry.get("music") if isinstance(entry.get("music"), dict) else {}
        if music.get("audio_id"):
            return client.create_reel(urls[0], entry["caption"], cover_url=cover_url,
                                      thumb_offset=entry.get("thumb_offset"),
                                      share_to_feed=entry.get("share_to_feed"),
                                      audio_configuration=audio_configuration(music))
        return client.create_reel(urls[0], entry["caption"], cover_url=cover_url,
                                  thumb_offset=entry.get("thumb_offset"),
                                  share_to_feed=entry.get("share_to_feed"))
    if media_type == "STORIES":
        return client.create_story(urls[0], kind=kinds[0])
    alt = entry.get("alt")
    if isinstance(alt, str) and alt.strip():
        return client.create_container(entry["media_url"], entry["caption"], alt_text=alt)
    return client.create_container(entry["media_url"], entry["caption"])


def _norm(text) -> str:
    """Titles and artists compare after NFKC, case-folding and collapsed spaces: an exact match, not a guess."""
    import unicodedata
    return " ".join(unicodedata.normalize("NFKC", str(text or "")).casefold().split())


def find_music(client, artist: str, title: str) -> tuple[list[dict], list[dict]]:
    """Search Meta's licensed catalogue for `artist` `title`. -> (every result, the exact matches): the same artist
    and the same title, music only (never an original sound that happens to share the name)."""
    found = client.search_audio(f"{artist} {title}", audio_type="music")
    exact = [r for r in found if isinstance(r, dict) and str(r.get("audio_type") or "music").lower() == "music"
             and _norm(r.get("display_artist")) == _norm(artist) and _norm(r.get("title")) == _norm(title)
             and r.get("audio_id")]
    return found, exact


def audio_configuration(music: dict) -> dict:
    """Meta's audio_configuration for the entry's song: audio_id, and the volumes (1-100) when they were set. An
    original_volume of 0 means the queue tool already took the clip's sound out, so no video_volume is sent."""
    conf = {"audio_id": str(music["audio_id"])}
    if music.get("volume"):
        conf["audio_volume"] = int(music["volume"])
    if music.get("original_volume"):
        conf["video_volume"] = int(music["original_volume"])
    return conf


def wants_music(entry: dict) -> bool:
    return entry.get("media_type") == "REELS" and isinstance(entry.get("music"), dict)


def attach_music(entry: dict, music_client) -> str | None:
    """Find the entry's song and record its audio_id. -> None when found, else why not (a plain sentence).
    Raises IGError only for a transient failure (the run retries later)."""
    music = entry["music"]
    if music_client is None:
        return ("no Facebook Login token is set (FB_ACCESS_TOKEN and FB_IG_USER_ID); Meta's Audio API cannot be "
                "reached through Instagram Login")
    try:
        _found, exact = find_music(music_client, music.get("artist", ""), music.get("title", ""))
    except IGError as err:
        if classify(err) == "transient":
            raise
        return f"the Audio API refused the lookup ({err})"
    if not exact:
        return (f"Instagram's licensed catalogue has no exact match for {music.get('artist')} - {music.get('title')} "
                "(check it with the music-check workflow)")
    music["audio_id"] = str(exact[0]["audio_id"])
    music["matched"] = {"title": exact[0].get("title"), "display_artist": exact[0].get("display_artist"),
                        "duration_in_ms": exact[0].get("duration_in_ms")}
    return None


def _is_video(entry: dict) -> bool:
    return entry.get("media_type") in ("REELS", "STORIES", "CAROUSEL") and "video" in _kinds(entry)


def publish_entry(entry: dict, client, now: datetime, checkpoint=None,
                  sleep=None) -> bool:
    """Publish one entry. Returns True if a post went out on this call."""
    # Crash recovery: an entry holding a creation_id must never be re-created.
    if entry.get("creation_id"):
        caption = entry.get("caption") or ""
        if caption:                 # a story has no caption, and is not in /media: its container says instead
            for post in client.recent_media():
                if post.get("caption") == caption:
                    entry["post_id"] = post["id"]
                    entry["published_at"] = now.isoformat()
                    entry["status"] = "published"
                    return False
        creation_id = entry["creation_id"]
        if entry.get("media_type") == "STORIES" and client.container_status(creation_id) == "PUBLISHED":
            entry["published_at"] = now.isoformat()
            entry["status"] = "published"
            return False
    else:
        creation_id = _create(entry, client, checkpoint=checkpoint, sleep=sleep)
        entry["creation_id"] = creation_id
        # Persist and PUSH before media_publish. A marker written to the
        # runner's working copy dies with the runner.
        if checkpoint:
            checkpoint(entry)

    _wait_for_container(client, creation_id, sleep=sleep, video=_is_video(entry))
    entry["post_id"] = client.publish(creation_id)
    entry["published_at"] = now.isoformat()
    entry["status"] = "published"
    entry["error"] = None
    return True


def run(entries: list[dict], client, now: datetime, dry_run: bool = False,
        checkpoint=None, sleep=None, music_client=None) -> list[dict]:
    published = []
    for entry in select_due(entries, now):
        # Standing rule 0.6: every publish path guards. Each path must check the
        # face gate result before posting. queue_post.py writes faces: passed only
        # after its gate; an entry that reached queue/ any other way is parked, not
        # posted. Parking (not just skipping) keeps it from logging every tick; a
        # human re-runs the gate and resets status to scheduled. `continue` so it
        # never uses up the one-per-run slot a good entry behind it is waiting for.
        if entry.get("faces") != "passed":
            entry["status"] = "refused_faces"
            entry["error"] = "no faces: passed; face gate was not run on this entry"
            print(f"refused: queue/{entry.get('slug')}.yaml has no faces: passed "
                  "(no image is posted unless its face gate passed)")
            continue
        if now - entry["scheduled_for"] > STALE_AFTER:
            entry["status"] = "stale"
            entry["error"] = "over 24h late; reschedule by hand"
            print(f"stale: {entry['slug']}")
            continue
        if wants_music(entry) and not dry_run:
            # A reel's song is attached at creation, through Facebook Login, and cannot be added once it is live.
            # No song (no token, no licensed match): HOLD the reel. Silent only when the entry says allow_silent.
            if entry.get("publish_via") == "facebook" and entry.get("creation_id"):
                if music_client is None:
                    entry["status"] = MUSIC_HELD
                    entry["error"] = ("music: its container was made through Facebook Login and no Facebook token "
                                      "is set now; set FB_ACCESS_TOKEN and FB_IG_USER_ID, then status: scheduled")
                    print(f"HELD {entry['slug']}: {entry['error']}")
                    continue
            elif not entry.get("creation_id"):
                try:
                    why = attach_music(entry, music_client)
                except IGError as err:
                    entry["error"] = f"music lookup, will retry: {err}"
                    print(f"transient error looking up the music for {entry['slug']}, will retry: {err}")
                    return published
                if why and entry.get("allow_silent") is True:
                    entry["music"]["result"] = f"posted without the song: {why}"
                    print(f"{entry['slug']}: no song ({why}); allow_silent is set, so it posts without music")
                elif why:
                    entry["status"] = MUSIC_HELD
                    entry["error"] = (f"music: {why}. The reel is held and is never posted silent; set allow_silent: "
                                      "true to post it without the song, or fix the song and set status: scheduled")
                    print(f"HELD {entry['slug']}: {entry['error']}")
                    continue
                else:
                    entry["publish_via"] = "facebook"
        if dry_run:
            if wants_music(entry):
                print(f"DRY RUN: {entry['slug']} has music ({entry['music'].get('artist')} - "
                      f"{entry['music'].get('title')}), looked up when it publishes")
            print(f"DRY RUN would publish {entry['slug']} ({entry.get('media_type')}, "
                  f"{len(entry.get('media') or [])} file(s))")
            print(f"  media_url: {entry['media_url']}")
            print(f"  caption:   {entry['caption'][:120]}...")
            return []
        via = music_client if entry.get("publish_via") == "facebook" else client
        try:
            if publish_entry(entry, via, now, checkpoint=checkpoint, sleep=sleep):
                published.append(entry)
                print(f"published {entry['slug']} -> {entry['post_id']}")
            else:
                print(f"adopted existing post for {entry['slug']}")
        except IGError as err:
            kind = classify(err)
            if kind == "auth":
                raise AuthAborted(str(err)) from None
            entry["error"] = str(err)
            if kind == "validation":
                entry["status"] = "failed"
                print(f"FAILED {entry['slug']}: {err}")
            else:
                print(f"transient error on {entry['slug']}, will retry: {err}")
        return published        # at most one entry per run, success or not
    return published


def load_entries() -> list[tuple[str, dict]]:
    if not os.path.isdir(QUEUE_DIR):
        return []
    out = []
    for name in sorted(os.listdir(QUEUE_DIR)):
        if name.endswith(".yaml"):
            path = os.path.join(QUEUE_DIR, name)
            with open(path, encoding="utf-8") as fh:
                out.append((path, yaml.safe_load(fh)))
    return out


def save_entry(path: str, entry: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(entry, fh, sort_keys=False, allow_unicode=True,
                       default_flow_style=False)


def wants_gallery(entry: dict) -> bool:
    """A queue entry opts out of the artwork gallery with `gallery: false`.

    Photographs (`content_class: photo`) are queued that way: the gallery is
    the artwork site, and a hiking photograph must not appear in it. Every
    existing entry carries a gallery mapping, so absence of the key or a
    mapping keeps today's behaviour.
    """
    return entry.get("gallery") is not False


def append_gallery(entries: list[dict]) -> None:
    entries = [e for e in entries if wants_gallery(e)]
    if not entries:
        return
    with open(GALLERY, encoding="utf-8") as fh:
        data = json.load(fh)
    known = {p.get("permalink") for p in data["pieces"]}
    for entry in entries:
        piece = gallery_piece(entry)
        if piece["permalink"] not in known:
            data["pieces"].append(piece)
    with open(GALLERY, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _git(*args: str) -> None:
    subprocess.run(["git", "-C", REPO, *args], check=False,
                   capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish at most one due queue entry to Instagram.")
    parser.add_argument("--dry-run", action="store_true",
                        help="do everything except the two POSTs")
    args = parser.parse_args()

    loaded = load_entries()
    entries = [e for _, e in loaded]
    paths = {id(e): p for p, e in loaded}
    now = datetime.now(timezone.utc)

    if not entries:
        print("queue is empty")
        return 0

    token = os.environ.get("IG_ACCESS_TOKEN", "")
    user_id = os.environ.get("IG_USER_ID", "")
    if not args.dry_run and not (token and user_id):
        print("IG_ACCESS_TOKEN and IG_USER_ID must be set")
        return 2
    client = IGClient(user_id, token) if token else None
    # Facebook Login, for a reel's song only (everything else keeps Instagram Login). Optional: without it a reel
    # with music is held, never posted silent.
    fb_token, fb_user = os.environ.get("FB_ACCESS_TOKEN", ""), os.environ.get("FB_IG_USER_ID", "")
    music_client = IGClient(fb_user, fb_token, api=FB_API) if fb_token and fb_user else None

    def checkpoint(entry):
        """Commit and push the creation_id before media_publish is called."""
        save_entry(paths[id(entry)], entry)
        _git("add", "queue")
        _git("commit", "-q", "-m", f"wip: container for {entry['slug']}")
        _git("push", "-q")

    try:
        published = run(entries, client, now, dry_run=args.dry_run,
                        checkpoint=checkpoint, music_client=music_client)
    except AuthAborted as exc:
        print(f"AUTH FAILURE, aborting run without failing any entry: {exc}")
        return 3
    finally:
        if not args.dry_run:
            for path, entry in loaded:
                save_entry(path, entry)

    if published:
        append_gallery(published)
    return 0


if __name__ == "__main__":
    sys.exit(main())

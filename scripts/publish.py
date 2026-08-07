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
from ig_client import IGClient, IGError  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE_DIR = os.path.join(REPO, "queue")
GALLERY = os.path.join(REPO, "data", "gallery.json")
STALE_AFTER = timedelta(hours=24)
CONTAINER_POLL_SECONDS = 5
CONTAINER_POLL_ATTEMPTS = 12

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


def _wait_for_container(client, container_id: str, sleep=None) -> None:
    """An image container is not guaranteed ready the instant /media returns."""
    sleep = sleep or time.sleep
    for _ in range(CONTAINER_POLL_ATTEMPTS):
        status = client.container_status(container_id)
        if status in ("FINISHED", "PUBLISHED"):
            return
        if status in ("ERROR", "EXPIRED"):
            raise IGError(400, {"error": {"code": 100,
                                          "message": f"container {status}"}})
        sleep(CONTAINER_POLL_SECONDS)
    raise IGError(0, {"error": {"code": 2, "message": "container never finished"}})


def publish_entry(entry: dict, client, now: datetime, checkpoint=None,
                  sleep=None) -> bool:
    """Publish one entry. Returns True if a post went out on this call."""
    # Crash recovery: an entry holding a creation_id must never be re-created.
    if entry.get("creation_id"):
        for post in client.recent_media():
            if post.get("caption") == entry["caption"]:
                entry["post_id"] = post["id"]
                entry["published_at"] = now.isoformat()
                entry["status"] = "published"
                return False
        creation_id = entry["creation_id"]
    else:
        if entry["media_type"] == "CAROUSEL":
            base = entry["media_url"].rsplit("/", 1)[0]
            urls = [f"{base}/{os.path.basename(m)}" for m in entry["media"]]
            creation_id = client.create_carousel(urls, entry["caption"])
        else:
            creation_id = client.create_container(entry["media_url"],
                                                  entry["caption"])
        entry["creation_id"] = creation_id
        # Persist and PUSH before media_publish. A marker written to the
        # runner's working copy dies with the runner.
        if checkpoint:
            checkpoint(entry)

    _wait_for_container(client, creation_id, sleep=sleep)
    entry["post_id"] = client.publish(creation_id)
    entry["published_at"] = now.isoformat()
    entry["status"] = "published"
    entry["error"] = None
    return True


def run(entries: list[dict], client, now: datetime, dry_run: bool = False,
        checkpoint=None, sleep=None) -> list[dict]:
    published = []
    for entry in select_due(entries, now):
        if now - entry["scheduled_for"] > STALE_AFTER:
            entry["status"] = "stale"
            entry["error"] = "over 24h late; reschedule by hand"
            print(f"stale: {entry['slug']}")
            continue
        if dry_run:
            print(f"DRY RUN would publish {entry['slug']}")
            print(f"  image_url: {entry['media_url']}")
            print(f"  caption:   {entry['caption'][:120]}...")
            return []
        try:
            if publish_entry(entry, client, now, checkpoint=checkpoint, sleep=sleep):
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


def append_gallery(entries: list[dict]) -> None:
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

    def checkpoint(entry):
        """Commit and push the creation_id before media_publish is called."""
        save_entry(paths[id(entry)], entry)
        _git("add", "queue")
        _git("commit", "-q", "-m", f"wip: container for {entry['slug']}")
        _git("push", "-q")

    try:
        published = run(entries, client, now, dry_run=args.dry_run,
                        checkpoint=checkpoint)
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

"""Tests for publish.py, against a fake client. No network.

    "M:/Photo-Painting Collages/_tools/venv/Scripts/python.exe" tests/test_publish.py

The three behaviours that matter: publishing at most one due entry, never
double-posting after a crash, and aborting the whole run on an auth error
instead of marking every due post failed.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))
import ig_client  # noqa: E402
import publish  # noqa: E402

MDT = timezone(timedelta(hours=-6))
NOW = datetime(2026, 8, 20, 19, 5, tzinfo=MDT)
PASSED = 0


def check(condition, label):
    global PASSED
    if not condition:
        raise AssertionError(label)
    PASSED += 1
    print(f"  ok  {label}")


def entry(slug, when, **over):
    e = {"slug": slug, "artwork_id": "001", "scheduled_for": when,
         "media_type": "IMAGE", "media": [f"publish/{slug}.jpg"],
         "caption": f'"{slug}"\n\nbody\n\n#art', "media_url": f"https://x/{slug}.jpg",
         "status": "scheduled", "creation_id": None, "post_id": None,
         "published_at": None, "error": None,
         "gallery": {"id": "001", "title": slug, "year": 2026, "series": None,
                     "alt": "alt", "images": [], "etsy_url": None}}
    e.update(over)
    return e


class FakeClient:
    def __init__(self, fail=None, existing=None):
        self.fail = fail
        self.created = []
        self.published = []
        self.existing = existing or []

    def create_container(self, image_url, caption):
        if self.fail == "create":
            raise ig_client.IGError(400, {"error": {"code": 100, "type": "Invalid"}})
        self.created.append(image_url)
        return f"c{len(self.created)}"

    def create_carousel(self, child_urls, caption):
        self.created.append(tuple(child_urls))
        return f"c{len(self.created)}"

    def container_status(self, container_id):
        return "FINISHED"

    def publish(self, creation_id):
        if self.fail == "auth":
            raise ig_client.IGError(400, {"error": {"code": 190,
                                                    "type": "OAuthException"}})
        if self.fail == "transient":
            raise ig_client.IGError(500, {"error": {"code": 2, "type": "Server"}})
        self.published.append(creation_id)
        return f"post-{creation_id}"

    def recent_media(self, limit=25):
        return self.existing


def test_selects_only_due_scheduled_entries():
    entries = [entry("due", NOW - timedelta(hours=1)),
               entry("future", NOW + timedelta(days=1)),
               entry("done", NOW - timedelta(days=1), status="published")]
    due = publish.select_due(entries, NOW)
    check([e["slug"] for e in due] == ["due"],
          "only the due, scheduled entry selected")


def test_earliest_first_and_one_per_run():
    entries = [entry("b", NOW - timedelta(hours=1)),
               entry("a", NOW - timedelta(hours=5))]
    check(publish.select_due(entries, NOW)[0]["slug"] == "a",
          "earliest due entry first")
    published = publish.run(entries, FakeClient(), NOW, dry_run=False)
    check(len(published) == 1,
          "at most ONE entry per run -- anti-stampede after an outage")


def test_stale_entries_are_not_published():
    e = entry("old", NOW - timedelta(hours=30))
    published = publish.run([e], FakeClient(), NOW, dry_run=False)
    check(published == [], "an entry over 24h late is not published")
    check(e["status"] == "stale", "it is marked stale for a human to reschedule")


def test_happy_path_records_everything():
    e = entry("good", NOW - timedelta(minutes=5))
    published = publish.run([e], FakeClient(), NOW, dry_run=False)
    check(len(published) == 1, "published one")
    check(e["status"] == "published", "status updated")
    check(e["post_id"] == "post-c1", "post id recorded")
    check(e["published_at"] is not None, "published_at recorded")
    check(e["creation_id"] == "c1", "creation_id retained")


def test_creation_id_is_persisted_before_publish():
    """The dangerous window is between media_publish returning and the state
    write. The runner is destroyed on crash, so the marker must be PUSHED
    before the call, not written to the working copy after it."""
    saves = []
    e = entry("crashy", NOW - timedelta(minutes=5))
    publish.run([e], FakeClient(), NOW, dry_run=False,
                checkpoint=lambda x: saves.append(dict(x)))
    check(len(saves) >= 1, "a checkpoint was taken")
    check(saves[0]["creation_id"] is not None, "creation_id present at checkpoint")
    check(saves[0]["post_id"] is None, "checkpoint happened BEFORE media_publish")


def test_never_recreates_a_container_after_a_crash():
    e = entry("resumed", NOW - timedelta(minutes=5), creation_id="c-existing")
    client = FakeClient()
    publish.run([e], client, NOW, dry_run=False)
    check(client.created == [], "no new container created for an entry holding one")
    check(client.published == ["c-existing"], "the existing container is published")


def test_detects_a_post_that_already_went_out():
    e = entry("resumed", NOW - timedelta(minutes=5), creation_id="c-existing")
    client = FakeClient(existing=[{"id": "already", "caption": e["caption"]}])
    published = publish.run([e], client, NOW, dry_run=False)
    check(published == [], "nothing published a second time")
    check(e["post_id"] == "already", "the existing post is adopted, not duplicated")
    check(e["status"] == "published", "and the entry is closed out")


def test_error_classification():
    check(publish.classify(ig_client.IGError(400, {"error": {"code": 190}})) == "auth",
          "190 is auth")
    check(publish.classify(ig_client.IGError(400, {"error": {"code": 102}})) == "auth",
          "102 is auth")
    check(publish.classify(ig_client.IGError(500, {"error": {"code": 2}})) == "transient",
          "5xx is transient")
    check(publish.classify(ig_client.IGError(400, {"error": {"code": 4}})) == "transient",
          "rate-limit code 4 is transient")
    check(publish.classify(ig_client.IGError(0, {"error": {"code": None}})) == "transient",
          "a transport failure is transient")
    check(publish.classify(ig_client.IGError(400, {"error": {"code": 100}})) == "validation",
          "genuine 4xx is validation")


def test_auth_error_aborts_the_run_without_failing_the_entry():
    """A dead token must not permanently mark every due post failed."""
    e = entry("victim", NOW - timedelta(minutes=5))
    try:
        publish.run([e], FakeClient(fail="auth"), NOW, dry_run=False)
        check(False, "should have raised")
    except publish.AuthAborted:
        check(True, "run aborted on auth error")
    check(e["status"] == "scheduled", "the entry is NOT marked failed")


def test_validation_error_fails_the_entry():
    e = entry("bad", NOW - timedelta(minutes=5))
    publish.run([e], FakeClient(fail="create"), NOW, dry_run=False)
    check(e["status"] == "failed", "validation error fails the entry immediately")
    check("100" in str(e["error"]), "the API message is recorded")


def test_transient_error_leaves_it_scheduled():
    e = entry("flaky", NOW - timedelta(minutes=5))
    publish.run([e], FakeClient(fail="transient"), NOW, dry_run=False)
    check(e["status"] == "scheduled", "transient error retries on the next tick")
    check(e["error"] is not None, "but the reason is recorded")


def test_dry_run_makes_no_calls():
    e = entry("dry", NOW - timedelta(minutes=5))
    client = FakeClient()
    published = publish.run([e], client, NOW, dry_run=True)
    check(client.created == [] and client.published == [], "no API calls in dry-run")
    check(published == [], "nothing reported as published")
    check(e["status"] == "scheduled", "entry untouched")


def test_gallery_append_is_a_strict_allowlist():
    """inventory.json carries Etsy prices, reach, private curation notes and the
    absolute working-drive path. gallery.json must be built key by key."""
    e = entry("g", NOW - timedelta(minutes=5), post_id="p1")
    e["gallery"]["price"] = "40.00"
    e["gallery"]["reach"] = 79
    e["gallery"]["folder"] = r"M:\Photo-Painting Collages\001 - x"
    piece = publish.gallery_piece(e)
    check(set(piece) == {"id", "title", "year", "series", "images", "alt",
                         "etsy_url", "permalink"},
          "only allowlisted keys survive")
    blob = str(piece)
    for leak in ("price", "reach", "M:\\", "_tools"):
        check(leak not in blob, f"no {leak} in the published record")


def main():
    test_selects_only_due_scheduled_entries()
    test_earliest_first_and_one_per_run()
    test_stale_entries_are_not_published()
    test_happy_path_records_everything()
    test_creation_id_is_persisted_before_publish()
    test_never_recreates_a_container_after_a_crash()
    test_detects_a_post_that_already_went_out()
    test_error_classification()
    test_auth_error_aborts_the_run_without_failing_the_entry()
    test_validation_error_fails_the_entry()
    test_transient_error_leaves_it_scheduled()
    test_dry_run_makes_no_calls()
    test_gallery_append_is_a_strict_allowlist()
    print(f"\n{PASSED} checks passed")


if __name__ == "__main__":
    main()

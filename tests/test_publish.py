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
         "faces": "passed",
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


def test_due_entry_without_faces_passed_is_refused():
    """Standing rule 0.6: EVERY publish path guards.
    queue_post.py writes faces: passed only after its gate; anything else that
    reaches queue/ (hand edit, another tool) must not be published."""
    import io
    import contextlib
    for label, over in (("missing", None), ("not passed", "refused"),
                        ("blank", ""), ("passed-ish", "Passed")):
        e = entry("nofaces", NOW - timedelta(minutes=5))
        if over is None:
            del e["faces"]
        else:
            e["faces"] = over
        client = FakeClient()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            published = publish.run([e], client, NOW, dry_run=False)
        check(published == [] and client.created == [] and client.published == [],
              f"faces {label}: nothing published, no API call")
        check(e["status"] == "refused_faces", f"faces {label}: status refused_faces")
        out = buf.getvalue().strip().splitlines()
        check(len(out) == 1 and "nofaces.yaml" in out[0] and "no faces: passed" in out[0],
              f"faces {label}: one log line naming the file and reason")


def test_refused_entry_does_not_block_the_next_due_one_or_repeat():
    bad = entry("bad", NOW - timedelta(hours=3))
    del bad["faces"]
    good = entry("good", NOW - timedelta(hours=1))
    client = FakeClient()
    published = publish.run([bad, good], client, NOW, dry_run=False)
    check([e["slug"] for e in published] == ["good"],
          "an unguarded entry does not use up the one-per-run slot")
    check(bad["status"] == "refused_faces", "the refused one is parked")
    check(publish.select_due([bad], NOW) == [],
          "and is not selected again on the next tick")


def test_due_entry_with_faces_passed_publishes_as_before():
    e = entry("ok", NOW - timedelta(minutes=5), faces="passed")
    client = FakeClient()
    published = publish.run([e], client, NOW, dry_run=False)
    check(len(published) == 1 and client.published == ["c1"], "published via the stub")
    check(e["status"] == "published" and e["post_id"] == "post-c1", "recorded as before")


def test_not_due_entry_without_faces_is_untouched():
    e = entry("later", NOW + timedelta(days=1))
    del e["faces"]
    before = dict(e)
    published = publish.run([e], FakeClient(), NOW, dry_run=False)
    check(published == [] and e == before, "a future entry is left exactly as it was")


def test_a_photo_entry_skips_the_gallery():
    """`gallery: false` is how queue_post.py --kind photo says "not artwork":
    the entry publishes to Instagram but never reaches data/gallery.json."""
    import json
    import tempfile
    photo = entry("hike-photo", NOW - timedelta(minutes=5), post_id="p-photo",
                  gallery=False, content_class="photo")
    art = entry("an-artwork", NOW - timedelta(minutes=5), post_id="p-art")
    check(publish.wants_gallery(photo) is False, "gallery: false opts out")
    check(publish.wants_gallery(art) is True, "a gallery mapping opts in")
    check(publish.wants_gallery({"slug": "old"}) is True,
          "an old entry with no gallery key keeps the old behaviour")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "gallery.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pieces": []}, fh)
        old = publish.GALLERY
        publish.GALLERY = path
        try:
            publish.append_gallery([photo])
            with open(path, encoding="utf-8") as fh:
                check(json.load(fh)["pieces"] == [], "a photo adds no gallery piece")
            publish.append_gallery([photo, art])
            with open(path, encoding="utf-8") as fh:
                pieces = json.load(fh)["pieces"]
            check(len(pieces) == 1 and pieces[0]["title"] == "an-artwork",
                  "an artwork queued beside a photo still lands")
        finally:
            publish.GALLERY = old


# ---------------------------------------------------------------- every type Meta's API takes
# Carousels of pictures and clips (with alt text on the pictures), reels, stories, alt text on a single picture.

class FakeClientAll(FakeClient):
    """Records every create call with its arguments. `statuses` maps a container id to the statuses it reports in
    turn (a clip is IN_PROGRESS for a while); the last one repeats."""

    def __init__(self, statuses=None, **kw):
        super().__init__(**kw)
        self.calls = []
        self.statuses = statuses or {}
        self.polled = []

    def _new(self, what, **args):
        self.calls.append((what, args))
        self.created.append(what)
        return f"c{len(self.created)}"

    def create_container(self, image_url, caption, alt_text=None):
        return self._new("image", url=image_url, caption=caption, alt_text=alt_text)

    def create_carousel(self, child_urls, caption):
        return self._new("old-carousel", urls=list(child_urls), caption=caption)

    def create_carousel_item(self, url, kind="image", alt_text=None):
        return self._new("item", url=url, kind=kind, alt_text=alt_text)

    def create_carousel_container(self, children, caption):
        return self._new("carousel", children=list(children), caption=caption)

    def create_reel(self, video_url, caption, cover_url=None, thumb_offset=None, share_to_feed=None):
        return self._new("reel", url=video_url, caption=caption, cover_url=cover_url, thumb_offset=thumb_offset,
                         share_to_feed=share_to_feed)

    def create_story(self, url, kind="image"):
        return self._new("story", url=url, kind=kind)

    def container_status(self, container_id):
        self.polled.append(container_id)
        seq = self.statuses.get(container_id) or ["FINISHED"]
        return seq.pop(0) if len(seq) > 1 else seq[0]


def media_entry(slug, media_type, media, **over):
    e = entry(slug, NOW - timedelta(minutes=5), media_type=media_type, media=media,
              media_url=f"https://raw/x/sha/{media[0]}", gallery=False)
    e.update(over)
    return e


def test_the_supports_line_names_every_type():
    check(set(publish.SUPPORTS) == {"IMAGE", "CAROUSEL", "CAROUSEL_VIDEO", "REELS", "STORIES", "ALT_TEXT"},
          "SUPPORTS names every post type (queue_post.py reads it from origin/main)")


def test_a_carousel_of_pictures_and_clips_with_alt_text():
    e = media_entry("ch", "CAROUSEL", ["publish/ch-01.jpg", "publish/ch-02.mp4", "publish/ch-03.jpg"],
                    media_kinds=["image", "video", "image"], alt=["A ridge.", "", ""])
    client = FakeClientAll(statuses={"c2": ["IN_PROGRESS", "IN_PROGRESS", "FINISHED"]})
    saves, slept = [], []
    published = publish.run([e], client, NOW, checkpoint=lambda x: saves.append(dict(x)),
                            sleep=lambda s: slept.append(s))
    items = [a for w, a in client.calls if w == "item"]
    check(len(published) == 1 and e["status"] == "published", "published")
    check([i["kind"] for i in items] == ["image", "video", "image"]
          and items[1]["url"] == "https://raw/x/sha/publish/ch-02.mp4", "one child per file, a clip as a video child")
    check(items[0]["alt_text"] == "A ridge." and items[1]["alt_text"] is None and items[2]["alt_text"] is None,
          "alt text on the pictures that have one, never on a clip")
    carousel = next(a for w, a in client.calls if w == "carousel")
    check(carousel["children"] == ["c1", "c2", "c3"] and carousel["caption"] == e["caption"],
          "the carousel container is made from the children, with the caption")
    check(saves and saves[0]["children_ids"] == ["c1", "c2", "c3"] and saves[0]["creation_id"] is None,
          "the children are checkpointed before the wait (a crash reuses them)")
    check(slept and set(slept) == {publish.VIDEO_POLL_SECONDS}, "the clip child is waited for at the video pace")
    check(e["creation_id"] == "c4" and e["post_id"] == "post-c4", "creation and post ids recorded")


def test_children_are_reused_after_a_crash():
    e = media_entry("ch", "CAROUSEL", ["publish/ch-01.jpg", "publish/ch-02.jpg"], media_kinds=["image", "image"],
                    alt=["", ""], children_ids=["k1", "k2"])
    client = FakeClientAll()
    publish.run([e], client, NOW)
    check([w for w, _ in client.calls] == ["carousel"] and client.calls[0][1]["children"] == ["k1", "k2"],
          "no child is made twice: the checkpointed ones are used")


def test_an_older_picture_carousel_entry_takes_the_old_call():
    e = media_entry("old", "CAROUSEL", ["publish/old-1.jpg", "publish/old-2.jpg"])
    client = FakeClientAll()
    publish.run([e], client, NOW)
    check([w for w, _ in client.calls] == ["old-carousel"], "an entry with no media_kinds is posted exactly as before")


def test_a_reel():
    e = media_entry("r", "REELS", ["publish/r.mp4"], media_kinds=["video"], cover="publish/r-cover.jpg",
                    thumb_offset=None, share_to_feed=True)
    client = FakeClientAll(statuses={"c1": ["IN_PROGRESS", "FINISHED"]})
    slept = []
    published = publish.run([e], client, NOW, sleep=lambda s: slept.append(s))
    w, a = client.calls[0]
    check(len(published) == 1 and w == "reel" and a["url"] == "https://raw/x/sha/publish/r.mp4", "a REELS container")
    check(a["cover_url"] == "https://raw/x/sha/publish/r-cover.jpg" and a["share_to_feed"] is True, "cover and feed share")
    check(slept == [publish.VIDEO_POLL_SECONDS], "waited for at the video pace")
    e2 = media_entry("r2", "REELS", ["publish/r2.mp4"], media_kinds=["video"], cover=None, thumb_offset=1500,
                     share_to_feed=False)
    client = FakeClientAll()
    publish.run([e2], client, NOW)
    check(client.calls[0][1]["thumb_offset"] == 1500 and client.calls[0][1]["cover_url"] is None
          and client.calls[0][1]["share_to_feed"] is False, "thumb offset when there is no cover")


def test_stories():
    for media, kind in ((["publish/s.jpg"], "image"), (["publish/s.mp4"], "video")):
        e = media_entry("s", "STORIES", media, media_kinds=[kind], caption="")
        client = FakeClientAll()
        published = publish.run([e], client, NOW, sleep=lambda s: None)
        check(len(published) == 1 and client.calls[0] == ("story", {"url": f"https://raw/x/sha/{media[0]}",
                                                                    "kind": kind}), f"a {kind} story")


def test_a_story_already_published_is_adopted_not_posted_twice():
    e = media_entry("s", "STORIES", ["publish/s.jpg"], media_kinds=["image"], caption="", creation_id="c-held")
    client = FakeClientAll(statuses={"c-held": ["PUBLISHED"]}, existing=[{"id": "other", "caption": ""}])
    published = publish.run([e], client, NOW)
    check(published == [] and client.published == [] and e["status"] == "published",
          "its container says PUBLISHED: adopted, no second media_publish")
    check(e["post_id"] is None, "an empty caption never matches some other post")


def test_a_single_picture_sends_its_alt_text():
    e = media_entry("p", "IMAGE", ["publish/p.jpg"], alt="Red rock ridge under a clear sky")
    client = FakeClientAll()
    publish.run([e], client, NOW)
    check(client.calls[0][1]["alt_text"] == "Red rock ridge under a clear sky", "alt text sent with the picture")
    e = entry("art", NOW - timedelta(minutes=5))
    client = FakeClient()
    publish.run([e], client, NOW)
    check(client.published == ["c1"], "an entry with no alt is posted with the old two-argument call")


def test_a_clip_still_processing_is_transient_and_resumes():
    e = media_entry("r", "REELS", ["publish/r.mp4"], media_kinds=["video"], cover=None, thumb_offset=None,
                    share_to_feed=True)
    client = FakeClientAll(statuses={"c1": ["IN_PROGRESS"]})
    publish.run([e], client, NOW, sleep=lambda s: None)
    check(e["status"] == "scheduled" and e["creation_id"] == "c1" and "never finished" in (e["error"] or ""),
          "still processing after the wait: left scheduled, its container kept")
    check(len(client.polled) == publish.VIDEO_POLL_ATTEMPTS, "polled for the whole video budget")
    client2 = FakeClientAll()
    published = publish.run([e], client2, NOW)
    check(len(published) == 1 and [w for w, _ in client2.calls] == [], "the next run publishes it, making nothing new")


def test_an_unknown_media_type_fails_the_entry():
    e = media_entry("x", "IGTV", ["publish/x.mp4"])
    client = FakeClientAll()
    publish.run([e], client, NOW)
    check(e["status"] == "failed" and "unknown media_type" in e["error"] and client.calls == [], "refused, nothing made")


def test_the_client_sends_what_meta_documents():
    sent = []

    class Rec(ig_client.IGClient):
        def __init__(self):
            self.user_id = "U"

        def _request(self, method, path, **kw):
            sent.append((method, path, kw.get("json")))
            return {"id": f"id{len(sent)}"}
    c = Rec()
    c.create_container("https://i/1.jpg", "cap", alt_text="alt")
    c.create_carousel_item("https://i/2.jpg", "image", alt_text="alt2")
    c.create_carousel_item("https://i/3.mp4", "video", alt_text="ignored")
    c.create_carousel_container(["a", "b"], "cap")
    c.create_reel("https://i/4.mp4", "cap", cover_url="https://i/c.jpg", thumb_offset=99, share_to_feed=True)
    c.create_story("https://i/5.mp4", "video")
    bodies = [b for _, _, b in sent]
    check(all(m == "POST" and p == "U/media" for m, p, _ in sent), "every create is POST /<user>/media")
    check(bodies[0] == {"image_url": "https://i/1.jpg", "caption": "cap", "alt_text": "alt"}, "an image with alt_text")
    check(bodies[1] == {"is_carousel_item": True, "image_url": "https://i/2.jpg", "alt_text": "alt2"},
          "a picture child with alt_text")
    check(bodies[2] == {"is_carousel_item": True, "media_type": "VIDEO", "video_url": "https://i/3.mp4"},
          "a clip child: media_type VIDEO, video_url, no alt_text")
    check(bodies[3] == {"media_type": "CAROUSEL", "children": "a,b", "caption": "cap"}, "the carousel container")
    check(bodies[4] == {"media_type": "REELS", "video_url": "https://i/4.mp4", "caption": "cap",
                        "cover_url": "https://i/c.jpg", "share_to_feed": True}, "a reel: the cover wins over the offset")
    check(bodies[5] == {"media_type": "STORIES", "video_url": "https://i/5.mp4"}, "a story: no caption")


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
    test_due_entry_without_faces_passed_is_refused()
    test_refused_entry_does_not_block_the_next_due_one_or_repeat()
    test_due_entry_with_faces_passed_publishes_as_before()
    test_not_due_entry_without_faces_is_untouched()
    test_a_photo_entry_skips_the_gallery()
    test_the_supports_line_names_every_type()
    test_a_carousel_of_pictures_and_clips_with_alt_text()
    test_children_are_reused_after_a_crash()
    test_an_older_picture_carousel_entry_takes_the_old_call()
    test_a_reel()
    test_stories()
    test_a_story_already_published_is_adopted_not_posted_twice()
    test_a_single_picture_sends_its_alt_text()
    test_a_clip_still_processing_is_transient_and_resumes()
    test_an_unknown_media_type_fails_the_entry()
    test_the_client_sends_what_meta_documents()
    print(f"\n{PASSED} checks passed")


if __name__ == "__main__":
    main()

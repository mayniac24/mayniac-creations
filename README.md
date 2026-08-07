# mayniac-creations

Public gallery and publishing host for [Mayniac Creations](https://mayniacart.etsy.com)
photo-painting collages by Evan Maynard.

`publish/` holds the exact image bytes Instagram fetches — the Content Publishing API can
only pull media from a public HTTPS URL. `queue/` holds scheduled posts, one YAML file per
entry. A GitHub Actions cron publishes at most one due entry every 15 minutes.

**Everything in this repo is public, including queued-but-unposted work.** Adding a piece
to `queue/` publishes it: the image sits at a public URL and its caption and scheduled date
are readable from the moment it is pushed. That is accepted and intentional.

Tooling that fills the queue lives on the artist's private drive and is not in this repo.

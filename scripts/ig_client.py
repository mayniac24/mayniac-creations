"""The only module in this repo that makes HTTP calls.

Isolated so publish.py can be tested against a fake with no network. Host is
graph.instagram.com -- the Instagram-Login path. graph.facebook.com is the
other API and takes different permission names entirely.

The token NEVER appears in a URL. Meta accepts ?access_token=, and a requests
exception prints the full URL -- into a world-readable public-repo log.
"""
from __future__ import annotations

import requests

API = "https://graph.instagram.com/v25.0"
TIMEOUT = 30


class IGError(Exception):
    """An API error, carrying enough to classify it. Never carries the token."""

    def __init__(self, status: int, payload: dict):
        err = (payload or {}).get("error", {})
        self.status = status
        self.code = err.get("code")
        self.subcode = err.get("error_subcode")
        self.message = err.get("message", "")
        self.type = err.get("type", "")
        super().__init__(f"HTTP {status} code={self.code} {self.type}: {self.message}")


class IGClient:
    def __init__(self, user_id: str, token: str):
        self.user_id = user_id
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self._session.request(method, f"{API}/{path}",
                                         timeout=TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            # Re-raise without the original, whose text can contain the URL.
            raise IGError(0, {"error": {"message": type(exc).__name__,
                                        "type": "transport"}}) from None
        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except ValueError:
                payload = {"error": {"message": resp.text[:200]}}
            raise IGError(resp.status_code, payload)
        return resp.json()

    def me(self) -> dict:
        return self._request("GET", "me", params={"fields": "id,username"})

    def create_container(self, image_url: str, caption: str, alt_text: str | None = None) -> str:
        """A single-image post. `alt_text` (up to 1000 characters) is Meta's, for images only."""
        body = {"image_url": image_url, "caption": caption}
        if alt_text:
            body["alt_text"] = alt_text
        data = self._request("POST", f"{self.user_id}/media", json=body)
        return data["id"]

    def create_carousel(self, child_urls: list[str], caption: str) -> str:
        children = [
            self._request("POST", f"{self.user_id}/media",
                          json={"image_url": url, "is_carousel_item": True})["id"]
            for url in child_urls
        ]
        data = self._request("POST", f"{self.user_id}/media",
                             json={"media_type": "CAROUSEL",
                                   "children": ",".join(children),
                                   "caption": caption})
        return data["id"]

    def create_carousel_item(self, url: str, kind: str = "image", alt_text: str | None = None) -> str:
        """One carousel child: a picture (image_url, with its alt text) or a clip (media_type VIDEO, video_url).
        A reel cannot be a carousel item; video children take no alt text."""
        body = {"is_carousel_item": True}
        if kind == "video":
            body.update(media_type="VIDEO", video_url=url)
        else:
            body["image_url"] = url
            if alt_text:
                body["alt_text"] = alt_text
        return self._request("POST", f"{self.user_id}/media", json=body)["id"]

    def create_carousel_container(self, children: list[str], caption: str) -> str:
        """The carousel itself, from up to 20 finished child containers."""
        data = self._request("POST", f"{self.user_id}/media",
                             json={"media_type": "CAROUSEL", "children": ",".join(children),
                                   "caption": caption})
        return data["id"]

    def create_reel(self, video_url: str, caption: str, cover_url: str | None = None,
                    thumb_offset: int | None = None, share_to_feed: bool | None = None) -> str:
        """A reel. A cover_url wins over thumb_offset (ms into the clip)."""
        body = {"media_type": "REELS", "video_url": video_url, "caption": caption}
        if cover_url:
            body["cover_url"] = cover_url
        elif thumb_offset is not None:
            body["thumb_offset"] = int(thumb_offset)
        if share_to_feed is not None:
            body["share_to_feed"] = bool(share_to_feed)
        return self._request("POST", f"{self.user_id}/media", json=body)["id"]

    def create_story(self, url: str, kind: str = "image") -> str:
        """A story: one picture or one clip. Meta takes no caption on a story."""
        body = {"media_type": "STORIES", ("video_url" if kind == "video" else "image_url"): url}
        return self._request("POST", f"{self.user_id}/media", json=body)["id"]

    def container_status(self, container_id: str) -> str:
        """EXPIRED | ERROR | FINISHED | IN_PROGRESS | PUBLISHED."""
        data = self._request("GET", container_id, params={"fields": "status_code"})
        return data.get("status_code", "")

    def publish(self, creation_id: str) -> str:
        data = self._request("POST", f"{self.user_id}/media_publish",
                             json={"creation_id": creation_id})
        return data["id"]

    def recent_media(self, limit: int = 25) -> list[dict]:
        """Used only for crash recovery: did a post with this caption go out?"""
        data = self._request("GET", f"{self.user_id}/media",
                             params={"fields": "id,caption,timestamp",
                                     "limit": limit})
        return data.get("data", [])

    def publishing_limit(self) -> dict:
        return self._request("GET", f"{self.user_id}/content_publishing_limit",
                             params={"fields": "config,quota_usage"})

#!/usr/bin/env python3
"""Refresh the long-lived Instagram token and store the new one as a secret.

Weekly, not monthly. A long-lived token lasts 60 days and can only be refreshed
while still valid and at least 24 hours old. Miss the window and there is NO
programmatic recovery -- only a full manual re-auth through the Meta dashboard.
A monthly cadence puts one missed run inside the danger zone.

GITHUB_TOKEN has no secrets scope, so writing the new token back needs the
fine-grained PAT in GH_SECRETS_PAT.
"""
from __future__ import annotations

import os
import subprocess
import sys

import requests

MIN_SAFE_DAYS = 21
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"


def main() -> int:
    token = os.environ.get("IG_ACCESS_TOKEN", "")
    if not token:
        print("IG_ACCESS_TOKEN is not set")
        return 2

    # This endpoint is the one place Meta requires the token as a query
    # parameter. requests keeps it out of the exception text because nothing
    # here raises on status -- never add raise_for_status(), and never print
    # resp.url. This log is public.
    resp = requests.get(REFRESH_URL,
                        params={"grant_type": "ig_refresh_token",
                                "access_token": token},
                        timeout=30)
    if resp.status_code != 200:
        print(f"REFRESH FAILED: HTTP {resp.status_code} {resp.text[:300]}")
        return 1

    data = resp.json()
    days = data.get("expires_in", 0) // 86400
    print(f"refreshed, valid for {days} days")

    new_token = data["access_token"]
    result = subprocess.run(
        ["gh", "secret", "set", "IG_ACCESS_TOKEN", "--body", new_token],
        capture_output=True, text=True,
        env={**os.environ, "GH_TOKEN": os.environ.get("GH_SECRETS_PAT", "")})
    if result.returncode != 0:
        # stderr could echo the command; print only the tail and never the body.
        print("FAILED to write the secret. The token was refreshed but NOT "
              "stored -- the stored one still works until it expires.")
        print(f"gh exit {result.returncode}: {result.stderr.strip()[-200:]}")
        return 1
    print("secret updated")

    if days < MIN_SAFE_DAYS:
        print(f"WARNING: only {days} days of validity after a refresh -- "
              f"expected ~60. Something is wrong with the token.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

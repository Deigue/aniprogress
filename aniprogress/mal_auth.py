"""One-time MAL OAuth2 (PKCE) helper.

    python -m aniprogress.mal_auth --client-id YOUR_MAL_CLIENT_ID

MAL's PKCE implementation only supports the `plain` method, so the verifier and
the challenge are the same string. Prints the tokens to paste into your compose
file; nothing is written to disk.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import urllib.parse
from urllib.request import Request, urlopen

AUTH = "https://myanimelist.net/v1/oauth2/authorize"
TOKEN = "https://myanimelist.net/v1/oauth2/token"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", default="", help="only if your app has one")
    ap.add_argument("--redirect-uri", default="http://localhost")
    args = ap.parse_args()

    # MAL supports code_challenge_method=plain only: verifier == challenge.
    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")[:128]

    url = AUTH + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": args.client_id,
        "code_challenge": verifier,
        "code_challenge_method": "plain",
        "redirect_uri": args.redirect_uri,
    })
    print("\n1. Open this URL and approve:\n")
    print(url)
    print("\n2. You'll be redirected to a URL that fails to load. That's fine —")
    print("   copy the value of ?code=... from the address bar.\n")
    code = input("   code = ").strip()

    fields = {
        "client_id": args.client_id,
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": args.redirect_uri,
    }
    if args.client_secret:
        fields["client_secret"] = args.client_secret

    req = Request(TOKEN, data=urllib.parse.urlencode(fields).encode(),
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())

    print("\n--- add these to your compose environment ---")
    print(f"MAL_CLIENT_ID:     {args.client_id}")
    print(f"MAL_TOKEN:         {d.get('access_token')}")
    print(f"MAL_REFRESH_TOKEN: {d.get('refresh_token')}")
    print(f"\n(access token expires in {d.get('expires_in')}s; the service "
          f"refreshes it automatically using the refresh token)")


if __name__ == "__main__":
    main()

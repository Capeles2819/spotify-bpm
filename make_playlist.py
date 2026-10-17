"""Create a Spotify playlist on your own profile filled with tracks in a BPM range.

Usage:
    python make_playlist.py                       # 170-190 BPM, default searches
    python make_playlist.py --min 160 --max 180
    python make_playlist.py --min 85 --max 95 --name "Easy spin"
    python make_playlist.py --query "180 bpm running" --query "170 bpm"
    python make_playlist.py --dry-run              # show what would be added, create nothing

First run opens your browser so you can approve the app (one time).
Spotify app settings must list this Redirect URI exactly:
    http://127.0.0.1:8888/callback

Tempo comes from GetSongBPM.com (see find_high_tempo.py); tracks it does not
know are skipped.
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from find_high_tempo import (  # noqa: E402
    API, load_env, lookup_bpm, normalize_tempo, playlist_tracks, search_playlists,
)

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-modify-public playlist-modify-private"
TOKEN_FILE = Path(__file__).with_name(".spotify_token.json")

DEFAULT_QUERIES = [
    "180 bpm running", "175 bpm running", "170 bpm running",
    "high tempo workout", "fast running songs",
]


# ---------------------------------------------------------------- auth (PKCE)

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    code = None
    error = None
    expected_state = None
    timeout = 5  # drop idle/speculative browser connections instead of blocking

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        if "code" in qs and qs.get("state", [None])[0] == _CallbackHandler.expected_state:
            _CallbackHandler.code = qs["code"][0]
            body = "<h2>Spotify login done. You can close this tab.</h2>"
        elif "code" in qs:
            _CallbackHandler.error = "state mismatch (an old login tab was used; approve the newest one)"
            body = f"<h2>Login failed: {_CallbackHandler.error}</h2>"
        else:
            _CallbackHandler.error = qs.get("error", ["unknown"])[0]
            body = f"<h2>Login failed: {_CallbackHandler.error}</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body.encode())
        print(f"callback received: {'ok' if _CallbackHandler.code else _CallbackHandler.error}")

    def log_message(self, *args):  # silence request logging
        pass


def _login(client_id):
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    })

    _CallbackHandler.code = None
    _CallbackHandler.error = None
    _CallbackHandler.expected_state = state
    server = http.server.HTTPServer(("127.0.0.1", 8888), _CallbackHandler)
    server.timeout = 1  # handle_request() returns every second so we can check the clock

    print("Opening your browser to approve the app. If it does not open, visit:\n")
    print(url, "\n")
    webbrowser.open(url)

    deadline = time.time() + 300
    while _CallbackHandler.code is None and _CallbackHandler.error is None:
        if time.time() > deadline:
            server.server_close()
            sys.exit("Timed out waiting for the Spotify login (5 min).")
        server.handle_request()  # one request (or 1 s timeout), on this thread
    server.server_close()
    if _CallbackHandler.error:
        sys.exit(f"Spotify login failed: {_CallbackHandler.error}")
    print("exchanging code for token...")

    r = requests.post("https://accounts.spotify.com/api/token", data={
        "grant_type": "authorization_code",
        "code": _CallbackHandler.code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }, timeout=15)
    r.raise_for_status()
    tok = r.json()
    tok["expires_at"] = time.time() + tok["expires_in"] - 60
    TOKEN_FILE.write_text(json.dumps(tok))
    return tok


def _refresh(client_id, tok):
    r = requests.post("https://accounts.spotify.com/api/token", data={
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": client_id,
    }, timeout=15)
    if r.status_code != 200:
        return None
    new = r.json()
    new.setdefault("refresh_token", tok["refresh_token"])
    new["expires_at"] = time.time() + new["expires_in"] - 60
    TOKEN_FILE.write_text(json.dumps(new))
    return new


def user_token():
    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    if TOKEN_FILE.exists():
        tok = json.loads(TOKEN_FILE.read_text())
        if tok.get("expires_at", 0) > time.time():
            return tok["access_token"]
        tok = _refresh(client_id, tok)
        if tok:
            return tok["access_token"]
    return _login(client_id)["access_token"]


# ------------------------------------------------------------- track gather

def gather_tracks(session, bpm_session, queries, lo, hi, per_query=4, per_playlist=50):
    seen, keep = set(), []
    for q in queries:
        print(f"\nSearching playlists: '{q}'")
        for p in search_playlists(session, q, limit=per_query):
            print(f"  scanning [{p['name']}] ({p['tracks']['total']} tracks)")
            for t in playlist_tracks(session, p["id"], limit=per_playlist):
                if t["id"] in seen:
                    continue
                seen.add(t["id"])
                raw, note = lookup_bpm(bpm_session, t["name"], [a["name"] for a in t["artists"]])
                bpm, _ = normalize_tempo(raw)
                if bpm is not None and lo <= bpm <= hi:
                    artists = ", ".join(a["name"] for a in t["artists"])
                    keep.append({"id": t["id"], "name": t["name"], "artists": artists, "bpm": bpm})
                    print(f"     + {bpm:>3} BPM  {t['name']}  -  {artists}")
    keep.sort(key=lambda x: x["bpm"])
    return keep


# ---------------------------------------------------------------- playlist

def create_playlist(session, name, description, public, track_ids):
    me = session.get(f"{API}/me", timeout=15)
    me.raise_for_status()
    user_id = me.json()["id"]

    r = session.post(f"{API}/users/{user_id}/playlists", json={
        "name": name, "description": description, "public": public,
    }, timeout=15)
    r.raise_for_status()
    pl = r.json()

    uris = [f"spotify:track:{i}" for i in track_ids]
    for i in range(0, len(uris), 100):
        r = session.post(f"{API}/playlists/{pl['id']}/tracks",
                         json={"uris": uris[i:i + 100]}, timeout=15)
        r.raise_for_status()
    return pl


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min", type=int, default=170, help="lowest BPM to include (default 170)")
    ap.add_argument("--max", type=int, default=190, help="highest BPM to include (default 190)")
    ap.add_argument("--query", action="append", help="playlist search term (repeatable)")
    ap.add_argument("--name", help="playlist name (default auto)")
    ap.add_argument("--private", action="store_true", help="make the playlist private")
    ap.add_argument("--dry-run", action="store_true", help="list tracks but create nothing")
    args = ap.parse_args()

    load_env()
    if not os.environ.get("GETSONGBPM_API_KEY"):
        sys.exit("GETSONGBPM_API_KEY missing from .env; tempo filtering needs it.")

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {user_token()}"
    bpm_session = requests.Session()
    bpm_session.headers["User-Agent"] = "spotify-bpm-script/1.0"

    queries = args.query or DEFAULT_QUERIES
    tracks = gather_tracks(session, bpm_session, queries, args.min, args.max)

    print(f"\n{len(tracks)} tracks between {args.min} and {args.max} BPM")
    if not tracks:
        sys.exit("Nothing to add.")
    if args.dry_run:
        print("Dry run; no playlist created.")
        return

    name = args.name or f"{args.min}-{args.max} BPM (auto)"
    desc = f"Tracks {args.min}-{args.max} BPM. Tempo data from GetSongBPM.com. Built with spotify-bpm."
    pl = create_playlist(session, name, desc, not args.private, [t["id"] for t in tracks])
    print(f"\nCreated playlist '{pl['name']}' with {len(tracks)} tracks:")
    print(pl["external_urls"]["spotify"])
    print("\nTempo data from GetSongBPM.com - https://getsongbpm.com")


if __name__ == "__main__":
    main()

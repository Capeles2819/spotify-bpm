"""Find high-tempo tracks on Spotify using the Web API.

Usage:
    python find_high_tempo.py            # default queries
    python find_high_tempo.py "170 bpm"  # custom playlist search

Reads SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET from .env in this folder.
Uses the client-credentials flow, so no browser login is needed.

Optional: set GETSONGBPM_API_KEY in .env (free key from https://getsongbpm.com/api)
and each track is looked up on GetSongBPM for its actual tempo.
Tempo data courtesy of GetSongBPM.com (a link back is required by their terms).
"""
import base64
import os
import sys
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API = "https://api.spotify.com/v1"
BPM_API = "https://api.getsong.co/search/"


def load_env():
    env = Path(__file__).with_name(".env")
    for line in env.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def get_token():
    cid, secret = os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"]
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {auth}"},
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def get(session, path, **params):
    r = session.get(f"{API}{path}", params=params, timeout=15)
    return r


def search_playlists(session, query, limit=5):
    r = get(session, "/search", q=query, type="playlist", limit=limit)
    r.raise_for_status()
    items = r.json()["playlists"]["items"]
    return [p for p in items if p]  # API sometimes returns null entries


def playlist_tracks(session, playlist_id, limit=10):
    r = get(session, f"/playlists/{playlist_id}/tracks", limit=limit,
            fields="items(track(name,artists(name),id,popularity))")
    r.raise_for_status()
    out = []
    for it in r.json()["items"]:
        t = it.get("track")
        if t and t.get("id"):
            out.append(t)
    return out


def clean_title(name):
    """Strip ' - Remaster', '(feat. X)', etc. so GetSongBPM matches better."""
    import re
    name = re.split(r"\s+-\s+", name)[0]
    name = re.sub(r"\s*[\(\[](feat|with|remaster|version|edit|mix)[^\)\]]*[\)\]]", "", name, flags=re.I)
    return name.strip()


_bpm_cache = {}


def lookup_bpm(bpm_session, title, artist):
    """Return (tempo, matched_title) from GetSongBPM, or (None, reason)."""
    key = os.environ.get("GETSONGBPM_API_KEY")
    if not key:
        return None, "no key"
    cache_key = (title.lower(), artist.lower())
    if cache_key in _bpm_cache:
        return _bpm_cache[cache_key]
    try:
        r = bpm_session.get(
            BPM_API,
            params={"api_key": key, "type": "both",
                    "lookup": f"song:{clean_title(title)} artist:{artist}", "limit": 3},
            timeout=15,
        )
        data = r.json()
    except Exception as e:  # network or JSON error
        return None, f"error {e}"
    if isinstance(data, dict) and data.get("error"):
        return None, data["error"]
    hits = data.get("search") if isinstance(data, dict) else None
    if not hits or not isinstance(hits, list):
        result = (None, "no match")
    else:
        h = hits[0]
        result = (h.get("tempo"), h.get("title"))
    _bpm_cache[cache_key] = result
    return result


def probe_audio_features(session, track_id):
    r = get(session, f"/audio-features/{track_id}")
    return r.status_code, r.text[:200]


def main():
    load_env()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {get_token()}"
    bpm_session = requests.Session()
    bpm_session.headers["User-Agent"] = "spotify-bpm-script/1.0"
    have_bpm_key = bool(os.environ.get("GETSONGBPM_API_KEY"))
    if not have_bpm_key:
        print("(GETSONGBPM_API_KEY not set in .env; tempo column will be skipped)")

    queries = sys.argv[1:] or ["180 bpm running", "170 bpm", "high energy workout", "spin class cadence"]
    first_track_id = None

    for q in queries:
        print(f"\n=== Playlists matching '{q}' ===")
        for p in search_playlists(session, q, limit=3):
            owner = p["owner"]["display_name"]
            total = p["tracks"]["total"]
            print(f"\n  [{p['name']}]  by {owner}  ({total} tracks)")
            print(f"  {p['external_urls']['spotify']}")
            for t in playlist_tracks(session, p["id"], limit=8):
                artists = ", ".join(a["name"] for a in t["artists"])
                line = f"     - {t['name']}  -  {artists}  (pop {t['popularity']})"
                if have_bpm_key:
                    tempo, note = lookup_bpm(bpm_session, t["name"], t["artists"][0]["name"])
                    line += f"  [{tempo} BPM]" if tempo else f"  [bpm: {note}]"
                print(line)
                first_track_id = first_track_id or t["id"]

    if first_track_id:
        print("\n=== Probe: can this app read audio features (tempo)? ===")
        code, body = probe_audio_features(session, first_track_id)
        print(f"  GET /audio-features/{first_track_id} -> HTTP {code}")
        print(f"  {body}")

    if have_bpm_key:
        print("\nTempo data from GetSongBPM.com - https://getsongbpm.com")


if __name__ == "__main__":
    main()

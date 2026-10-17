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

# Running / cycling cadence range. GetSongBPM often reports half-time
# (e.g. 92 for a song that feels like 184), so we fold into this window.
TEMPO_MIN, TEMPO_MAX = 120, 200


def normalize_tempo(raw):
    """Return (normalized_bpm, raw_bpm) with raw doubled/halved into 120-200."""
    try:
        t = float(raw)
    except (TypeError, ValueError):
        return None, None
    if t <= 0:
        return None, None
    raw_int = round(t)
    while t < TEMPO_MIN and t * 2 <= TEMPO_MAX:
        t *= 2
    while t > TEMPO_MAX and t / 2 >= TEMPO_MIN:
        t /= 2
    return round(t), raw_int


def _artist_matches(hit, artists):
    name = ((hit.get("artist") or {}).get("name") or "").lower()
    if not name:
        return False
    return any(a.lower() in name or name in a.lower() for a in artists)


def _bpm_query(bpm_session, key, search_type, lookup):
    """One GetSongBPM search; returns (hits, error_message)."""
    try:
        r = bpm_session.get(
            BPM_API,
            params={"api_key": key, "type": search_type, "lookup": lookup, "limit": 5},
            timeout=15,
        )
        data = r.json()
    except Exception as e:  # network or JSON error
        return None, f"error {e}"
    if isinstance(data, dict) and data.get("error"):
        return None, data["error"]
    hits = data.get("search") if isinstance(data, dict) else None
    return (hits if isinstance(hits, list) else []), None


def lookup_bpm(bpm_session, title, artists):
    """Return (tempo, note) from GetSongBPM, or (None, reason).

    Strategy:
      1. song+artist lookup for each credited artist (first two)
      2. title-only lookup, accepting a hit only if its artist matches
    """
    key = os.environ.get("GETSONGBPM_API_KEY")
    if not key:
        return None, "no key"
    cache_key = (title.lower(), tuple(a.lower() for a in artists))
    if cache_key in _bpm_cache:
        return _bpm_cache[cache_key]

    clean = clean_title(title)
    chosen, how = None, None

    for artist in artists[:2]:
        hits, err = _bpm_query(bpm_session, key, "both", f"song:{clean} artist:{artist}")
        if err:
            return None, err
        if hits:
            chosen, how = hits[0], "match"
            break

    if chosen is None:
        hits, err = _bpm_query(bpm_session, key, "song", clean)
        if err:
            return None, err
        for h in hits:
            if _artist_matches(h, artists):
                chosen, how = h, "title-fallback"
                break

    if chosen is None:
        result = (None, "not in GetSongBPM")
    else:
        result = (chosen.get("tempo"), how)
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
                    raw, note = lookup_bpm(bpm_session, t["name"], [a["name"] for a in t["artists"]])
                    bpm, raw_int = normalize_tempo(raw)
                    if bpm is None:
                        line += f"  [bpm: {note}]"
                    else:
                        tag = f"{bpm} BPM"
                        if raw_int != bpm:
                            tag += f" (listed {raw_int})"
                        if note == "title-fallback":
                            tag += " ~"
                        line += f"  [{tag}]"
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

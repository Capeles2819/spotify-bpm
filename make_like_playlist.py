"""Build (or refill) a playlist that sounds like a reference playlist.

Spotify removed Recommendations for new apps, so this does the expansion by hand:
  1. read the reference playlist's tracks (Web API, or the public embed page when the
     API refuses, as it does for Spotify-made mixes)
  2. collect its artists, fetch each artist's top tracks
  3. keep tracks whose artists carry an electronic genre tag (configurable)
  4. write seeds + expansions into a playlist (replace an existing one, or create new)

Usage:
    python make_like_playlist.py <playlist url or id> --into <existing playlist id> [--name NAME]
    python make_like_playlist.py <playlist url or id> --name "New playlist"      # create new
    python make_like_playlist.py <playlist url or id> --dry-run

Options:
    --genres techno,hard,rave      comma list; a track is kept if ANY of its artists has
                                   ANY genre containing one of these words (default below)
    --per-artist 5                 top tracks taken per artist (max 10)
    --max 120                      cap on total tracks
    --no-genre-filter              keep every top track regardless of genre
"""
import argparse
import json
import re
import sys
from collections import OrderedDict

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from find_high_tempo import API, load_env  # noqa: E402
from make_playlist import user_token  # noqa: E402

DEFAULT_GENRE_WORDS = "techno,hard,rave,edm,big room,electro,trance,bass house,tech house"


def playlist_id_from(arg):
    m = re.search(r"playlist[/:]([A-Za-z0-9]{22})", arg)
    return m.group(1) if m else arg.strip()


# ------------------------------------------------------------ reference read

def reference_tracks_api(session, pid):
    r = session.get(f"{API}/playlists/{pid}/tracks", params={
        "limit": 100, "fields": "items(track(id,name,artists(id,name))),next"}, timeout=15)
    if r.status_code != 200:
        return None, None
    items = [it["track"] for it in r.json()["items"] if it.get("track") and it["track"].get("id")]
    name_r = session.get(f"{API}/playlists/{pid}", params={"fields": "name"}, timeout=15)
    return (name_r.json().get("name") if name_r.ok else pid), items


def reference_tracks_embed(pid):
    """Fallback: Spotify's public embed page carries the first ~50 tracks as JSON."""
    r = requests.get(f"https://open.spotify.com/embed/playlist/{pid}",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.S)
    if not m:
        sys.exit("Could not find playlist data on the embed page.")
    ent = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    tracks = []
    for t in ent.get("trackList", []):
        tid = (t.get("uri") or "").split(":")[-1]
        if len(tid) == 22:
            tracks.append({"id": tid, "name": t.get("title"), "artists": []})
    return ent.get("name", pid), tracks


def hydrate_tracks(session, tracks):
    """Fill in artist ids for tracks that came from the embed page."""
    need = [t["id"] for t in tracks if not t["artists"]]
    by_id = {}
    for i in range(0, len(need), 50):
        r = session.get(f"{API}/tracks", params={"ids": ",".join(need[i:i + 50])}, timeout=15)
        r.raise_for_status()
        for t in r.json()["tracks"]:
            if t:
                by_id[t["id"]] = t
    for t in tracks:
        if not t["artists"] and t["id"] in by_id:
            full = by_id[t["id"]]
            t["name"] = full["name"]
            t["artists"] = [{"id": a["id"], "name": a["name"]} for a in full["artists"]]
    return [t for t in tracks if t["artists"]]


# ---------------------------------------------------------------- expansion

def artist_genres(session, artist_ids):
    out = {}
    ids = list(artist_ids)
    for i in range(0, len(ids), 50):
        r = session.get(f"{API}/artists", params={"ids": ",".join(ids[i:i + 50])}, timeout=15)
        r.raise_for_status()
        for a in r.json()["artists"]:
            if a:
                out[a["id"]] = [g.lower() for g in a.get("genres", [])]
    return out


def top_tracks(session, artist_id, n):
    r = session.get(f"{API}/artists/{artist_id}/top-tracks", params={"market": "US"}, timeout=15)
    if r.status_code != 200:
        return []
    return [{"id": t["id"], "name": t["name"],
             "artists": [{"id": a["id"], "name": a["name"]} for a in t["artists"]]}
            for t in r.json().get("tracks", [])[:n]]


def genre_ok(track, genres_by_artist, words):
    for a in track["artists"]:
        for g in genres_by_artist.get(a["id"], []):
            if any(w in g for w in words):
                return True
    return False


def dedupe_key(t):
    name = re.split(r"\s+-\s+|\(", t["name"].lower())[0].strip()
    return name, t["artists"][0]["name"].lower()


# ---------------------------------------------------------------- write out

def write_playlist(session, into, name, description, uris):
    if into:
        pid = into
        session.put(f"{API}/playlists/{pid}", json={"name": name, "description": description},
                    timeout=15).raise_for_status()
        # PUT replaces (max 100), POST appends the rest
        session.put(f"{API}/playlists/{pid}/tracks", json={"uris": uris[:100]}, timeout=15).raise_for_status()
        for i in range(100, len(uris), 100):
            session.post(f"{API}/playlists/{pid}/tracks", json={"uris": uris[i:i + 100]}, timeout=15).raise_for_status()
    else:
        me = session.get(f"{API}/me", timeout=15); me.raise_for_status()
        r = session.post(f"{API}/users/{me.json()['id']}/playlists",
                         json={"name": name, "description": description, "public": False}, timeout=15)
        r.raise_for_status()
        pid = r.json()["id"]
        for i in range(0, len(uris), 100):
            session.post(f"{API}/playlists/{pid}/tracks", json={"uris": uris[i:i + 100]}, timeout=15).raise_for_status()
    r = session.get(f"{API}/playlists/{pid}", params={"fields": "name,external_urls,tracks.total"}, timeout=15)
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reference", help="reference playlist URL or id")
    ap.add_argument("--into", help="existing playlist id to overwrite")
    ap.add_argument("--name", help="playlist name")
    ap.add_argument("--genres", default=DEFAULT_GENRE_WORDS)
    ap.add_argument("--per-artist", type=int, default=5)
    ap.add_argument("--max", type=int, default=120)
    ap.add_argument("--no-genre-filter", action="store_true")
    ap.add_argument("--filter-seeds", action="store_true",
                    help="also drop reference tracks that fail the genre filter (mixed-genre references)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    load_env()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {user_token()}"

    ref_id = playlist_id_from(args.reference)
    ref_name, seeds = reference_tracks_api(session, ref_id)
    if seeds is None:
        print("API refused the reference playlist; reading its public embed page instead.")
        ref_name, seeds = reference_tracks_embed(ref_id)
    seeds = hydrate_tracks(session, seeds)
    print(f"Reference: '{ref_name}' with {len(seeds)} usable tracks")

    words = [w.strip().lower() for w in args.genres.split(",") if w.strip()]
    genres = {}
    if not args.no_genre_filter:
        genres = artist_genres(session, {a["id"] for t in seeds for a in t["artists"]})

    if args.filter_seeds and not args.no_genre_filter:
        kept = [t for t in seeds if genre_ok(t, genres, words)]
        dropped = [t for t in seeds if t not in kept]
        print(f"Seed genre filter: kept {len(kept)}, dropped {len(dropped)}")
        for t in dropped:
            print(f"  - {t['name']}  -  {', '.join(a['name'] for a in t['artists'])}")
        seeds = kept

    artists = OrderedDict()
    for t in seeds:
        for a in t["artists"]:
            artists.setdefault(a["id"], a["name"])
    print(f"{len(artists)} artists to expand through")

    chosen, seen_ids, seen_keys = [], set(), set()

    def add(t, why):
        if t["id"] in seen_ids or dedupe_key(t) in seen_keys:
            return False
        seen_ids.add(t["id"]); seen_keys.add(dedupe_key(t))
        chosen.append(t)
        print(f"  + {t['name']}  -  {', '.join(a['name'] for a in t['artists'])}   [{why}]")
        return True

    print("\nSeeds:")
    for t in seeds:
        add(t, "seed")

    print("\nExpansion (artist top tracks, round-robin so every artist gets a turn):")
    skipped = 0
    pools = {aid: top_tracks(session, aid, min(args.per_artist, 10)) for aid in artists}
    if not args.no_genre_filter:
        unknown = {a["id"] for pool in pools.values() for t in pool for a in t["artists"]} - set(genres)
        if unknown:
            genres.update(artist_genres(session, unknown))
    for rnd in range(args.per_artist):
        if len(chosen) >= args.max:
            break
        for aid, aname in artists.items():
            if len(chosen) >= args.max:
                break
            # take the next not-yet-used, genre-approved track from this artist's pool
            while pools[aid]:
                t = pools[aid].pop(0)
                if not args.no_genre_filter and not genre_ok(t, genres, words):
                    skipped += 1
                    continue
                if add(t, f"top of {aname}, pass {rnd + 1}"):
                    break

    print(f"\n{len(chosen)} tracks selected ({len(seeds)} seeds), {skipped} skipped by genre filter")
    if args.dry_run:
        print("Dry run; nothing written.")
        return

    name = args.name or f"Like {ref_name} (auto)"
    desc = f"Built from '{ref_name}' plus top tracks of its artists. Made with spotify-bpm."
    pl = write_playlist(session, args.into, name, desc, [f"spotify:track:{t['id']}" for t in chosen])
    print(f"\nPlaylist '{pl['name']}' now has {pl['tracks']['total']} tracks:")
    print(pl["external_urls"]["spotify"])


if __name__ == "__main__":
    main()

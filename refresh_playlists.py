"""Keep the auto playlists topped up, remembering what you removed.

For every playlist in playlists.json:
  1. read what is in it now
  2. anything that was there last run but is gone now -> you deleted it -> mark REJECTED
     (never added again)
  3. if the playlist is below its target size, generate fresh candidates from the same
     source (reference playlist + artist top tracks, or BPM search), skipping anything
     already present or rejected, and append until it reaches the target

State lives in state.json next to this file (gitignored). Meant to run unattended from
cron on the Raspberry Pi; it never opens a browser - a valid .spotify_token.json must
already exist (log in once with make_playlist.py on a machine with a browser, then copy
the token file over).

Usage:
    python refresh_playlists.py              # refresh everything
    python refresh_playlists.py --dry-run    # report only, change nothing
    python refresh_playlists.py --only 2xM9PwnIHFkCXbdkLWI906
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from find_high_tempo import API, load_env, lookup_bpm, normalize_tempo  # noqa: E402
from make_like_playlist import build_candidates, dedupe_key  # noqa: E402
from make_playlist import TOKEN_FILE, gather_tracks, user_token  # noqa: E402

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "playlists.json"
STATE_FILE = HERE / "state.json"


def log(msg=""):
    print(msg, flush=True)


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def playlist_tracks_all(session, pid):
    """Every track in the playlist, in order: list of {id, name, artists:[{name}]}."""
    out, url = [], f"{API}/playlists/{pid}/tracks"
    params = {"limit": 100, "fields": "items(track(id,name,artists(name))),next"}
    while url:
        r = session.get(url, params=params, timeout=20)
        r.raise_for_status()
        body = r.json()
        for it in body["items"]:
            t = it.get("track")
            if t and t.get("id"):
                out.append({"id": t["id"], "name": t["name"], "artists": t["artists"]})
        url, params = body.get("next"), None
    return out


def label(t):
    return f"{t['name']}  -  {', '.join(a['name'] for a in t['artists'])}"


def append_tracks(session, pid, track_ids):
    uris = [f"spotify:track:{i}" for i in track_ids]
    for i in range(0, len(uris), 100):
        r = session.post(f"{API}/playlists/{pid}/tracks", json={"uris": uris[i:i + 100]}, timeout=20)
        r.raise_for_status()


def candidates_like(session, cfg, exclude_ids, exclude_keys, need):
    words = [w.strip().lower() for w in cfg.get("genres", "").split(",") if w.strip()]
    _, tracks, _ = build_candidates(
        session, cfg["reference"], words,
        filter_seeds=cfg.get("filter_seeds", False),
        per_artist=cfg.get("per_artist", 3),
        max_n=need,
        exclude_ids=exclude_ids, exclude_keys=exclude_keys,
        log=lambda *_: None,  # quiet; we print our own summary
    )
    return tracks


def candidates_bpm(session, cfg, exclude_ids, exclude_keys, need):
    bpm_session = requests.Session()
    bpm_session.headers["User-Agent"] = "spotify-bpm-refresh/1.0"
    found = gather_tracks(session, bpm_session, cfg["queries"], cfg["min"], cfg["max"])
    out = []
    for t in found:
        if t["id"] in exclude_ids:
            continue
        # gather_tracks returns artists as a joined string; rebuild the dict shape for dedupe
        shaped = {"id": t["id"], "name": t["name"], "artists": [{"name": t["artists"].split(", ")[0]}]}
        if dedupe_key(shaped) in exclude_keys:
            continue
        out.append(shaped)
        if len(out) >= need:
            break
    return out


def refresh_one(session, cfg, state, dry_run):
    pid = cfg["id"]
    st = state.setdefault(pid, {"known": {}, "rejected": {}, "runs": []})
    current = playlist_tracks_all(session, pid)
    current_ids = {t["id"] for t in current}
    log(f"\n=== {cfg['name']}  ({len(current)} tracks, target {cfg['target']})")

    # 1. detect deletions since last run
    first_run = not st["known"]
    removed = {tid: name for tid, name in st["known"].items() if tid not in current_ids}
    if first_run:
        log("  first run: recording current contents, nothing marked rejected")
    elif removed:
        for tid, name in removed.items():
            log(f"  rejected (you removed it): {name}")
        st["rejected"].update(removed)
    else:
        log("  no deletions since last run")

    # 2. top up
    need = cfg["target"] - len(current)
    added = []
    if need > 0:
        exclude_ids = current_ids | set(st["rejected"])
        exclude_keys = {dedupe_key(t) for t in current}
        log(f"  need {need} more; generating candidates...")
        gen = candidates_bpm if cfg["mode"] == "bpm" else candidates_like
        picks = gen(session, cfg, exclude_ids, exclude_keys, need)
        picks = picks[:need]
        for t in picks:
            log(f"  + {label(t)}")
        if picks and not dry_run:
            append_tracks(session, pid, [t["id"] for t in picks])
        added = picks
        if len(picks) < need:
            log(f"  (only {len(picks)} fresh candidates available right now)")
    else:
        log("  at target, nothing to add")

    # 3. remember what is there now (plus what we just added)
    if not dry_run:
        known = {t["id"]: label(t) for t in current}
        known.update({t["id"]: label(t) for t in added})
        st["known"] = known
        st["runs"].append({
            "at": dt.datetime.now().isoformat(timespec="seconds"),
            "size_before": len(current), "removed": len(removed) if not first_run else 0,
            "added": len(added),
        })
        st["runs"] = st["runs"][-30:]
    return len(removed) if not first_run else 0, len(added)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report only; change nothing, save no state")
    ap.add_argument("--only", help="refresh just this playlist id")
    args = ap.parse_args()

    load_env()
    if not TOKEN_FILE.exists():
        sys.exit("No .spotify_token.json here. Log in once with make_playlist.py on a machine "
                 "with a browser and copy the token file next to this script.")

    cfg = load_json(CONFIG_FILE, {"playlists": []})
    state = load_json(STATE_FILE, {})
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {user_token()}"

    log(f"refresh_playlists {dt.datetime.now():%Y-%m-%d %H:%M}  {'(dry run)' if args.dry_run else ''}")
    totals = [0, 0]
    for pl in cfg["playlists"]:
        if args.only and pl["id"] != args.only:
            continue
        try:
            r, a = refresh_one(session, pl, state, args.dry_run)
            totals[0] += r; totals[1] += a
        except requests.HTTPError as e:
            log(f"  ERROR on {pl['name']}: {e} {getattr(e.response, 'text', '')[:200]}")
        if not args.dry_run:
            save_json(STATE_FILE, state)
    log(f"\ndone: {totals[0]} rejected, {totals[1]} added")


if __name__ == "__main__":
    main()

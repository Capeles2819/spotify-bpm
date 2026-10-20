# spotify-bpm

A small Python script that searches Spotify for high-tempo playlists (running,
spin class, workout) and lists their tracks with the actual BPM of each song.

Spotify no longer exposes tempo through its Web API for new apps, so tempo
comes from [GetSongBPM](https://getsongbpm.com).

**Tempo data provided by [GetSongBPM.com](https://getsongbpm.com)**

## Setup

1. Create a Spotify app at <https://developer.spotify.com/dashboard>.
2. Get a free API key at <https://getsongbpm.com/api>.
3. Copy `.env.example` to `.env` and fill in the three values.
4. Run:

```bash
pip install requests
python find_high_tempo.py "180 bpm running"
```

## The scripts

| Script | What it does |
|---|---|
| `find_high_tempo.py` | Lists tracks from high-tempo playlists with their BPM (GetSongBPM). |
| `make_playlist.py` | Creates a playlist on your profile from a BPM range. First run opens a browser login (needs redirect URI `http://127.0.0.1:8888/callback` in the Spotify app settings). |
| `make_like_playlist.py` | Creates or refills a playlist that sounds like a reference playlist: its tracks plus each artist's top tracks, filtered by artist genre. |
| `refresh_playlists.py` | Keeps the playlists in `playlists.json` topped up. Tracks you delete are remembered in `state.json` and never re-added. Runs from cron on the Pi. |

## Customizing

All refresher settings are in `playlists.json`, one entry per playlist:

```json
{
  "id": "2xM9PwnIHFkCXbdkLWI906",
  "name": "Smooth Jazz (auto)",
  "mode": "like",
  "reference": "29uiOXIxJNOejWUGPYCHHc",
  "genres": "smooth jazz,jazz",
  "filter_seeds": true,
  "per_artist": 3,
  "target": 150
}
```

- `reference`: any Spotify playlist ID or URL. Swap it to change the flavor.
- `genres`: comma list matched against Spotify's artist genre tags. Fewer words is
  stricter; empty disables the filter.
- `filter_seeds`: `true` also drops reference tracks that fail the genre check.
- `per_artist`: top tracks each artist may contribute.
- `target`: playlist size to maintain. Raise it to grow the playlist.
- A `"mode": "bpm"` entry uses `min`, `max` and `queries` (playlist search terms) instead.

Edit on the Pi and test without changing anything:

```bash
nano ~/spotify-bpm/playlists.json
cd ~/spotify-bpm && python3 refresh_playlists.py --dry-run
```

Or edit on the PC, commit, push, then `git pull` on the Pi.

### Adding a new playlist

Create it once (PC, browser login on first run), preview with `--dry-run`:

```bash
python make_like_playlist.py <reference url> --name "Lo-fi Study (auto)" --genres "lo-fi,chillhop,jazz" --filter-seeds --per-artist 3 --max 150
```

Copy the playlist ID from the printed URL into a new `playlists.json` entry.

Genre words that match well: `drum and bass`, `melodic techno`, `tech house`,
`afrobeats`, `reggaeton`, `lo-fi`, `chillhop`, `synthwave`, `indie rock`,
`classic rock`, `r&b`, `neo soul`, `bossa nova`, `latin`.

## Credits

- Track and playlist data: [Spotify Web API](https://developer.spotify.com/documentation/web-api)
- Tempo (BPM) data: [GetSongBPM.com](https://getsongbpm.com)

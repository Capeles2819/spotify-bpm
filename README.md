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

## Credits

- Track and playlist data: [Spotify Web API](https://developer.spotify.com/documentation/web-api)
- Tempo (BPM) data: [GetSongBPM.com](https://getsongbpm.com)

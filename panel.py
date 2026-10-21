"""Web panel for managing playlists.json without editing JSON by hand.

Runs on the Raspberry Pi as a systemd service (deploy/spotify-panel.service) and
listens on http://0.0.0.0:5070 for the home LAN (5060 is on Chrome's blocked-port list). There is no login: anyone on the
LAN can use it, so do not expose the port to the internet.

It only edits playlists.json / state.json and runs the existing scripts:
  * Save / Remove / Add   -> rewrite playlists.json (atomically, after validation)
  * Preview / Run now     -> python3 refresh_playlists.py [--dry-run] --only <id>
  * Add playlist          -> python3 make_like_playlist.py <ref> --name ... (then append entry)
  * Un-reject             -> drop a track id from state.json "rejected"
  * Log tab               -> tail of refresh.log + the runs history from state.json

One script runs at a time, in a background thread; the page polls for its output.

Usage:
    python3 panel.py                        # 0.0.0.0:5070
    PANEL_DIR=/tmp/test python3 panel.py    # use another folder's playlists.json/state.json/refresh.log
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from find_high_tempo import API, load_env  # noqa: E402
from make_like_playlist import playlist_id_from, reference_tracks_embed  # noqa: E402
from make_playlist import TOKEN_FILE, _refresh  # noqa: E402

import requests  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PANEL_DIR", HERE)).resolve()
CONFIG_FILE = DATA_DIR / "playlists.json"
STATE_FILE = DATA_DIR / "state.json"
LOG_FILE = DATA_DIR / "refresh.log"
HOST = os.environ.get("PANEL_HOST", "0.0.0.0")
PORT = int(os.environ.get("PANEL_PORT", "5070"))

ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
URL_RE = re.compile(r"https://open\.spotify\.com/playlist/([A-Za-z0-9]{22})")

app = Flask(__name__)
FILE_LOCK = threading.Lock()


# ---------------------------------------------------------------- files

def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json_atomic(path, data):
    """Serialise first (so a bad object never truncates the file), then write + rename."""
    text = json.dumps(data, indent=2, ensure_ascii=False)
    json.loads(text)  # must round-trip before anything touches the real file
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_config():
    cfg = load_json(CONFIG_FILE, {"playlists": []})
    cfg.setdefault("playlists", [])
    return cfg


def find_entry(cfg, pid):
    for pl in cfg["playlists"]:
        if pl.get("id") == pid:
            return pl
    return None


# ---------------------------------------------------------------- validation

class Invalid(ValueError):
    pass


def as_int(val, name, lo=None, hi=None):
    try:
        n = int(str(val).strip())
    except (TypeError, ValueError):
        raise Invalid(f"{name} must be a whole number")
    if lo is not None and n < lo:
        raise Invalid(f"{name} must be at least {lo}")
    if hi is not None and n > hi:
        raise Invalid(f"{name} must be at most {hi}")
    return n


def clean_genres(val):
    words = [w.strip().lower() for w in str(val or "").split(",") if w.strip()]
    return ",".join(words)


def clean_reference(val):
    ref = playlist_id_from(str(val or "").strip())
    ref = ref.split("?")[0].strip()
    if not ID_RE.match(ref):
        raise Invalid("reference must be a Spotify playlist URL or 22-character id")
    return ref


def clean_queries(val):
    if isinstance(val, list):
        items = val
    else:
        items = re.split(r"[\n,]", str(val or ""))
    out = [q.strip() for q in items if q.strip()]
    if not out:
        raise Invalid("at least one search query is needed")
    return out


def apply_fields(entry, data):
    """Validate the editable fields in `data` and copy them onto `entry` (in place)."""
    name = str(data.get("name", "")).strip()
    if not name:
        raise Invalid("name is required")
    entry["name"] = name
    entry["target"] = as_int(data.get("target"), "target", 1, 10000)
    if entry.get("mode", "like") == "bpm":
        entry["min"] = as_int(data.get("min"), "min BPM", 1, 400)
        entry["max"] = as_int(data.get("max"), "max BPM", 1, 400)
        if entry["min"] >= entry["max"]:
            raise Invalid("min BPM must be below max BPM")
        entry["queries"] = clean_queries(data.get("queries"))
    else:
        entry["mode"] = "like"
        entry["reference"] = clean_reference(data.get("reference"))
        entry["genres"] = clean_genres(data.get("genres"))
        entry["filter_seeds"] = bool(data.get("filter_seeds"))
        entry["per_artist"] = as_int(data.get("per_artist"), "per_artist", 1, 10)
    return entry


# ---------------------------------------------------------------- jobs

class Job:
    def __init__(self, title, args, on_done=None):
        self.title = title
        self.args = args
        self.on_done = on_done
        self.lines = []
        self.rc = None
        self.started = dt.datetime.now().isoformat(timespec="seconds")
        self.finished = None
        self.result = None

    def status(self):
        return {
            "title": self.title, "command": " ".join(self.args), "running": self.rc is None,
            "rc": self.rc, "started": self.started, "finished": self.finished,
            "lines": self.lines, "result": self.result,
        }


JOB_LOCK = threading.Lock()
CURRENT_JOB = None


class Busy(RuntimeError):
    pass


def job_running():
    return CURRENT_JOB is not None and CURRENT_JOB.rc is None


def start_job(title, args, on_done=None):
    global CURRENT_JOB
    with JOB_LOCK:
        if job_running():
            raise Busy(f"'{CURRENT_JOB.title}' is still running")
        job = Job(title, args, on_done)
        CURRENT_JOB = job
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return job


def _run_job(job):
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    rc = -1
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", *job.args], cwd=str(HERE), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        for line in iter(proc.stdout.readline, ""):
            job.lines.append(line.rstrip("\r\n"))
        rc = proc.wait()
    except Exception as e:  # noqa: BLE001
        job.lines.append(f"panel: could not run the script: {e}")
    if rc == 0 and job.on_done:
        try:
            job.result = job.on_done(job)
        except Exception as e:  # noqa: BLE001
            job.lines.append(f"panel: script finished but the follow-up failed: {e}")
            rc = -2
    job.finished = dt.datetime.now().isoformat(timespec="seconds")
    job.rc = rc  # set last: this is what flips "running" to false


# ---------------------------------------------------------------- spotify lookups

class NoToken(RuntimeError):
    pass


def access_token():
    """Like make_playlist.user_token() but never falls back to the browser login
    (the Pi has no browser; that path would hang the panel)."""
    if not TOKEN_FILE.exists():
        raise NoToken("no .spotify_token.json on this machine")
    with FILE_LOCK:
        tok = json.loads(TOKEN_FILE.read_text())
        if tok.get("expires_at", 0) > time.time():
            return tok["access_token"]
        tok = _refresh(os.environ["SPOTIFY_CLIENT_ID"], tok)
    if not tok:
        raise NoToken("Spotify token expired and could not be refreshed; log in again with make_playlist.py on the PC")
    return tok["access_token"]


def spotify():
    if job_running():
        raise Busy("a script is running; Spotify lookups wait until it finishes")
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {access_token()}"
    return s


def pl_summary(p):
    return {"id": p["id"], "name": p["name"], "owner": (p.get("owner") or {}).get("display_name", ""),
            "tracks": (p.get("tracks") or {}).get("total"), "url": f"https://open.spotify.com/playlist/{p['id']}"}


NAME_CACHE = {}


def playlist_info(pid):
    """Name/owner/size for a playlist id. Spotify-made mixes 404 on the API, so fall back
    to the public embed page (name + first ~50 tracks)."""
    if pid in NAME_CACHE:
        return NAME_CACHE[pid]
    r = spotify().get(f"{API}/playlists/{pid}", params={"fields": "id,name,owner(display_name),tracks.total"}, timeout=15)
    if r.status_code == 200:
        info = pl_summary(r.json())
    else:
        try:
            name, _tracks = reference_tracks_embed(pid)
            info = {"id": pid, "name": name, "owner": "Spotify", "tracks": None,
                    "url": f"https://open.spotify.com/playlist/{pid}"}
        except Exception:  # noqa: BLE001
            raise Invalid(f"playlist {pid} not found on Spotify (deleted or made private?)")
    NAME_CACHE[pid] = info
    return info


# ---------------------------------------------------------------- API

def ok(**kw):
    return jsonify({"ok": True, **kw})


def fail(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code


@app.errorhandler(Invalid)
def _invalid(e):
    return fail(e, 400)


@app.errorhandler(Busy)
def _busy(e):
    return fail(e, 409)


@app.errorhandler(NoToken)
def _notoken(e):
    return fail(e, 503)


@app.errorhandler(requests.RequestException)
def _spotify_err(e):
    return fail(f"Spotify request failed: {e}", 502)


@app.get("/api/spotify/playlist/<pid>")
def api_spotify_playlist(pid):
    if not ID_RE.match(pid):
        raise Invalid("bad playlist id")
    return ok(playlist=playlist_info(pid))


@app.get("/api/spotify/search")
def api_spotify_search():
    q = request.args.get("q", "").strip()
    s = spotify()
    if q:
        r = s.get(f"{API}/search", params={"q": q, "type": "playlist", "limit": 15}, timeout=15)
        r.raise_for_status()
        items = r.json()["playlists"]["items"]
        source = "search"
    else:
        r = s.get(f"{API}/me/playlists", params={"limit": 50}, timeout=15)
        r.raise_for_status()
        items = r.json()["items"]
        source = "mine"
    out = [pl_summary(p) for p in items if p and p.get("id")]
    for p in out:
        NAME_CACHE.setdefault(p["id"], p)
    return ok(playlists=out, source=source)


@app.get("/api/config")
def api_config():
    with FILE_LOCK:
        cfg = load_config()
        state = load_json(STATE_FILE, {})
    summary = {}
    for pid, st in state.items():
        runs = st.get("runs", [])
        summary[pid] = {
            "known": len(st.get("known", {})), "rejected": len(st.get("rejected", {})),
            "last_run": runs[-1] if runs else None,
        }
    return ok(playlists=cfg["playlists"], state=summary)


@app.post("/api/playlists/<pid>")
def api_save(pid):
    data = request.get_json(force=True, silent=True) or {}
    with FILE_LOCK:
        cfg = load_config()
        entry = find_entry(cfg, pid)
        if entry is None:
            return fail("playlist not in playlists.json", 404)
        apply_fields(entry, data)
        save_json_atomic(CONFIG_FILE, cfg)
    return ok(playlist=entry)


@app.delete("/api/playlists/<pid>")
def api_remove(pid):
    with FILE_LOCK:
        cfg = load_config()
        before = len(cfg["playlists"])
        cfg["playlists"] = [p for p in cfg["playlists"] if p.get("id") != pid]
        if len(cfg["playlists"]) == before:
            return fail("playlist not in playlists.json", 404)
        save_json_atomic(CONFIG_FILE, cfg)
    return ok()


@app.post("/api/run")
def api_run():
    data = request.get_json(force=True, silent=True) or {}
    pid = str(data.get("id", "")).strip()
    if not ID_RE.match(pid):
        raise Invalid("bad playlist id")
    with FILE_LOCK:
        entry = find_entry(load_config(), pid)
    if entry is None:
        return fail("playlist not in playlists.json", 404)
    dry = bool(data.get("dry_run", True))
    args = ["refresh_playlists.py", "--only", pid] + (["--dry-run"] if dry else [])
    title = f"{'Preview' if dry else 'Run now'}: {entry.get('name', pid)}"
    return ok(job=start_job(title, args).status())


@app.post("/api/add")
def api_add():
    data = request.get_json(force=True, silent=True) or {}
    dry = bool(data.get("dry_run", True))
    new = apply_fields({"mode": "like"}, data)
    args = ["make_like_playlist.py", new["reference"], "--name", new["name"],
            "--per-artist", str(new["per_artist"]), "--max", str(new["target"])]
    if new["genres"]:
        args += ["--genres", new["genres"]]
    else:
        args += ["--no-genre-filter"]
    if new["filter_seeds"]:
        args.append("--filter-seeds")
    if dry:
        args.append("--dry-run")
        return ok(job=start_job(f"Preview new: {new['name']}", args).status())

    def on_done(job):
        urls = [m for line in job.lines for m in URL_RE.findall(line)]
        if not urls:
            raise RuntimeError("no playlist URL found in the script output; nothing added to playlists.json")
        pid = urls[-1]
        entry = {"id": pid, "name": new["name"], "mode": "like", "reference": new["reference"],
                 "genres": new["genres"], "filter_seeds": new["filter_seeds"],
                 "per_artist": new["per_artist"], "target": new["target"]}
        with FILE_LOCK:
            cfg = load_config()
            if find_entry(cfg, pid) is None:
                cfg["playlists"].append(entry)
                save_json_atomic(CONFIG_FILE, cfg)
        job.lines.append(f"panel: added '{new['name']}' ({pid}) to playlists.json")
        return {"added": entry}

    return ok(job=start_job(f"Create: {new['name']}", args, on_done).status())


@app.get("/api/job")
def api_job():
    return ok(job=CURRENT_JOB.status() if CURRENT_JOB else None)


@app.get("/api/state")
def api_state():
    with FILE_LOCK:
        cfg = load_config()
        state = load_json(STATE_FILE, {})
    names = {p.get("id"): p.get("name", p.get("id")) for p in cfg["playlists"]}
    rejected = [{"id": pid, "name": names.get(pid, pid), "in_config": pid in names,
                 "tracks": [{"id": tid, "label": lbl} for tid, lbl in st.get("rejected", {}).items()]}
                for pid, st in state.items()]
    runs = []
    for pid, st in state.items():
        for r in st.get("runs", []):
            runs.append({"playlist": names.get(pid, pid), **r})
    runs.sort(key=lambda r: r.get("at", ""), reverse=True)
    return ok(rejected=rejected, runs=runs[:200])


@app.post("/api/unreject")
def api_unreject():
    data = request.get_json(force=True, silent=True) or {}
    pid, tid = str(data.get("id", "")), str(data.get("track_id", ""))
    if not ID_RE.match(pid) or not ID_RE.match(tid):
        raise Invalid("bad id")
    if job_running():
        raise Busy("a script is running and may rewrite state.json; try again when it finishes")
    with FILE_LOCK:
        state = load_json(STATE_FILE, {})
        rej = state.get(pid, {}).get("rejected", {})
        if tid not in rej:
            return fail("track is not in the rejected list", 404)
        label = rej.pop(tid)
        save_json_atomic(STATE_FILE, state)
    return ok(removed=label)


@app.get("/api/log")
def api_log():
    n = max(1, min(int(request.args.get("n", 200)), 2000))
    lines = []
    if LOG_FILE.exists():
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    return ok(lines=lines, path=str(LOG_FILE))


@app.get("/")
def index():
    return render_template_string(PAGE, data_dir=str(DATA_DIR))


# ---------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spotify playlists panel</title>
<style>
  :root { --bg:#121212; --card:#1e1e1e; --line:#333; --fg:#eee; --muted:#9a9a9a; --green:#1db954; --red:#e0564b; --amber:#e6b450; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.4 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  header { padding:14px 20px; border-bottom:1px solid var(--line); display:flex; gap:16px; align-items:center; flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; }
  header small { color:var(--muted); }
  nav button { background:none; border:1px solid var(--line); color:var(--fg); padding:6px 14px; border-radius:16px; cursor:pointer; font:inherit; }
  nav button.active { background:var(--green); border-color:var(--green); color:#000; font-weight:600; }
  main { padding:16px 20px; max-width:1100px; }
  section.tab { display:none; } section.tab.active { display:block; }
  .cards { display:grid; grid-template-columns:repeat(auto-fill, minmax(420px, 1fr)); gap:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; }
  .card h3 { margin:0 0 4px; font-size:16px; display:flex; justify-content:space-between; gap:8px; }
  .card h3 a { color:var(--green); text-decoration:none; font-size:13px; white-space:nowrap; }
  .meta { color:var(--muted); font-size:12px; margin-bottom:10px; }
  .grid { display:grid; grid-template-columns:130px 1fr; gap:6px 10px; align-items:center; }
  label { color:var(--muted); font-size:13px; }
  input[type=text], input[type=number], textarea { width:100%; background:#111; color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:6px 8px; font:inherit; }
  textarea { min-height:60px; resize:vertical; }
  .btns { display:flex; gap:8px; margin-top:12px; flex-wrap:wrap; }
  button.act { background:#2a2a2a; color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:6px 12px; cursor:pointer; font:inherit; }
  button.act:hover { border-color:#666; }
  button.act.primary { background:var(--green); color:#000; border-color:var(--green); font-weight:600; }
  button.act.danger { color:var(--red); }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .msg { font-size:13px; margin-top:8px; min-height:1.2em; }
  .msg.err { color:var(--red); } .msg.okk { color:var(--green); }
  #jobbox { margin-top:20px; background:#0c0c0c; border:1px solid var(--line); border-radius:10px; padding:12px; }
  #jobbox h3 { margin:0 0 6px; font-size:14px; display:flex; justify-content:space-between; }
  pre { margin:0; white-space:pre-wrap; word-break:break-word; font:13px/1.35 ui-monospace, Consolas, monospace; max-height:420px; overflow:auto; }
  .pill { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; }
  .pill.run { background:var(--amber); color:#000; } .pill.ok { background:var(--green); color:#000; } .pill.bad { background:var(--red); color:#000; }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th, td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:500; }
  .rej { margin-bottom:18px; }
  .rej h3 { font-size:15px; margin:0 0 6px; }
  .rej ul { list-style:none; padding:0; margin:0; }
  .rej li { display:flex; justify-content:space-between; gap:10px; padding:4px 0; border-bottom:1px solid var(--line); }
  .note { color:var(--muted); font-size:13px; }
  .refrow { display:flex; gap:6px; }
  .refrow input { flex:1; }
  .refname { grid-column: 2; color:var(--muted); font-size:12px; margin-top:-2px; }
  .picker { grid-column: 1 / -1; background:#111; border:1px solid var(--line); border-radius:8px; padding:8px; }
  .picker input { margin-bottom:6px; }
  .picker ul { list-style:none; margin:0; padding:0; max-height:220px; overflow:auto; }
  .picker li { padding:5px 6px; border-radius:5px; cursor:pointer; display:flex; justify-content:space-between; gap:8px; }
  .picker li:hover { background:#2a2a2a; }
  .picker li small { color:var(--muted); white-space:nowrap; }
  @media (max-width:600px) { .cards { grid-template-columns:1fr; } .grid { grid-template-columns:1fr; } }
</style></head>
<body>
<header>
  <h1>Spotify playlists panel</h1>
  <nav>
    <button data-tab="playlists" class="active">Playlists</button>
    <button data-tab="add">Add playlist</button>
    <button data-tab="rejected">Rejected songs</button>
    <button data-tab="log">Log</button>
  </nav>
  <small>files in {{ data_dir }}</small>
</header>
<main>
  <section class="tab active" id="tab-playlists">
    <div class="cards" id="cards"></div>
  </section>

  <section class="tab" id="tab-add">
    <div class="card" style="max-width:560px">
      <h3>New "sounds like" playlist</h3>
      <p class="note">Creates a private playlist on your Spotify account from a reference playlist (its tracks plus each artist's top tracks, filtered by genre) and adds it to the refresher. Click <b>Find</b> to pick the reference from your own playlists or search Spotify by name. Use Preview first; it changes nothing.</p>
      <div class="grid">
        <label>Reference playlist</label><div class="refrow"><input type="text" id="add-reference" placeholder="pick one with Find, or paste a Spotify link"><button class="act" type="button" id="add-find">Find</button></div>
        <div class="refname" id="add-refname"></div>
        <label>Name</label><input type="text" id="add-name" placeholder="Lo-fi Study (auto)">
        <label>Genres</label><input type="text" id="add-genres" placeholder="lo-fi,chillhop,jazz  (empty = no genre filter)">
        <label>Filter seeds</label><input type="checkbox" id="add-filter_seeds" checked>
        <label>Per artist</label><input type="number" id="add-per_artist" value="3" min="1" max="10">
        <label>Target size</label><input type="number" id="add-target" value="150" min="1">
      </div>
      <div class="btns">
        <button class="act" id="add-preview">Preview</button>
        <button class="act primary" id="add-create">Create and add to refresher</button>
      </div>
      <div class="msg" id="add-msg"></div>
    </div>
  </section>

  <section class="tab" id="tab-rejected">
    <p class="note">Tracks you deleted from a playlist are remembered here and never re-added. Un-reject one and the refresher may add it back on a later run.</p>
    <div id="rejected"></div>
  </section>

  <section class="tab" id="tab-log">
    <h3 style="margin:0 0 6px">refresh.log (last 200 lines) <button class="act" id="log-reload" style="margin-left:10px">Reload</button></h3>
    <pre id="logtext" style="max-height:360px"></pre>
    <h3 style="margin:18px 0 6px">Runs history (state.json)</h3>
    <table id="runs"><thead><tr><th>When</th><th>Playlist</th><th>Size before</th><th>Removed</th><th>Added</th></tr></thead><tbody></tbody></table>
  </section>

  <div id="jobbox" hidden>
    <h3><span id="job-title"></span><span id="job-pill" class="pill"></span></h3>
    <div class="note" id="job-cmd"></div>
    <pre id="job-out"></pre>
  </div>
</main>

<script>
const $ = (s, r=document) => r.querySelector(s);
const SP = id => `https://open.spotify.com/playlist/${id}`;
let jobTimer = null, jobRunning = false;

function el(tag, attrs={}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'checked' || k === 'disabled' || k === 'hidden') e[k] = !!v;
    else e.setAttribute(k, v);
  }
  for (const k of kids) if (k != null) e.append(k);
  return e;
}
async function api(method, url, body) {
  const r = await fetch(url, {method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  const j = await r.json().catch(() => ({ok:false, error:`HTTP ${r.status}`}));
  if (!j.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
function say(node, text, bad) { node.textContent = text; node.className = 'msg ' + (bad ? 'err' : 'okk'); }

// Reference playlist picker: shows your playlists, or Spotify search results as you type.
function describe(p) { return `${p.name} \u2014 ${p.owner || '?'}${p.tracks != null ? ', ' + p.tracks + ' tracks' : ''}`; }
async function showRefName(input, label) {
  const id = (input.value.match(/[A-Za-z0-9]{22}/) || [])[0];
  if (!id) { label.textContent = ''; return; }
  label.textContent = 'looking up\u2026';
  try { const r = await api('GET', `/api/spotify/playlist/${id}`); label.textContent = describe(r.playlist); }
  catch (e) { label.textContent = e.message; }
}
function attachPicker(input, label, findBtn) {
  let box = null, timer = null;
  const close = () => { if (box) { box.remove(); box = null; } };
  const load = async (q) => {
    const ul = box.querySelector('ul'); ul.replaceChildren(el('li', {text: q ? 'searching\u2026' : 'loading your playlists\u2026'}));
    try {
      const r = await api('GET', '/api/spotify/search?q=' + encodeURIComponent(q));
      if (!box) return;
      ul.replaceChildren();
      if (!r.playlists.length) ul.append(el('li', {text: 'nothing found'}));
      for (const p of r.playlists) ul.append(el('li', {onclick: () => { input.value = p.id; label.textContent = describe(p); close(); }},
        el('span', {text: p.name}), el('small', {text: (p.owner || '') + (p.tracks != null ? ' \u00b7 ' + p.tracks : '')})));
      box.querySelector('.src').textContent = r.source === 'mine' ? 'Your playlists. Type a name to search all of Spotify.' : 'Spotify search results';
    } catch (e) { ul.replaceChildren(el('li', {text: e.message})); }
  };
  findBtn.onclick = () => {
    if (box) { close(); return; }
    const q = el('input', {type:'text', placeholder:'playlist name\u2026'});
    q.oninput = () => { clearTimeout(timer); timer = setTimeout(() => load(q.value.trim()), 400); };
    box = el('div', {class:'picker'}, q, el('div', {class:'note src'}), el('ul'));
    label.after(box); q.focus(); load('');
  };
  input.addEventListener('change', () => showRefName(input, label));
  showRefName(input, label);
}

// ---- tabs
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.toggle('active', x === b));
  document.querySelectorAll('section.tab').forEach(s => s.classList.toggle('active', s.id === 'tab-' + b.dataset.tab));
  if (b.dataset.tab === 'rejected') loadRejected();
  if (b.dataset.tab === 'log') loadLog();
});

// ---- playlists
function field(label, input) { return [el('label', {text: label}), input]; }
function card(p, st) {
  const f = {};
  const inp = (key, type, val, extra={}) => f[key] = el('input', {type, value: val ?? '', ...extra});
  const rows = [
    ...field('Name', inp('name', 'text', p.name)),
    ...field('Target size', inp('target', 'number', p.target, {min:1})),
  ];
  if (p.mode === 'bpm') {
    rows.push(...field('Min BPM', inp('min', 'number', p.min, {min:1})));
    rows.push(...field('Max BPM', inp('max', 'number', p.max, {min:1})));
    f.queries = el('textarea', {text: (p.queries || []).join('\n')});
    rows.push(...field('Searches (one per line)', f.queries));
  } else {
    const refIn = inp('reference', 'text', p.reference, {placeholder:'pick with Find, or paste a Spotify link'});
    const refLbl = el('div', {class:'refname'}), refBtn = el('button', {class:'act', text:'Find', type:'button'});
    rows.push(...field('Reference', el('div', {class:'refrow'}, refIn, refBtn)), refLbl);
    attachPicker(refIn, refLbl, refBtn);
    rows.push(...field('Genres', inp('genres', 'text', p.genres, {placeholder:'comma words; empty = no filter'})));
    f.filter_seeds = el('input', {type:'checkbox', checked: !!p.filter_seeds});
    rows.push(...field('Filter seeds', f.filter_seeds));
    rows.push(...field('Per artist', inp('per_artist', 'number', p.per_artist ?? 3, {min:1, max:10})));
  }
  const msg = el('div', {class:'msg'});
  const last = st && st.last_run ? `last run ${st.last_run.at.replace('T',' ')} (+${st.last_run.added} / -${st.last_run.removed})` : 'no runs yet';
  const meta = `${p.mode === 'bpm' ? 'BPM range' : 'sounds like'} · id ${p.id} · ${last}` + (st ? ` · ${st.rejected} rejected` : '');
  const collect = () => {
    const d = {};
    for (const [k, node] of Object.entries(f)) d[k] = node.type === 'checkbox' ? node.checked : node.value;
    return d;
  };
  const save = async () => {
    try { const r = await api('POST', `/api/playlists/${p.id}`, collect()); say(msg, 'Saved.'); Object.assign(p, r.playlist); }
    catch (e) { say(msg, e.message, true); }
  };
  const run = async (dry) => {
    if (!dry && !confirm(`Run the refresher now for "${p.name}"? This adds tracks to the real playlist.`)) return;
    try { const r = await api('POST', '/api/run', {id: p.id, dry_run: dry}); showJob(r.job); }
    catch (e) { say(msg, e.message, true); }
  };
  const remove = async () => {
    if (!confirm(`Remove "${p.name}" from the refresher? The Spotify playlist itself is not touched.`)) return;
    try { await api('DELETE', `/api/playlists/${p.id}`); loadPlaylists(); }
    catch (e) { say(msg, e.message, true); }
  };
  return el('div', {class:'card'},
    el('h3', {}, el('span', {text: p.name}), el('a', {href: SP(p.id), target:'_blank', rel:'noopener', text:'open in Spotify ↗'})),
    el('div', {class:'meta', text: meta}),
    el('div', {class:'grid'}, ...rows),
    el('div', {class:'btns'},
      el('button', {class:'act primary', text:'Save', onclick: save}),
      el('button', {class:'act job', text:'Preview', title:'refresh_playlists.py --dry-run --only ' + p.id, onclick: () => run(true)}),
      el('button', {class:'act job', text:'Run now', title:'refresh_playlists.py --only ' + p.id, onclick: () => run(false)}),
      el('button', {class:'act danger', text:'Remove from refresher', onclick: remove})),
    msg);
}
async function loadPlaylists() {
  const box = $('#cards'); box.replaceChildren();
  try {
    const r = await api('GET', '/api/config');
    if (!r.playlists.length) box.append(el('p', {class:'note', text:'No playlists in playlists.json yet. Use the Add playlist tab.'}));
    for (const p of r.playlists) box.append(card(p, r.state[p.id]));
  } catch (e) { box.append(el('p', {class:'msg err', text: e.message})); }
  setBusy(jobRunning);
}

// ---- add
function addPayload() {
  return {reference: $('#add-reference').value, name: $('#add-name').value, genres: $('#add-genres').value,
          filter_seeds: $('#add-filter_seeds').checked, per_artist: $('#add-per_artist').value, target: $('#add-target').value};
}
$('#add-preview').onclick = async () => {
  try { const r = await api('POST', '/api/add', {...addPayload(), dry_run: true}); say($('#add-msg'), ''); showJob(r.job); }
  catch (e) { say($('#add-msg'), e.message, true); }
};
$('#add-create').onclick = async () => {
  const d = addPayload();
  if (!confirm(`Create the playlist "${d.name}" on Spotify and add it to the refresher?`)) return;
  try { const r = await api('POST', '/api/add', {...d, dry_run: false}); say($('#add-msg'), ''); showJob(r.job); }
  catch (e) { say($('#add-msg'), e.message, true); }
};

// ---- rejected
async function loadRejected() {
  const box = $('#rejected'); box.replaceChildren();
  try {
    const r = await api('GET', '/api/state');
    let any = false;
    for (const pl of r.rejected) {
      if (!pl.tracks.length) continue;
      any = true;
      const ul = el('ul');
      for (const t of pl.tracks) {
        const li = el('li', {}, el('span', {text: t.label}),
          el('button', {class:'act', text:'Un-reject', onclick: async () => {
            try { await api('POST', '/api/unreject', {id: pl.id, track_id: t.id}); li.remove(); }
            catch (e) { alert(e.message); }
          }}));
        ul.append(li);
      }
      box.append(el('div', {class:'rej'}, el('h3', {text: pl.name + (pl.in_config ? '' : ' (no longer in playlists.json)')}), ul));
    }
    if (!any) box.append(el('p', {class:'note', text:'Nothing rejected yet. Delete a track in Spotify and the next refresh will remember it here.'}));
  } catch (e) { box.append(el('p', {class:'msg err', text: e.message})); }
}

// ---- log
async function loadLog() {
  try {
    const r = await api('GET', '/api/log?n=200');
    $('#logtext').textContent = r.lines.length ? r.lines.join('\n') : '(log is empty)';
    const s = await api('GET', '/api/state');
    const tb = $('#runs tbody'); tb.replaceChildren();
    for (const run of s.runs) tb.append(el('tr', {}, el('td', {text: (run.at || '').replace('T', ' ')}), el('td', {text: run.playlist}),
      el('td', {text: run.size_before}), el('td', {text: run.removed}), el('td', {text: run.added})));
    $('#logtext').scrollTop = $('#logtext').scrollHeight;
  } catch (e) { $('#logtext').textContent = e.message; }
}
$('#log-reload').onclick = loadLog;

// ---- job output
function setBusy(b) {
  jobRunning = b;
  document.querySelectorAll('button.job, #add-preview, #add-create').forEach(x => x.disabled = b);
}
function showJob(job) {
  const box = $('#jobbox');
  if (!job) { box.hidden = true; setBusy(false); return; }
  box.hidden = false;
  $('#job-title').textContent = job.title;
  $('#job-cmd').textContent = job.command + '   started ' + job.started.replace('T', ' ');
  const pill = $('#job-pill');
  pill.textContent = job.running ? 'running' : (job.rc === 0 ? 'done' : `failed (exit ${job.rc})`);
  pill.className = 'pill ' + (job.running ? 'run' : (job.rc === 0 ? 'ok' : 'bad'));
  const out = $('#job-out');
  const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 8;
  out.textContent = job.lines.join('\n') || (job.running ? '(waiting for output...)' : '(no output)');
  if (stick) out.scrollTop = out.scrollHeight;
  if (job.running) { setBusy(true); clearTimeout(jobTimer); jobTimer = setTimeout(pollJob, 1500); }
  else if (jobRunning) { setBusy(false); loadPlaylists(); if (job.result && job.result.added) { say($('#add-msg'), `Added "${job.result.added.name}" to the refresher.`); } }
  box.scrollIntoView({behavior:'smooth', block:'nearest'});
}
async function pollJob() {
  try { const r = await api('GET', '/api/job'); showJob(r.job); }
  catch (e) { jobTimer = setTimeout(pollJob, 3000); }
}

// ---- boot
attachPicker($('#add-reference'), $('#add-refname'), $('#add-find'));
(async () => {
  await loadPlaylists();
  try { const r = await api('GET', '/api/job'); if (r.job) { jobRunning = r.job.running; showJob(r.job); } } catch (e) {}
})();
</script>
</body></html>
"""


if __name__ == "__main__":
    load_env()
    print(f"panel: files in {DATA_DIR}; scripts run from {HERE}; listening on http://{HOST}:{PORT}", flush=True)
    app.run(host=HOST, port=PORT, debug=False, threaded=True)

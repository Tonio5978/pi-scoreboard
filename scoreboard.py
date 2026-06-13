#!/usr/bin/env python3
"""LED scoreboard for a 2x chained 64x32 RGB matrix (128x32).

Multi-sport ticker (MLB + NHL), iPhone-style card layout:

    [away logo] [away score]   <center: game state>   [home score] [home logo]

Center, by sport/state:
  - PRE-GAME : start time + day
  - MLB LIVE : inning + top/bottom arrow, bases diamond, count, outs
  - NHL LIVE : period + game clock (or INT during intermission)
  - FINAL    : FINAL (MLB shows /innings for extras; NHL shows /OT or /SO)

Rotation rule: lock to a favorite team's game while it's upcoming or live
(alternate if both favorites are live); once final, rotate the league. On a
favorites-off day, cycle all live games, else all final scores.

Run as root (GPIO):  cd ~/scoreboard && sudo -E $(which python) scoreboard.py
(Any python works: the guard below re-launches under the venv interpreter.)
"""

import os
import sys

# Re-exec under the project's venv python if started with a different
# interpreter (e.g. `$(which python)` -> /usr/bin/python, which lacks rgbmatrix
# + has an incompatible PIL).
_VENV_ROOT = os.path.dirname(os.path.abspath(__file__))
_VENV_PY = os.path.join(_VENV_ROOT, "bin", "python")
if os.path.exists(_VENV_PY) and os.path.abspath(sys.prefix) != _VENV_ROOT:
    os.execv(_VENV_PY, [_VENV_PY] + sys.argv)

import json
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

from rgbmatrix import RGBMatrix, RGBMatrixOptions
from rgbmatrix import graphics

try:
    from PIL import Image
    _HAVE_PIL = True
except ImportError:
    _HAVE_PIL = False

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
# Favorite team (display abbreviation) per league.
FAVORITE = {"MLB": "TOR", "NHL": "TOR"}   # Blue Jays / Maple Leafs

LOCAL_TZ = ZoneInfo("America/Toronto")
FONT_DIR = "fonts"
LOGO_DIR = "logos"
LOGO_SIZE = 24
DB_PATH = "scoreboard.db"
WEB_PORT = 8080                     # http://<pi-ip>:8080 to pick a game

# ESPN logo CDN (transparent PNGs, no circle).
LOGO_URL = "https://a.espncdn.com/i/teamlogos/{league}/500/{abbr}.png"

# MLB team id -> ESPN abbreviation.
MLB_ESPN = {
    108: "laa", 109: "ari", 110: "bal", 111: "bos", 112: "chc", 113: "cin",
    114: "cle", 115: "col", 116: "det", 117: "hou", 118: "kc", 119: "lad",
    120: "wsh", 121: "nym", 133: "ath", 134: "pit", 135: "sd", 136: "sea",
    137: "sf", 138: "stl", 139: "tb", 140: "tex", 141: "tor", 142: "min",
    143: "phi", 144: "atl", 145: "chw", 146: "mia", 147: "nyy", 158: "mil",
}
# NHL abbreviation -> ESPN abbreviation (only the ones that differ).
NHL_ESPN = {"LAK": "la", "SJS": "sj", "NJD": "nj", "TBL": "tb", "UTA": "utah"}

DWELL = 30                          # seconds each game shown while cycling
REFRESH_LIVE = 10                   # a favorite live game is on screen
REFRESH_PREGAME = 120              # waiting for first pitch / puck drop
REFRESH_CYCLE_LIVE = 30           # cycling through league live games
REFRESH_IDLE = 900                 # everything final / nothing to update
NHL_CLOCK_REFRESH = 10            # how often to re-fetch the live NHL clock

WIDTH, HEIGHT = 128, 32
CENTER_X = 64

WHITE = (255, 255, 255)
GREY = (110, 110, 110)
DIM = (45, 45, 45)
YELLOW = (255, 205, 0)

# Fallback colour for the abbreviation drawn when no logo is available (MLB).
TEAM_COLORS = {
    "ARI": (167, 25, 48), "ATL": (206, 17, 65), "BAL": (223, 90, 20),
    "BOS": (200, 55, 60), "CHC": (40, 90, 190), "CWS": (200, 200, 200),
    "CIN": (210, 20, 35), "CLE": (227, 40, 60), "COL": (120, 70, 190),
    "DET": (60, 110, 190), "HOU": (235, 110, 31), "KC": (40, 90, 190),
    "LAA": (200, 20, 45), "LAD": (30, 110, 190), "MIA": (0, 163, 224),
    "MIL": (255, 197, 47), "MIN": (211, 40, 80), "NYM": (252, 89, 16),
    "NYY": (70, 110, 200), "OAK": (0, 150, 100), "ATH": (0, 150, 100),
    "PHI": (232, 40, 55), "PIT": (253, 184, 39), "SD": (170, 130, 80),
    "SF": (253, 90, 30), "SEA": (0, 160, 150), "STL": (210, 40, 70),
    "TB": (120, 175, 225), "TEX": (40, 90, 190), "TOR": (40, 110, 200),
    "WSH": (200, 20, 60),
}


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------
class Glyph:
    def __init__(self, filename, width, height):
        self.font = graphics.Font()
        self.font.LoadFont(f"{FONT_DIR}/{filename}")
        self.width = width
        self.height = height


FONT_SCORE = None
FONT_BIG = None
FONT_SMALL = None


def load_fonts():
    global FONT_SCORE, FONT_BIG, FONT_SMALL
    FONT_SCORE = Glyph("10x20.bdf", 10, 20)
    FONT_BIG = Glyph("6x13.bdf", 6, 13)
    FONT_SMALL = Glyph("5x7.bdf", 5, 7)


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------
def text(canvas, glyph, x, y, color, s):
    graphics.DrawText(canvas, glyph.font, x, y, graphics.Color(*color), s)


def text_right(canvas, glyph, x_right, y, color, s):
    text(canvas, glyph, x_right - len(s) * glyph.width, y, color, s)


def text_center(canvas, glyph, cx, y, color, s):
    text(canvas, glyph, cx - (len(s) * glyph.width) // 2, y, color, s)


def diamond(canvas, cx, cy, r, color, fill):
    for dy in range(-r, r + 1):
        w = r - abs(dy)
        if fill:
            for dx in range(-w, w + 1):
                canvas.SetPixel(cx + dx, cy + dy, *color)
        else:
            canvas.SetPixel(cx - w, cy + dy, *color)
            canvas.SetPixel(cx + w, cy + dy, *color)


def dot(canvas, cx, cy, r, color, fill):
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            d2 = dx * dx + dy * dy
            if d2 <= r * r and (fill or d2 >= (r - 1) * (r - 1)):
                canvas.SetPixel(cx + dx, cy + dy, *color)


def arrow(canvas, cx, y0, h, color, pointing_up):
    for i in range(h):
        w = i if pointing_up else (h - 1 - i)
        for dx in range(-w, w + 1):
            canvas.SetPixel(cx + dx, y0 + i, *color)


def pause_icon(canvas, x, y, color):
    """Two vertical bars (||) at top-left x,y, 5px wide x 5px tall."""
    for dy in range(5):
        for dx in (0, 1, 3, 4):
            canvas.SetPixel(x + dx, y + dy, *color)


# --------------------------------------------------------------------------
# Logos (league-aware, ESPN transparent PNGs)
# --------------------------------------------------------------------------
_logo_cache = {}  # (league, abbr) -> list[(x, y, r, g, b)] or None


def _load_logo_pixels(league, abbr):
    if not _HAVE_PIL or not abbr:
        return None
    os.makedirs(LOGO_DIR, exist_ok=True)
    path = os.path.join(LOGO_DIR, f"{league}_{abbr}.png")

    def download():
        req = urllib.request.Request(
            LOGO_URL.format(league=league, abbr=abbr),
            headers={"User-Agent": "pi-scoreboard/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        with open(path, "wb") as f:
            f.write(data)

    if not os.path.exists(path):
        download()
    try:
        img = Image.open(path)
        img.load()
    except Exception:
        download()
        img = Image.open(path)
    img = img.convert("RGBA").resize((LOGO_SIZE, LOGO_SIZE), Image.LANCZOS)
    px = img.load()
    pixels = []
    for y in range(LOGO_SIZE):
        for x in range(LOGO_SIZE):
            r, g, b, a = px[x, y]
            if a > 80:
                pixels.append((x, y, r, g, b))
    return pixels


def get_logo(league, abbr):
    key = (league, abbr)
    if key not in _logo_cache:
        try:
            _logo_cache[key] = _load_logo_pixels(league, abbr)
        except Exception as e:
            print("logo error", key, e)
            _logo_cache[key] = None
    return _logo_cache[key]


def draw_logo(canvas, team, x0):
    y0 = (HEIGHT - LOGO_SIZE) // 2
    pixels = get_logo(team["league"], team["logo_abbr"])
    if pixels:
        for x, y, r, g, b in pixels:
            canvas.SetPixel(x0 + x, y0 + y, r, g, b)
    else:
        text(canvas, FONT_BIG,
             x0 + (LOGO_SIZE - len(team["abbr"]) * FONT_BIG.width) // 2,
             20, team["color"], team["abbr"])


# --------------------------------------------------------------------------
# Normalized game model
# --------------------------------------------------------------------------
class Game:
    def __init__(self, league, key, state, away, home,
                 start_dt=None, detail=None, is_fav=False, date=None):
        self.league = league        # "MLB" | "NHL"
        self.key = key
        self.uid = f"{league}:{key}"
        self.state = state          # "live" | "final" | "pregame"
        self.away = away            # team dict
        self.home = home
        self.start_dt = start_dt
        self.detail = detail or {}
        self.is_fav = is_fav
        self.date = date

    def to_dict(self):
        return {
            "league": self.league, "key": self.key, "state": self.state,
            "away": self.away, "home": self.home,
            "start": self.start_dt.isoformat() if self.start_dt else None,
            "detail": self.detail, "is_fav": self.is_fav, "date": self.date,
        }

    @classmethod
    def from_dict(cls, d):
        start = datetime.fromisoformat(d["start"]) if d.get("start") else None
        return cls(d["league"], d["key"], d["state"], d["away"], d["home"],
                   start, d.get("detail"), d.get("is_fav", False), d.get("date"))


def http_get_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "pi-scoreboard/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def today_iso():
    return datetime.now(LOCAL_TZ).date().isoformat()


# --------------------------------------------------------------------------
# Shared state between the display loop and the web server
# --------------------------------------------------------------------------
class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self._games = []     # latest full list of today's games (for the web UI)
        self._pin = None     # uid of the game the user chose to watch

    def set_games(self, games):
        with self._lock:
            self._games = games

    def games(self):
        with self._lock:
            return list(self._games)

    def set_pin(self, uid):
        with self._lock:
            self._pin = uid or None

    def pin(self):
        with self._lock:
            return self._pin


STATE = SharedState()


# --------------------------------------------------------------------------
# MLB
# --------------------------------------------------------------------------
MLB_SCHEDULE_URL = (
    "https://statsapi.mlb.com/api/v1/schedule"
    "?sportId=1&date={date}&hydrate=linescore,team"
)


def _mlb_team(side):
    t = side["team"]
    abbr = t.get("abbreviation", t.get("teamName", "?"))[:3].upper()
    return {
        "abbr": abbr,
        "score": side.get("score"),
        "color": TEAM_COLORS.get(abbr, WHITE),
        "logo_abbr": MLB_ESPN.get(t["id"]),
        "league": "mlb",
    }


def _mlb_state(s):
    return {"Live": "live", "Final": "final"}.get(s, "pregame")


def mlb_fetch_today():
    date = today_iso()
    data = http_get_json(MLB_SCHEDULE_URL.format(date=date))
    games = []
    for d in data.get("dates", []):
        for g in d["games"]:
            away = _mlb_team(g["teams"]["away"])
            home = _mlb_team(g["teams"]["home"])
            start = datetime.fromisoformat(g["gameDate"])
            is_fav = FAVORITE["MLB"] in (away["abbr"], home["abbr"])
            games.append(Game(
                "MLB", g["gamePk"],
                _mlb_state(g["status"]["abstractGameState"]),
                away, home, start, g.get("linescore", {}), is_fav, d["date"]))
    return games


def mlb_draw_center_live(canvas, ls):
    is_top = ls.get("isTopInning", True)
    arrow(canvas, 58, 1, 4, WHITE, pointing_up=is_top)
    text(canvas, FONT_SMALL, 63, 7, WHITE, str(ls.get("currentInning", "")))

    if ls.get("inningState", "") in ("Middle", "End"):
        text_center(canvas, FONT_SMALL, CENTER_X, 22, GREY,
                    ls["inningState"][:3].upper())
        return

    offense = ls.get("offense", {})
    for cx, cy, name in [(64, 12, "second"), (70, 18, "first"), (58, 18, "third")]:
        diamond(canvas, cx, cy, 4, GREY, fill=False)
        if name in offense:
            diamond(canvas, cx, cy, 3, YELLOW, fill=True)

    text(canvas, FONT_SMALL, 49, 31, WHITE,
         f"{ls.get('balls', 0)}-{ls.get('strikes', 0)}")
    outs = ls.get("outs", 0)
    for i in range(2):
        filled = outs > i
        dot(canvas, 68 + i * 5, 28, 1, WHITE if filled else DIM, fill=filled)


def mlb_draw_center_final(canvas, ls):
    innings = ls.get("currentInning")
    if innings and innings != 9:
        text_center(canvas, FONT_BIG, CENTER_X, 15, WHITE, "FINAL")
        text_center(canvas, FONT_SMALL, CENTER_X, 27, GREY, f"/{innings}")
    else:
        text_center(canvas, FONT_BIG, CENTER_X, 20, WHITE, "FINAL")


# --------------------------------------------------------------------------
# NHL
# --------------------------------------------------------------------------
NHL_SCHEDULE_URL = "https://api-web.nhle.com/v1/schedule/{date}"
NHL_PBP_URL = "https://api-web.nhle.com/v1/gamecenter/{id}/play-by-play"


def _nhl_team(t):
    abbr = t.get("abbrev", "?")
    return {
        "abbr": abbr,
        "score": t.get("score"),
        "color": WHITE,
        "logo_abbr": NHL_ESPN.get(abbr, abbr.lower()),
        "league": "nhl",
    }


def _nhl_state(s):
    if s in ("LIVE", "CRIT"):
        return "live"
    if s in ("FINAL", "OFF", "OVER"):
        return "final"
    return "pregame"


# Last-known live clock per NHL game uid. The schedule feed has no clock, so we
# seed freshly-built game objects from here to avoid a blank clock between a
# rotation rebuild and the next play-by-play fetch.
_NHL_CLOCK = {}


def nhl_fetch_today():
    date = today_iso()
    data = http_get_json(NHL_SCHEDULE_URL.format(date=date))
    games = []
    for day in data.get("gameWeek", []):
        if day.get("date") != date:
            continue
        for g in day.get("games", []):
            away = _nhl_team(g["awayTeam"])
            home = _nhl_team(g["homeTeam"])
            start = datetime.fromisoformat(g["startTimeUTC"])
            pd = g.get("periodDescriptor") or {}
            detail = {"period_num": pd.get("number"),
                      "period_type": pd.get("periodType"),
                      "clock": None, "intermission": False, "running": True}
            cached = _NHL_CLOCK.get(f"NHL:{g['id']}")
            if cached:
                detail.update(cached)
            is_fav = FAVORITE["NHL"] in (away["abbr"], home["abbr"])
            games.append(Game("NHL", g["id"], _nhl_state(g["gameState"]),
                              away, home, start, detail, is_fav, date))
    return games


def nhl_update_clock(game):
    """Fetch the live clock/period for one NHL game (play-by-play feed)."""
    d = http_get_json(NHL_PBP_URL.format(id=game.key))
    clock = d.get("clock") or {}
    pd = d.get("periodDescriptor") or {}
    info = {
        "clock": clock.get("timeRemaining"),
        "intermission": clock.get("inIntermission", False),
        "running": clock.get("running", True),
    }
    if pd.get("number"):
        info["period_num"] = pd["number"]
        info["period_type"] = pd.get("periodType")
    game.detail.update(info)
    _NHL_CLOCK[game.uid] = info
    game.away["score"] = d["awayTeam"].get("score", game.away["score"])
    game.home["score"] = d["homeTeam"].get("score", game.home["score"])


def _nhl_period_str(num, ptype):
    if ptype == "SO":
        return "SO"
    if ptype == "OT":
        n = (num or 4) - 3
        return "OT" if n <= 1 else f"{n}OT"
    return {1: "1ST", 2: "2ND", 3: "3RD"}.get(num, str(num or ""))


def nhl_draw_center_live(canvas, detail):
    period = _nhl_period_str(detail.get("period_num"), detail.get("period_type"))
    clock = detail.get("clock")

    if detail.get("intermission"):
        # Between periods: show INT + the intermission countdown.
        text_center(canvas, FONT_BIG, CENTER_X, 14, YELLOW, "INT")
        if clock:
            text_center(canvas, FONT_SMALL, CENTER_X, 26, GREY, clock)
        return

    text_center(canvas, FONT_BIG, CENTER_X, 14, WHITE, period)
    if not clock:
        return
    running = detail.get("running", True)
    # Clock stopped (whistle/stoppage): amber time + a pause icon.
    if not running:
        w = len(clock) * FONT_SMALL.width
        pause_icon(canvas, CENTER_X - w // 2 - 6, 21, YELLOW)
    text_center(canvas, FONT_SMALL, CENTER_X, 26,
                WHITE if running else YELLOW, clock)


def nhl_draw_center_final(canvas, detail):
    ptype = detail.get("period_type")
    if ptype in ("OT", "SO"):
        text_center(canvas, FONT_BIG, CENTER_X, 15, WHITE, "FINAL")
        text_center(canvas, FONT_SMALL, CENTER_X, 27, GREY, f"/{ptype}")
    else:
        text_center(canvas, FONT_BIG, CENTER_X, 20, WHITE, "FINAL")


# --------------------------------------------------------------------------
# Shared rendering
# --------------------------------------------------------------------------
def draw_sides(canvas, away, home, show_scores):
    draw_logo(canvas, away, 0)
    draw_logo(canvas, home, WIDTH - LOGO_SIZE)
    if show_scores:
        if away["score"] is not None:
            text(canvas, FONT_SCORE, LOGO_SIZE + 3, 24, WHITE, str(away["score"]))
        if home["score"] is not None:
            text_right(canvas, FONT_SCORE, WIDTH - LOGO_SIZE - 3, 24, WHITE,
                       str(home["score"]))


def draw_center_pregame(canvas, start_dt):
    if start_dt is None:
        return
    start = start_dt.astimezone(LOCAL_TZ)
    today = datetime.now(LOCAL_TZ).date()
    hour = start.hour % 12 or 12
    ampm = "P" if start.hour >= 12 else "A"
    time_str = f"{hour}:{start.minute:02d}{ampm}"
    day_str = "TODAY" if start.date() == today else start.strftime("%a %d").upper()
    text_center(canvas, FONT_SMALL, CENTER_X, 15, WHITE, time_str)
    text_center(canvas, FONT_SMALL, CENTER_X, 27, GREY, day_str)


def render(canvas, game):
    canvas.Clear()
    if game is None:
        text_center(canvas, FONT_BIG, CENTER_X, 20, GREY, "NO GAMES")
        return
    draw_sides(canvas, game.away, game.home,
               show_scores=game.state in ("live", "final"))
    if game.state == "pregame":
        draw_center_pregame(canvas, game.start_dt)
    elif game.league == "MLB":
        (mlb_draw_center_live if game.state == "live"
         else mlb_draw_center_final)(canvas, game.detail)
    elif game.league == "NHL":
        (nhl_draw_center_live if game.state == "live"
         else nhl_draw_center_final)(canvas, game.detail)


# --------------------------------------------------------------------------
# SQLite cache of final games
# --------------------------------------------------------------------------
_conn = None


def init_db():
    global _conn
    _conn = sqlite3.connect(DB_PATH)
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS games ("
        "uid TEXT PRIMARY KEY, league TEXT, date TEXT, state TEXT,"
        "data TEXT, updated_at TEXT)"
    )
    _conn.commit()


def cache_finals(games):
    if _conn is None:
        return
    now = datetime.now(LOCAL_TZ).isoformat()
    rows = [(g.uid, g.league, g.date, "final", json.dumps(g.to_dict()), now)
            for g in games if g.state == "final"]
    if rows:
        _conn.executemany("INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?)", rows)
        _conn.commit()


def load_cached_finals(date):
    if _conn is None:
        return []
    cur = _conn.execute(
        "SELECT data FROM games WHERE date=? AND state='final'", (date,))
    return [Game.from_dict(json.loads(r[0])) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# Rotation
# --------------------------------------------------------------------------
class Rotation:
    def __init__(self, games, refresh, cycling):
        self.games = games
        self.refresh = refresh
        self.cycling = cycling


def _fetch_all():
    """All of today's games across leagues, with cached-finals fallback."""
    games = []
    for fetch in (mlb_fetch_today, nhl_fetch_today):
        try:
            games += fetch()
        except Exception as e:
            print("fetch error:", fetch.__name__, e)
    if games:
        cache_finals(games)
    else:
        games = load_cached_finals(today_iso())
    return games


def build_rotation():
    games = _fetch_all()
    STATE.set_games(games)

    # A manually-picked game wins over everything (until it ends). Interleave it
    # so it reappears between every other game.
    pin = STATE.pin()
    if pin:
        pinned = next((g for g in games if g.uid == pin), None)
        if pinned is None or pinned.state == "final":
            STATE.set_pin(None)
        else:
            base = [g for g in games if g.uid != pin and g.state == "live"]
            if not base:
                base = [g for g in games if g.uid != pin and g.state == "final"]
            order = []
            for g in base:
                order += [pinned, g]
            return Rotation(order or [pinned], REFRESH_CYCLE_LIVE,
                            cycling=len(order) > 1)

    favs_live = [g for g in games if g.is_fav and g.state == "live"]
    if favs_live:
        return Rotation(favs_live, REFRESH_LIVE, cycling=len(favs_live) > 1)

    favs_pre = [g for g in games if g.is_fav and g.state == "pregame"]
    if favs_pre:
        return Rotation(favs_pre, REFRESH_PREGAME, cycling=len(favs_pre) > 1)

    live = [g for g in games if g.state == "live"]
    if live:
        return Rotation(live, REFRESH_CYCLE_LIVE, cycling=True)

    finals = [g for g in games if g.state == "final"] or load_cached_finals(today_iso())
    if finals:
        return Rotation(finals, REFRESH_IDLE, cycling=True)

    return Rotation([], REFRESH_IDLE, cycling=False)


# --------------------------------------------------------------------------
# Web interface (pick a game to watch)
# --------------------------------------------------------------------------
PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Scoreboard</title><style>
body{background:#0d0d0f;color:#eee;font-family:system-ui,sans-serif;margin:0;padding:16px}
h1{font-size:18px;display:flex;justify-content:space-between;align-items:center}
.league{color:#888;font-size:12px;margin:16px 0 4px;text-transform:uppercase;letter-spacing:1px}
.row{display:flex;align-items:center;gap:8px;background:#1b1b1f;border-radius:10px;padding:8px 10px;margin:6px 0}
.row.pinned{outline:2px solid #4ad}
.logo{width:30px;height:30px;object-fit:contain}
.score{font-weight:700;font-size:18px;min-width:22px;text-align:center}
.teams{flex:1;font-size:14px}
.state{font-size:11px;color:#9ad;min-width:54px;text-align:center}
button{background:#2a9d5a;color:#fff;border:0;border-radius:8px;padding:8px 12px;font-size:13px;cursor:pointer}
.row.pinned button{background:#4ad}
.clear{background:#a33;font-size:12px;padding:6px 10px}
</style></head><body>
<h1>Scoreboard <button class=clear onclick="clr()">Auto / Clear pick</button></h1>
<div id=list></div>
<script>
async function load(){
 let d; try{d=await (await fetch('/api/games')).json()}catch(e){return}
 const el=document.getElementById('list');el.innerHTML='';let last='';
 for(const g of d.games){
  if(g.league!=last){last=g.league;const h=document.createElement('div');h.className='league';h.textContent=g.league;el.appendChild(h);}
  const r=document.createElement('div');r.className='row'+(g.pinned?' pinned':'');
  const st=g.state=='pregame'?g.start:g.state.toUpperCase();
  r.innerHTML=`<img class=logo src="${g.away.logo}" onerror="this.style.visibility='hidden'">
   <span class=score>${g.away.score??''}</span>
   <div class=teams><b>${g.away.abbr}</b> @ <b>${g.home.abbr}</b></div>
   <span class=score>${g.home.score??''}</span>
   <img class=logo src="${g.home.logo}" onerror="this.style.visibility='hidden'">
   <span class=state>${st}</span>`;
  const b=document.createElement('button');b.textContent=g.pinned?'Watching':'Watch';
  b.onclick=()=>act(g.pinned?'/clear':'/pin?uid='+encodeURIComponent(g.uid));
  r.appendChild(b);el.appendChild(r);
 }
 if(!d.games.length)el.innerHTML='<p style=color:#888>No games today.</p>';
}
async function act(u){await fetch(u);load()}
async function clr(){await fetch('/clear');load()}
load();setInterval(load,5000);
</script></body></html>"""


def _game_web_dict(g):
    def t(team):
        la = team.get("logo_abbr")
        logo = LOGO_URL.format(league=team["league"], abbr=la) if la else ""
        return {"abbr": team["abbr"], "score": team["score"], "logo": logo}

    start = ""
    if g.start_dt:
        s = g.start_dt.astimezone(LOCAL_TZ)
        start = f"{s.hour % 12 or 12}:{s.minute:02d}{'PM' if s.hour >= 12 else 'AM'}"
    return {"uid": g.uid, "league": g.league, "state": g.state, "start": start,
            "away": t(g.away), "home": t(g.home)}


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, PAGE.encode())
        elif u.path == "/api/games":
            pin = STATE.pin()
            order = {"live": 0, "pregame": 1, "final": 2}
            data = [_game_web_dict(g) for g in STATE.games()]
            for d in data:
                d["pinned"] = (d["uid"] == pin)
            data.sort(key=lambda d: (order.get(d["state"], 3), d["league"]))
            body = json.dumps({"games": data, "pin": pin}).encode()
            self._send(200, body, "application/json")
        elif u.path == "/pin":
            uid = parse_qs(u.query).get("uid", [None])[0]
            STATE.set_pin(uid)
            self._send(200, b'{"ok":true}', "application/json")
        elif u.path == "/clear":
            STATE.set_pin(None)
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(404, b"not found")


def start_web():
    srv = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), WebHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"web interface on :{WEB_PORT}")


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def main():
    options = RGBMatrixOptions()
    options.rows = 32
    options.cols = 64
    options.chain_length = 2
    options.gpio_slowdown = 4
    options.brightness = 40
    # Stay root after GPIO init so we can read logos/ under the 700-mode home.
    options.drop_privileges = False

    matrix = RGBMatrix(options=options)
    canvas = matrix.CreateFrameCanvas()
    load_fonts()
    init_db()
    start_web()

    rot = build_rotation()
    idx = 0
    cur_uid = None
    applied_pin = STATE.pin()
    cur_day = today_iso()
    last_fetch = time.monotonic()
    last_switch = time.monotonic()
    last_clock = 0.0

    while True:
        now = time.monotonic()

        # Rebuild on the normal cadence, when the web pick changes, or when the
        # local date changes. The date can jump well after startup: the Pi has no
        # RTC, so on boot it may hold a stale time until NTP syncs a few seconds
        # later -- without this we'd stay stuck on the old day's (final) games
        # until the next idle refresh (~15 min). Also handles midnight rollover.
        if (now - last_fetch >= rot.refresh
                or STATE.pin() != applied_pin
                or today_iso() != cur_day):
            rot = build_rotation()
            last_fetch = now
            applied_pin = STATE.pin()
            cur_day = today_iso()
            uids = [g.uid for g in rot.games]
            if cur_uid in uids:
                idx = uids.index(cur_uid)
            else:
                idx = 0
                last_switch = now

        if rot.cycling and len(rot.games) > 1 and now - last_switch >= DWELL:
            idx = (idx + 1) % len(rot.games)
            last_switch = now

        game = rot.games[idx] if rot.games else None
        switched = game is not None and game.uid != cur_uid
        cur_uid = game.uid if game else None

        # Keep the live NHL clock fresh for the game on screen.
        if game and game.league == "NHL" and game.state == "live":
            if switched or now - last_clock >= NHL_CLOCK_REFRESH:
                try:
                    nhl_update_clock(game)
                except Exception as e:
                    print("clock error:", e)
                last_clock = now

        render(canvas, game)
        canvas = matrix.SwapOnVSync(canvas)
        time.sleep(1)


if __name__ == "__main__":
    main()

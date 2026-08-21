"""Arena dashboard: watch every match in a browser, with a drawn hex board.

    python dashboard.py            # http://localhost:8765
    python dashboard.py 9000       # custom port

Reads the same snapshots arena.py writes, so it needs no cooperation from the
running match: state is replayed from the move log, exactly. Pages refresh
themselves every 5 seconds. Stdlib only.
"""

import glob
import html
import json
import math
import os
import pickle
import re
import statistics
import sys
import traceback
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from catanatron import RESOURCES, Color, Game, RandomPlayer
from catanatron.state_functions import (
    get_actual_victory_points,
    get_largest_army,
    get_longest_road_color,
    get_player_buildings,
    player_key,
    player_num_dev_cards,
)

COLORS = [Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE]
SIZE = 46  # hex radius in px
ANGLES = {  # pointy-top hex: vertices at N/S, verified against shared nodes
    "NORTH": 90,
    "NORTHEAST": 30,
    "SOUTHEAST": -30,
    "SOUTH": -90,
    "SOUTHWEST": -150,
    "NORTHWEST": 150,
}
TILE_FILL = {
    "WOOD": "#2f6b3a",
    "BRICK": "#a4472c",
    "SHEEP": "#7fae4a",
    "WHEAT": "#d8a63c",
    "ORE": "#6b7280",
    None: "#c2b280",
}
SEAT_FILL = {"RED": "#d64545", "BLUE": "#3b6fd4", "WHITE": "#e8e6df", "ORANGE": "#e08a2e"}


_cursor = {}  # base -> (actions_applied, game): stepping forward costs one action
_blobs = {}  # path -> (stat key, blob): playback would otherwise unpickle per frame


def snapshot(base):
    """The recorded game, re-read only when the match writes a new snapshot."""
    path = base + ".pkl"
    st = os.stat(path)
    key = (st.st_mtime_ns, st.st_size)
    hit = _blobs.get(path)
    if hit and hit[0] == key:
        return hit[1]
    with open(path, "rb") as f:
        blob = pickle.load(f)
    _blobs[path] = (key, blob)
    return blob


def load(base, upto=None):
    """Replay the recorded actions — all of them, or the first `upto` (scrubbing)."""
    blob = snapshot(base)
    actions = blob["actions"]
    target = len(actions) if upto is None else max(0, min(upto, len(actions)))
    applied, game = _cursor.get(base, (0, None))
    if game is None or applied > target:  # rewind: replay from the start
        game = Game([RandomPlayer(c) for c in COLORS[: len(blob["specs"])]], seed=blob["seed"])
        applied = 0
    for action in actions[applied:target]:
        game.execute(action, validate_action=False)
    _cursor[base] = (target, game)
    return game, blob["specs"], len(actions)


_vp = {}  # base -> (n_actions, samples): one replay per snapshot, not per frame


def vp_series(base, samples=140):
    """[(move, turn, {seat: vp})] sampled across the whole recorded game."""
    blob = snapshot(base)
    actions = blob["actions"]
    hit = _vp.get(base)
    if hit and hit[0] == len(actions):
        return hit[1]
    game = Game([RandomPlayer(c) for c in COLORS[: len(blob["specs"])]], seed=blob["seed"])
    step = max(1, len(actions) // samples)
    out = [(0, 0, {c.value: 0 for c in game.state.colors})]
    for i, action in enumerate(actions):
        game.execute(action, validate_action=False)
        if i % step == 0 or i == len(actions) - 1:
            out.append(
                (
                    i + 1,
                    game.state.num_turns,
                    {c.value: get_actual_victory_points(game.state, c) for c in game.state.colors},
                )
            )
    _vp[base] = (len(actions), out)
    return out


def vp_history(base, upto=None):
    """Victory points per seat over time, cut at the move being viewed."""
    rows = vp_series(base)
    if upto is not None:  # scrubbing must not spoil the ending
        rows = [r for r in rows if r[0] <= upto] or rows[:1]
    turns = [r[1] for r in rows]
    return turns, {seat: [r[2][seat] for r in rows] for seat in rows[0][2]}


def vp_chart(turns, series, labels, width=560, height=196):
    """Step chart with a win line, a turn axis, and a named legend."""
    if len(turns) < 3:
        return "<div class=muted>chart appears after a few moves</div>"
    top = max(10, max(max(v) for v in series.values()))
    left, right, head, foot = 30, width - 40, 14, height - 44
    sx = lambda t: left + (right - left) * t / max(turns[-1], 1)
    sy = lambda v: foot - (foot - head) * v / top
    out = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">'
        "<title>victory points over time</title>"
    ]
    tick = 'font-family="IBM Plex Mono,monospace" font-size="9.5" letter-spacing=".05em"'
    for vp in range(0, top + 1, 2):
        y, win = sy(vp), vp == 10
        out.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" '
            f'stroke="{"#6b6a52" if win else "#26251d"}"'
            + (' stroke-dasharray="4 5"' if win else "")
            + "/>"
        )
        out.append(
            f'<text x="{left - 8}" y="{y:.1f}" text-anchor="end" dominant-baseline="central" '
            f'{tick} fill="{"#c9c079" if win else "#6d6a5c"}">{vp}</text>'
        )
    out.append(
        f'<text x="{right}" y="{sy(10) - 7:.1f}" text-anchor="end" {tick} fill="#c9c079" '
        'opacity=".8">WIN</text>'
    )
    for t in (0, turns[-1]):
        out.append(
            f'<text x="{sx(t):.1f}" y="{foot + 16:.1f}" text-anchor="middle" '
            f'{tick} fill="#6d6a5c">turn {t}</text>'
        )
    legend = []
    for slot, (color, values) in enumerate(series.items()):
        # nudge each line a hair apart so tied scores stay readable
        offset = (slot - (len(series) - 1) / 2) * 1.6
        points = " ".join(
            f"{sx(t):.1f},{sy(v) + offset:.1f}" for t, v in zip(turns, values)
        )
        out.append(
            f'<polygon points="{sx(turns[0]):.1f},{foot} {points} {sx(turns[-1]):.1f},{foot}" '
            f'fill="{SEAT_FILL[color]}" opacity=".07"/>'
        )
        out.append(
            f'<polyline points="{points}" fill="none" stroke="{SEAT_FILL[color]}" '
            'stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>'
        )
        ex, ey = sx(turns[-1]), sy(values[-1]) + offset
        out.append(
            f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="3.2" fill="{SEAT_FILL[color]}" '
            'stroke="#100f0a" stroke-width="1.5"/>'
        )
        out.append(
            f'<text x="{ex + 8:.1f}" y="{ey:.1f}" dominant-baseline="central" '
            'font-family="IBM Plex Mono,monospace" font-size="11" font-weight="500" '
            f'fill="{SEAT_FILL[color]}">{values[-1]}</text>'
        )
        name = short(labels.get(color, "?"))
        legend.append(
            f"<span class=lg><span class=dot style='background:{SEAT_FILL[color]}'></span>"
            f"{name} <b>{values[-1]}</b></span>"
        )
    out.append("</svg>")
    return "".join(out) + f"<div class=legend>{''.join(legend)}</div>"


_logs = {}  # path -> (stat key, parsed rows)


def read_jsonl(path):
    """Parsed lines, re-read only when the file grows (playback hits this a lot)."""
    if not os.path.exists(path):
        return []
    st = os.stat(path)
    key = (st.st_mtime_ns, st.st_size)
    hit = _logs.get(path)
    if hit and hit[0] == key:
        return hit[1]
    rows = []
    with open(path, errors="replace") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    _logs[path] = (key, rows)
    return rows


def decisions(match_name, game_index):
    return [r for r in read_jsonl(match_name + "_decisions.jsonl") if r.get("game") == game_index]


def short(spec):
    """`llm:openai/gpt-5.6-luna` -> `gpt-5.6-luna`: seats are named by model, not colour."""
    return html.escape(str(spec).removeprefix("llm:").split("/")[-1])


def spend(rows, upto_turn=None):
    """Per-seat spend so far: {color: {cost, moves, retries, tokens}}.

    New logs carry a per-move `cost_usd` delta plus a cumulative `cost_total`;
    older logs put the running total in `cost_usd`, hence the two branches.
    """
    out = {}
    for row in rows:
        if upto_turn is not None and (row.get("turn") or 0) > upto_turn:
            break
        s = out.setdefault(
            row.get("color"), {"cost": 0.0, "moves": 0, "retries": 0, "tokens": 0}
        )
        if row.get("type") == "retry":
            s["retries"] += 1
            continue
        s["moves"] += 1
        s["tokens"] += (row.get("tokens_in") or 0) + (row.get("tokens_out") or 0)
        if "cost_total" in row:
            s["cost"] += row.get("cost_usd") or 0
        else:
            s["cost"] = max(s["cost"], row.get("cost_usd") or 0)
    return out


def node_positions(board_map):
    """node id -> (x, y); tiles sharing a node agree exactly (see test below)."""
    positions, centers = {}, {}
    for coord, tile in board_map.land_tiles.items():
        x, _, z = coord
        cx, cy = SIZE * math.sqrt(3) * (x + z / 2), SIZE * 1.5 * z
        centers[coord] = (cx, cy)
        for ref, node_id in tile.nodes.items():
            angle = math.radians(ANGLES[ref.value])
            positions[node_id] = (cx + SIZE * math.cos(angle), cy - SIZE * math.sin(angle))
    return positions, centers


def board_svg(game, last_action=None):
    state = game.state
    board = state.board
    positions, centers = node_positions(board.map)
    xs = [p[0] for p in positions.values()]
    ys = [p[1] for p in positions.values()]
    pad = 30
    ox, oy = pad - min(xs), pad - min(ys)
    width, height = max(xs) - min(xs) + 2 * pad, max(ys) - min(ys) + 2 * pad
    at = lambda p: (p[0] + ox, p[1] + oy)

    out = [f'<svg viewBox="0 0 {width:.0f} {height:.0f}" width="100%" role="img">']
    out.append("<title>Catan board</title>")
    for coord, tile in board.map.land_tiles.items():
        cx, cy = at(centers[coord])
        pts = " ".join(
            f"{cx + SIZE * math.cos(math.radians(a)):.1f},{cy - SIZE * math.sin(math.radians(a)):.1f}"
            for a in (90, 30, -30, -90, -150, 150)
        )
        out.append(
            f'<polygon points="{pts}" fill="{TILE_FILL[tile.resource]}" '
            'stroke="#1c1c1a" stroke-width="1.5"/>'
        )
        if tile.resource is not None:
            hot = tile.number in (6, 8)
            out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="15" fill="#f2ede1"/>')
            out.append(
                f'<text x="{cx:.1f}" y="{cy:.1f}" text-anchor="middle" dominant-baseline="central" '
                f'font-size="15" font-weight="600" fill="{"#b3261e" if hot else "#1c1c1a"}">'
                f"{tile.number}</text>"
            )
        else:
            out.append(
                f'<text x="{cx:.1f}" y="{cy:.1f}" text-anchor="middle" dominant-baseline="central" '
                'font-size="11" fill="#4a4436">desert</text>'
            )
        if board.robber_coordinate == coord:
            out.append(
                f'<circle cx="{cx:.1f}" cy="{cy + 26:.1f}" r="9" fill="#111" stroke="#fff" stroke-width="1.5"/>'
            )

    for color in state.colors:
        fill = SEAT_FILL[color.value]
        for edge in get_player_buildings(state, color, "ROAD"):
            (x1, y1), (x2, y2) = at(positions[edge[0]]), at(positions[edge[1]])
            out.append(
                f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                f'stroke="{fill}" stroke-width="7" stroke-linecap="round" opacity="0.95"/>'
            )
    for node_id, (px, py) in positions.items():
        x, y = at((px, py))
        out.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" fill="#00000022"/>'
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" dominant-baseline="central" '
            f'font-size="9" fill="#fffc">{node_id}</text>'
        )
    for color in state.colors:
        fill = SEAT_FILL[color.value]
        for node_id in get_player_buildings(state, color, "SETTLEMENT"):
            x, y = at(positions[node_id])
            out.append(
                f'<rect x="{x - 9:.1f}" y="{y - 9:.1f}" width="18" height="18" rx="3" '
                f'fill="{fill}" stroke="#1c1c1a" stroke-width="1.5"/>'
            )
        for node_id in get_player_buildings(state, color, "CITY"):
            x, y = at(positions[node_id])
            out.append(
                f'<rect x="{x - 12:.1f}" y="{y - 12:.1f}" width="24" height="24" rx="4" '
                f'fill="{fill}" stroke="#1c1c1a" stroke-width="2.5"/>'
                f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" dominant-baseline="central" '
                'font-size="11" font-weight="600" fill="#1c1c1a">C</text>'
            )
    if last_action is not None:  # ring whatever just changed
        kind, value = last_action.action_type.value, last_action.value
        spot = None
        # dispatch on action type, not value shape: a ROLL's (3, 6) looks
        # exactly like a road between nodes 3 and 6
        if kind in ("BUILD_SETTLEMENT", "BUILD_CITY") and value in positions:
            spot = at(positions[value])
        elif kind == "BUILD_ROAD" and isinstance(value, tuple) and value[0] in positions:
            (x1, y1), (x2, y2) = at(positions[value[0]]), at(positions[value[1]])
            spot = ((x1 + x2) / 2, (y1 + y2) / 2)
        elif kind == "MOVE_ROBBER" and isinstance(value, tuple) and value[0] in centers:
            spot = at(centers[value[0]])
        if spot:
            out.append(
                f'<circle class="lastring" cx="{spot[0]:.1f}" cy="{spot[1]:.1f}" r="20" '
                'fill="none" stroke="#e8c96a" stroke-width="3" opacity="0.95"/>'
            )
    out.append("</svg>")
    return "".join(out)


DEV_CARDS = ["KNIGHT", "VICTORY_POINT", "ROAD_BUILDING", "YEAR_OF_PLENTY", "MONOPOLY"]
DEV_LABEL = {
    "KNIGHT": "knight",
    "VICTORY_POINT": "vp card",
    "ROAD_BUILDING": "road building",
    "YEAR_OF_PLENTY": "year of plenty",
    "MONOPOLY": "monopoly",
}


def seat_rows(game, specs):
    state = game.state
    seats = dict(zip(COLORS, specs))
    rows = []
    for color in state.colors:
        key = player_key(state, color)
        get = lambda field: state.player_state[f"{key}_{field}"]
        hand = {r: get(f"{r}_IN_HAND") for r in RESOURCES}
        held = {c: get(f"{c}_IN_HAND") for c in DEV_CARDS}
        played = {c: get(f"PLAYED_{c}") for c in DEV_CARDS}
        badges = []
        if get_longest_road_color(state) == color:
            badges.append(f"longest road ({get('LONGEST_ROAD_LENGTH')})")
        if get_largest_army(state)[0] == color:
            badges.append(f"largest army ({played['KNIGHT']})")
        rows.append(
            {
                "color": color.value,
                "model": seats.get(color, "?"),
                "vp": get_actual_victory_points(state, color),
                "hand": hand,
                "cards": sum(hand.values()),
                "held": held,
                "played": played,
                "dev": player_num_dev_cards(state, color),
                "towns": len(get_player_buildings(state, color, "SETTLEMENT")),
                "cities": len(get_player_buildings(state, color, "CITY")),
                "roads": len(get_player_buildings(state, color, "ROAD")),
                "road_len": get("LONGEST_ROAD_LENGTH"),
                "badges": ", ".join(badges),
            }
        )
    return sorted(rows, key=lambda r: -r["vp"])


def race_svg(rows, width=600):
    """The whole standing as one picture: four tokens on the shared race to 10."""
    tied = max(Counter(r["vp"] for r in rows).values(), default=1)
    height = 66 + 30 * tied  # tied seats stack upward; give them room
    left, right, base = 34, width - 34, height - 26
    x = lambda vp: left + (right - left) * min(vp, 10) / 10
    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">'
           "<title>race to 10 victory points</title>"]
    out.append(
        f'<line x1="{left}" y1="{base}" x2="{right}" y2="{base}" stroke="#3b3929" stroke-width="1.5"/>'
    )
    for vp in range(11):
        tall = vp % 5 == 0
        out.append(
            f'<line x1="{x(vp):.1f}" y1="{base - (7 if tall else 4)}" x2="{x(vp):.1f}" '
            f'y2="{base}" stroke="{"#6b6a52" if tall else "#33322a"}" stroke-width="1.5"/>'
        )
        if tall:
            out.append(
                f'<text x="{x(vp):.1f}" y="{base + 15}" text-anchor="middle" font-size="9.5" '
                'font-family="IBM Plex Mono,monospace" letter-spacing=".08em" '
                f'fill="{"#c9c079" if vp == 10 else "#6d6a5c"}">{vp}</text>'
            )
    stack = Counter()
    for rank, r in enumerate(rows):
        vp, cx = r["vp"], x(r["vp"])
        y = base - 14 - 30 * stack[vp]  # tied seats sit one above the other
        stack[vp] += 1
        fill = SEAT_FILL[r["color"]]
        if rank == 0:
            out.append(f'<circle cx="{cx:.1f}" cy="{y:.1f}" r="11" fill="{fill}" opacity=".22"/>')
        out.append(
            f'<circle cx="{cx:.1f}" cy="{y:.1f}" r="6.5" fill="{fill}" '
            'stroke="#100f0a" stroke-width="2"/>'
        )
        # label above the token, pinned inside the edges so names never collide
        anchor = "start" if cx < 62 else ("end" if cx > width - 62 else "middle")
        out.append(
            f'<text x="{cx:.1f}" y="{y - 13:.1f}" text-anchor="{anchor}" '
            'font-size="11.5" font-weight="600" letter-spacing="-.01em" '
            f'fill="{fill}">{short(r["model"])}</text>'
        )
    out.append("</svg>")
    return "".join(out)


def seat_html(rows, spent=None):
    """The race track, then one dense line per seat: hand, holdings, spend."""
    spent = spent or {}
    out = [f"<div class='race rise'>{race_svg(rows)}</div>"]
    for rank, r in enumerate(rows, 1):
        s = spent.get(r["color"])
        money = (
            f"<span class=spend>${s['cost']:.4f}</span>"
            f"<span class=gap>{s['moves']}<span class=sub> moves</span></span>"
            f"<span class=gap>{s['tokens'] / 1000:.1f}k<span class=sub> tok</span></span>"
            + (f"<span class='gap warn'>{s['retries']}<span class=sub> retries</span></span>" if s["retries"] else "")
            if s
            else ""
        )
        hand = "".join(
            f"<span class='rc{'' if r['hand'][res] else ' zero'}' style='--c:{TILE_FILL[res]}' "
            f"title='{res.lower()}'>{r['hand'][res]}</span>"
            for res in RESOURCES
        )
        cards = "".join(  # "knight 2 · 1 played" beats "knight 2 +1"
            f"<span class='dv{'' if r['held'][c] else ' spent'}'>{DEV_LABEL[c]}"
            + (
                f"<b>{r['held'][c]}</b>" + (f"<i>&middot; {r['played'][c]} played</i>" if r["played"][c] else "")
                if r["held"][c]
                else f"<b>{r['played'][c]}</b><i>played</i>"
            )
            + "</span>"
            for c in DEV_CARDS
            if r["held"][c] or r["played"][c]
        )
        danger = " danger" if r["cards"] >= 8 else ""  # robber discards at 8+
        out.append(
            f"<div class='srow rise' style='--seat:{SEAT_FILL[r['color']]}'>"
            f"<div class=line><span class=pos>{rank}</span>"
            f"<span class=sname>{short(r['model'])}</span>"
            f"<span class=hand>{hand}<b class='held{danger}'>{r['cards']}</b></span>"
            f"<span class=svp>{r['vp']}<i>vp</i></span></div>"
            + (f"<div class='line devs'>{cards}</div>" if cards else "")
            + f"<div class=facts><span class=sub>towns </span>{r['towns']}"
            f"<span class=gap><span class=sub>cities </span>{r['cities']}</span>"
            f"<span class=gap><span class=sub>roads </span>{r['roads']}</span>"
            + (f"<span class=gap><b>{html.escape(r['badges'])}</b></span>" if r["badges"] else "")
            + f"<span class=money>{money}</span></div></div>"
        )
    return "".join(out)


SCORING = {"BUILD_SETTLEMENT", "BUILD_CITY"}  # the only moves that move the VP line


def timeline_html(merged, limit=30):
    """Newest-first feed, grouped under the turn each decision belongs to."""
    out, turn = [], object()
    for d in merged[-limit:][::-1]:
        if d.get("turn") != turn:
            turn = d.get("turn")
            out.append(f"<div class=turnsep>turn {turn}</div>")
        out.append(entry_html(d))
    return "".join(out)


def commentary(match_name, limit=14):
    """Recent decisions with each model's stated reason, from the match log."""
    path = match_name + ".out"
    if not os.path.exists(path):
        return []
    with open(path, errors="replace") as f:
        lines = [ln.rstrip("\n") for ln in f if "] " in ln and ln[:2].isdigit()]
    return lines[-limit:][::-1]


REFRESH = '<meta http-equiv="refresh" content="5">'

FONTS = (
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    "family=Fraunces:ital,opsz,wght@0,9..144,500;0,9..144,600;1,9..144,400&"
    "family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600"
    '&display=swap">'
)

# A dark table under a lamp: felt and wood for the game, mono telemetry for the
# machines playing it. Single braces — CSS is substituted, not formatted.
CSS = """
:root{
--ink:#100f0a;--card:#1b1a12;--card2:#191810;--line:#2b2a20;--line2:#3b3929;
--text:#ece7d9;--dim:#94907e;--faint:#6d6a5c;--gold:#e8c96a;--win:#8fbf5a;--warn:#d9926a;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--text);
font:15px/1.6 "IBM Plex Sans",-apple-system,system-ui,sans-serif;
background-image:radial-gradient(120vh 78vh at 28% -12%,#26241a 0%,transparent 62%),
radial-gradient(88vh 58vh at 88% -4%,#1d1f17 0%,transparent 58%);
background-attachment:fixed}
body:before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.55;
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)' opacity='.04'/%3E%3C/svg%3E")}
a{color:var(--gold);text-decoration:none}
a:hover{text-decoration:underline}
h1{font:600 25px/1.15 Fraunces,Georgia,serif;letter-spacing:-.015em;margin:0}
h2{font:600 10px/1 "IBM Plex Sans",sans-serif;text-transform:uppercase;
letter-spacing:.16em;color:var(--faint);margin:0 0 11px}
.page{position:relative;z-index:1;max-width:1560px;margin:0 auto;padding:22px 26px 70px}
.muted,.sub{color:var(--dim);font-size:13px}
.sub{font-size:.82em;color:var(--faint);font-weight:400}
.num,.tele,.spend,.mono{font-family:"IBM Plex Mono",ui-monospace,monospace;
font-variant-numeric:tabular-nums}

.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:baseline;gap:16px;
flex-wrap:wrap;padding:13px 26px;border-bottom:1px solid var(--line);
background:rgba(16,15,10,.85);backdrop-filter:blur(14px)}
.topbar .home{font:500 10px "IBM Plex Sans";text-transform:uppercase;letter-spacing:.18em;
color:var(--faint)}
.topbar .meta{margin-left:auto;font-size:12px;color:var(--dim)}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 11px;border-radius:999px;
border:1px solid var(--line2);font:500 11px "IBM Plex Sans";letter-spacing:.06em;
text-transform:uppercase;color:var(--dim)}
.pill.on{color:var(--win);border-color:#3d5a2d}
.pill.on:before{content:"";width:6px;height:6px;border-radius:50%;background:var(--win);
animation:beat 2.4s ease-out infinite}
@keyframes beat{0%{box-shadow:0 0 0 0 rgba(143,191,90,.45)}70%{box-shadow:0 0 0 7px rgba(143,191,90,0)}100%{box-shadow:0 0 0 0 rgba(143,191,90,0)}}

.card{position:relative;overflow:hidden;border:1px solid var(--line);border-radius:14px;
background:linear-gradient(180deg,#1e1c14,var(--card2));
box-shadow:0 20px 44px -30px #000,inset 0 1px 0 rgba(255,255,255,.045);padding:15px 17px}
.wrap{display:grid;grid-template-columns:minmax(450px,1.03fr) minmax(410px,.97fr);
gap:26px;align-items:start;margin-top:18px}
@media (max-width:1120px){.wrap{grid-template-columns:1fr}}
.stack{display:grid;gap:24px}
.seatgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(275px,1fr));gap:12px}

.boardwrap{padding:6px 4px 2px}
.boardwrap:before{content:"";position:absolute;left:50%;top:46%;width:76%;height:66%;
transform:translate(-50%,-50%);pointer-events:none;filter:blur(26px);
background:radial-gradient(closest-side,rgba(232,201,106,.11),transparent 72%)}
.lastring{animation:ring 2s ease-out infinite}
@keyframes ring{0%{opacity:.95}70%{opacity:.15}100%{opacity:.95}}

.race{margin:-4px 0 10px}
.srow{--seat:#888;position:relative;padding:11px 0 11px 13px;border-top:1px solid var(--line)}
.srow:before{content:"";position:absolute;left:0;top:11px;bottom:11px;width:2px;background:var(--seat)}
.srow:last-child{border-bottom:1px solid var(--line)}
.line{display:flex;align-items:center;gap:10px}
.pos{font:600 11px Fraunces,serif;color:var(--faint);width:9px}
.sname{font-weight:600;letter-spacing:-.01em;font-size:14.5px}
.svp{margin-left:auto;font:600 21px/1 Fraunces,Georgia,serif;color:var(--gold)}
.svp i{font:500 9px "IBM Plex Sans";font-style:normal;color:var(--faint);
margin-left:3px;letter-spacing:.12em;text-transform:uppercase}
/* the hand as five little resource cards, not five pills */
.hand{display:inline-flex;align-items:center;gap:3px;margin-left:4px}
.rc{position:relative;width:19px;height:25px;border-radius:3px;background:var(--c);
display:flex;align-items:flex-end;justify-content:center;padding-bottom:2px;
font:600 10px "IBM Plex Mono",monospace;color:#14140d;
box-shadow:inset 0 -2px 0 rgba(0,0,0,.22),0 1px 2px rgba(0,0,0,.4)}
.rc:before{content:"";position:absolute;top:0;left:0;right:0;height:8px;
border-radius:3px 3px 0 0;background:rgba(255,255,255,.2)}
.rc.zero{opacity:.22}
.held{margin-left:6px;font:500 11px "IBM Plex Mono",monospace;color:var(--dim)}
.held.danger{color:#f0b9a0}
.devs{gap:5px;flex-wrap:wrap;margin-top:8px}
.dv{font:500 10.5px/1.6 "IBM Plex Mono",monospace;padding:1px 7px;border-radius:5px;
background:#241f14;color:var(--gold);border:1px solid #453a22}
.dv.spent{color:var(--faint);border-color:var(--line);background:transparent}
.dv b{font-weight:600;margin-left:5px}
.dv i{font-style:normal;opacity:.6;margin-left:4px}
.facts{font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--dim);
letter-spacing:.01em;margin-top:7px;display:flex;flex-wrap:wrap;align-items:baseline}
.facts b{color:var(--gold);font-weight:500}
.facts .sub{margin-right:4px}
.money{margin-left:auto;padding-left:12px}
.spend{color:var(--win)}
.gap{margin-left:11px}
.facts .warn{color:var(--warn)}

.feed{position:relative;max-height:640px;overflow-y:auto;padding:2px 6px 2px 21px;
scrollbar-width:thin;scrollbar-color:#33322a transparent}
.feed:before{content:"";position:absolute;left:4px;top:6px;bottom:6px;width:1px;background:var(--line)}
.turnsep{display:flex;align-items:center;gap:10px;margin:17px 0 9px;
font:500 10px "IBM Plex Mono",monospace;letter-spacing:.2em;text-transform:uppercase;color:var(--faint)}
.turnsep:after{content:"";flex:1;height:1px;background:var(--line)}
.turnsep:first-child{margin-top:2px}
.entry{position:relative;margin:0 0 15px;--seat:#888}
.entry:before{content:"";position:absolute;left:-21px;top:6px;width:8px;height:8px;
border-radius:50%;background:var(--seat);box-shadow:0 0 0 3px var(--ink)}
/* a scored point gets a diamond: the shape says "victory point", the fill says who */
.entry.key:before{width:9px;height:9px;left:-21.5px;border-radius:1px;
transform:rotate(45deg);outline:1.5px solid var(--gold);outline-offset:1.5px}
.act{font-weight:600;font-size:14.5px;letter-spacing:-.005em}
.entry.key .act{color:var(--gold)}
.entry.waiting .act{color:var(--warn)}
.tele{font:400 11px "IBM Plex Mono",monospace;color:var(--faint);letter-spacing:.02em;margin-top:1px}
.why{font:400 14px/1.5 Fraunces,Georgia,serif;font-style:italic;color:#b6b1a0;margin-top:4px}
.retry{display:inline-block;font:500 10px "IBM Plex Mono",monospace;color:var(--warn);
background:#2c1e16;border:1px solid #5a3626;border-radius:999px;padding:1px 8px;
margin-left:8px;vertical-align:1px;cursor:help;text-transform:none;letter-spacing:.04em}

.controls{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-top:16px}
.controls input[type=range]{flex:1;min-width:190px;accent-color:var(--gold);height:3px}
.btn{background:#211f16;color:var(--text);border:1px solid var(--line2);border-radius:8px;
padding:5px 12px;font:500 12px "IBM Plex Sans";cursor:pointer;transition:.14s ease}
.btn:hover{background:#2c2919;border-color:#4f4b36;transform:translateY(-1px)}
.btn:active{transform:none}
.counter{font:500 12px "IBM Plex Mono",monospace;color:var(--dim)}
.counter b{color:var(--text)}
.winner{display:flex;align-items:baseline;gap:10px;margin-top:16px;
font:600 19px Fraunces,Georgia,serif;color:var(--gold)}
.winner .sub{font:500 10px "IBM Plex Sans";letter-spacing:.16em;text-transform:uppercase}
.legend{display:flex;flex-wrap:wrap;gap:15px;margin:8px 2px 0;
font:400 11px "IBM Plex Mono",monospace;color:var(--dim)}
.lg b{color:var(--text);font-weight:500;margin-left:3px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}

table{border-collapse:collapse;width:100%}
td,th{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);font-size:13.5px}
th{font:500 10px "IBM Plex Sans";text-transform:uppercase;letter-spacing:.14em;color:var(--faint)}
tbody tr:hover td,table tr:hover td{background:#1c1b13}

/* first paint only — a live frame swaps innerHTML, and re-animating that flickers */
@keyframes rise{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:none}}
.fresh .rise{animation:rise .45s cubic-bezier(.2,.7,.3,1) both}
.fresh #seats>*:nth-child(2){animation-delay:.04s}
.fresh #seats>*:nth-child(3){animation-delay:.08s}
.fresh #seats>*:nth-child(4){animation-delay:.12s}
.fresh #seats>*:nth-child(5){animation-delay:.16s}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

def page(title, body, refresh=""):
    """Wrap a body in the shell. Not str.format: the CSS is full of braces."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"{refresh}<title>{html.escape(title)}</title>{FONTS}"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )


def index_page():
    rows = []
    for pkl in sorted(glob.glob("*_g*.pkl")):
        base = pkl[:-4]
        match_name = base.rsplit("_g", 1)[0]
        try:
            game, specs, n = load(base)
        except Exception as exc:
            rows.append(f"<tr><td>{html.escape(base)}</td><td colspan=4 class=muted>loading ({exc})</td></tr>")
            continue
        winner = game.winning_color()
        seats = dict(zip(COLORS, specs))
        lead = seat_rows(game, specs)[0]
        status = (
            f"<b>winner: {short(seats.get(winner, winner.value))}</b>"
            if winner
            else f"leading: {short(lead['model'])} ({lead['vp']} vp)"
        )
        rows.append(
            f"<tr><td><a href='/game?base={html.escape(base)}'>{html.escape(base)}</a></td>"
            f"<td class=mono>{game.state.num_turns}</td><td class=mono>{n}</td>"
            f"<td class=sub>{', '.join(short(s) for s in specs)}</td><td>{status}</td></tr>"
        )
    board = leaderboard()
    lb = "".join(
        f"<tr><td><b>{short(r['model'])}</b></td><td class=mono><b>{r['wins']}</b></td>"
        f"<td class=mono>{r['games']}</td><td class='mono sub'>{r['moves']}</td>"
        f"<td class='mono sub'>{r['retries']}</td><td class='mono sub'>{r['median_s']:.0f}s</td>"
        f"<td class='mono spend'>${r['cost']:.3f}</td></tr>"
        for r in board
    )
    lb_table = (
        "<div class='card rise'><h2>models</h2><table><tr><th>model</th><th>wins</th>"
        "<th>games</th><th>moves</th><th>retries</th><th>median move</th><th>spend</th></tr>"
        f"{lb}</table></div>"
        if board
        else ""
    )
    body = (
        "<div class=topbar><h1>Catan LLM arena</h1>"
        "<span class='meta mono'>every match on disk, replayed from its move log"
        " &middot; refreshes every 5s</span></div>"
        f"<div class='page fresh'><div class=stack>{lb_table}"
        "<div class='card rise'><h2>matches</h2><table>"
        "<tr><th>game</th><th>turn</th><th>moves</th><th>seats</th><th>status</th></tr>"
        + (
            "".join(rows)
            or "<tr><td colspan=5 class=sub>no matches yet &mdash; run ./run_match.sh</td></tr>"
        )
        + "</table></div></div></div>"
    )
    return page("Catan LLM arena", body, REFRESH)


def action_times(base):
    """Wall-clock time of each recorded action, for real-time playback."""
    return [row["t"] for row in read_jsonl(base + ".jsonl") if "i" in row]


def seats_of(base):
    """The lineup a game was played with, from its move log's header line."""
    try:
        with open(base + ".jsonl", errors="replace") as f:
            header = json.loads(f.readline())
    except (OSError, ValueError):
        return []
    return [s.removeprefix("llm:") for s in header.get("players", []) if s.startswith("llm:")]


def leaderboard():
    """Aggregate every finished game and every logged decision, by model."""
    wins, games = Counter(), Counter()
    for path in sorted(glob.glob("*_results.jsonl")):
        match_name = path.removesuffix("_results.jsonl")
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                # wins and games must come from the same place, or a model can
                # end up with more wins than games played
                for model in seats_of(f"{match_name}_g{row.get('game')}"):
                    games[model] += 1
                winner = row.get("winner", "")
                for spec in set(re.findall(r"llm:[\w./-]+", winner)):
                    wins[spec.removeprefix("llm:")] += 1
    stats = {}
    for path in sorted(glob.glob("*_decisions.jsonl")):
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                s = stats.setdefault(
                    row.get("model", "?"), {"moves": 0, "retries": 0, "seconds": [], "cost": 0.0}
                )
                if row.get("type") == "retry":
                    s["retries"] += 1
                    continue
                s["moves"] += 1
                s["seconds"].append(row.get("seconds", 0))
                if "cost_total" in row:  # per-move delta; older logs logged the running total
                    s["cost"] += row.get("cost_usd") or 0
                else:
                    s["cost"] = max(s["cost"], row.get("cost_usd") or 0)
    rows = []
    for model in stats.keys() | games.keys() | wins.keys():
        s = stats.get(model, {"moves": 0, "retries": 0, "seconds": [], "cost": 0.0})
        rows.append(
            {
                "model": model,
                "wins": wins.get(model, 0),
                "games": games.get(model, 0),
                "moves": s["moves"],
                "retries": s["retries"],
                "median_s": round(statistics.median(s["seconds"]), 1) if s["seconds"] else 0,
                "cost": s["cost"],
            }
        )
    return sorted(rows, key=lambda r: (-r["wins"], r["median_s"]))


RESOURCE_RE = re.compile(r"'([A-Z_]+)'")


def pretty_action(text):
    """Turn `MARITIME_TRADE ('ORE','ORE','ORE',None,'WHEAT')` into `trade 3x ore -> wheat`."""
    kind, _, rest = str(text).partition(" ")
    rest = rest.strip()
    if kind == "MARITIME_TRADE":
        items = RESOURCE_RE.findall(rest)
        if items:
            *give, get = items
            counts = Counter(give)
            traded = ", ".join(f"{n}&times; {r.lower()}" for r, n in counts.items())
            return f"trade {traded} &rarr; {get.lower()}"
    if kind == "MOVE_ROBBER":
        victim = re.search(r"Color\.([A-Z]+)", rest)
        return "move robber" + (f" &rarr; rob {victim.group(1).lower()}" if victim else "")
    if kind == "BUILD_ROAD":
        nodes = re.findall(r"\d+", rest)
        return f"build road {'&ndash;'.join(nodes[:2])}" if nodes else "build road"
    if kind == "BUILD_SETTLEMENT":
        return f"build settlement {rest}"
    if kind == "BUILD_CITY":
        return f"upgrade to city {rest}"
    if kind == "PLAY_YEAR_OF_PLENTY":
        return "year of plenty: " + ", ".join(r.lower() for r in RESOURCE_RE.findall(rest))
    if kind == "PLAY_MONOPOLY":
        got = RESOURCE_RE.findall(rest)
        return "monopoly: " + (got[0].lower() if got else rest.lower())
    simple = {
        "BUY_DEVELOPMENT_CARD": "buy development card",
        "PLAY_KNIGHT_CARD": "play knight",
        "PLAY_ROAD_BUILDING": "play road building",
        "END_TURN": "end turn",
        "ROLL": "roll",
        "DISCARD": "discard",
    }
    return simple.get(kind, html.escape(str(text)).lower())


def merge_retries(rows):
    """A retry is part of the decision it delayed, not an event of its own."""
    pending, merged = {}, []
    for row in rows:
        model = row.get("model")
        if row.get("type") == "retry":
            pending.setdefault(model, []).append(row)
            continue
        merged.append({**row, "waits": pending.pop(model, [])})
    for model, waits in pending.items():  # still retrying: no move yet
        merged.append({**waits[-1], "type": "waiting", "waits": waits})
    return merged


def entry_html(d):
    """One decision in the feed: what the model did, why, and what it cost."""
    seat = SEAT_FILL.get(d.get("color"), "#888")
    model = short(d.get("model", "?"))
    waits = d.get("waits") or []
    badge = ""
    if waits:
        gave_up = ", ".join(f"{w.get('gave_up_after_s', '?')}s" for w in waits)
        badge = (
            f"<span class=retry title='{html.escape(str(waits[-1].get('error', ''))[:150])}'>"
            f"&#8635; {len(waits)} retr{'y' if len(waits) == 1 else 'ies'} &middot; {gave_up}</span>"
        )
    if d.get("type") == "waiting":
        return (
            f"<div class='entry waiting rise' style='--seat:{seat}'>"
            f"<div class=act>waiting on {model} {badge}</div>"
            "<div class=tele>no answer yet &middot; still retrying</div></div>"
        )
    action = str(d.get("action", "?"))
    key = " key" if action.split(" ")[0] in SCORING else ""
    cost = d.get("cost_usd")
    cost = f" &middot; ${cost:.4f}" if isinstance(cost, (int, float)) and "cost_total" in d else ""
    return (
        f"<div class='entry{key} rise' style='--seat:{seat}'>"
        f"<div class=act>{pretty_action(action)}{badge}</div>"
        f"<div class=tele>{model} &middot; {d.get('seconds', '?')}s{cost}"
        f" &middot; {d.get('chose', '?')} of {d.get('options', '?')} options</div>"
        + (f"<div class=why>{html.escape(str(d.get('reason', '')))}</div>" if d.get("reason") else "")
        + "</div>"
    )


def game_page(base, at=None):
    game, specs, n = load(base, upto=at)
    live = at is None or at >= n
    match_name, game_index = base.rsplit("_g", 1)[0], int(base.rsplit("_g", 1)[1])
    winner = game.winning_color()
    rowsd = decisions(match_name, game_index)
    rows = seat_html(seat_rows(game, specs), spend(rowsd, game.state.num_turns))

    shown = n if live else at
    played = game.state.actions[-1] if game.state.actions else None
    scrubber = (
        "<div class=controls>"
        "<button class=btn onclick=\"goto(cur-1)\">&larr;</button>"
        f"<input type=range min=0 max={n} value={shown} id=sc "
        "oninput=\"lab.textContent=this.value\" onchange=\"play(null);goto(+this.value)\">"
        "<button class=btn onclick=\"goto(cur+1)\">&rarr;</button>"
        "<button class=btn onclick=\"play(150)\">&#9654; play</button>"
        "<button class=btn onclick=\"play(18)\">&#9193; fast</button>"
        "<button class=btn onclick=\"play(0)\">&#9201; real time</button>"
        "<button class=btn onclick=\"play(null)\">&#9208; pause</button>"
        f"<span class=counter>move <b id=lab>{shown}</b> / <b id=tot>{n}</b></span>"
        f"<span class='pill{' on' if live else ''}' id=st>{'live' if live else 'paused'}</span>"
        "</div>"
        f"<script>const BASE={base!r};let cur={shown},N={n},timer=null,liveTimer=null;"
        "async function goto(i){const r=await fetch('/frame?base='+BASE+'&at='+Math.max(0,i));"
        "const d=await r.json();cur=d.at;N=d.n;sc.max=N;sc.value=cur;lab.textContent=cur;"
        "tot.textContent=N;board.innerHTML=d.svg;seats.innerHTML=d.seats;"
        "chart.innerHTML=d.chart;meta.textContent=d.meta;"
        "if(d.timeline)tl.innerHTML=d.timeline;return d;}"
        "setTimeout(()=>document.querySelector('.page').classList.remove('fresh'),1200);"
        "function setSt(t){st.textContent=t;st.className='pill'+(t==='live'?' on':'');}"
        "function halt(){clearTimeout(timer);clearInterval(timer);timer=null;"
        "clearInterval(liveTimer);liveTimer=null;}"
        "async function step(ms){if(cur>=N){halt();follow();return;}const d=await goto(cur+1);"
        "if(ms===0)timer=setTimeout(()=>step(0),Math.min(d.gap_ms||700,6000));}"
        # wall-clock playback: background tabs clamp timers to 1s, so jump to the
        # move the elapsed time calls for instead of trusting one tick per move
        "function play(ms){halt();if(ms===null){setSt('paused');return;}"
        "if(cur>=N)cur=0;setSt(ms===0?'real time':(ms<500?'fast':'playing'));"
        "if(ms===0){step(0);return;}const start=Date.now(),from=cur;let busy=false;"
        "timer=setInterval(async()=>{if(busy)return;busy=true;"
        "const want=Math.min(N,from+Math.floor((Date.now()-start)/ms));"
        "if(want>cur)await goto(want);if(cur>=N){halt();follow();}busy=false;},"
        "Math.min(ms,120));}"
        "function follow(){halt();setSt('live');liveTimer=setInterval(()=>goto(1e9),5000);}"
        f"{'follow();' if live else ''}</script>"
    )

    upto_turn = game.state.num_turns  # scrubbed back: don't show decisions from the future
    timeline = timeline_html(
        [d for d in merge_retries(rowsd) if (d.get("turn") or 0) <= upto_turn]
    ) or "".join(
        f"<div class=entry><div class=tele>{html.escape(line)}</div></div>"
        for line in commentary(match_name)
    )

    labels = {c.value: spec for c, spec in zip(COLORS, specs)}
    turns, series = vp_history(base, upto=shown)
    banner = (
        f"<div class=winner><span class=sub>winner</span>"
        f"{short(dict(zip(COLORS, specs)).get(winner, winner.value))}"
        f"<span class=sub>in {game.state.num_turns} turns</span></div>"
        if winner
        else ""
    )
    body = (
        "<div class=topbar><span class=home><a href='/'>&larr; arena</a></span>"
        f"<h1>{html.escape(match_name)} <span class=sub>game {game_index}</span></h1>"
        f"<span class='meta mono' id=meta>turn {game.state.num_turns} &middot; "
        f"{n} moves recorded</span></div>"
        "<div class='page fresh'>"
        f"{banner}{scrubber}"
        "<div class=wrap>"
        "<div class=stack>"
        f"<div class='card boardwrap'><div id=board>{board_svg(game, played)}</div></div>"
        f"<div class=card><h2>victory points</h2><div id=chart>"
        f"{vp_chart(turns, series, labels)}</div></div></div>"
        f"<div class=stack><div><h2>race to 10</h2>"
        f"<div id=seats>{rows}</div></div>"
        "<div><h2>decisions &amp; reasoning</h2>"
        f"<div class=feed id=tl>{timeline or '<div class=tele>no decisions logged yet</div>'}</div>"
        "</div></div></div></div>"
    )
    return page(base, body)  # JS follows live, no meta refresh


def frame(base, at):
    """One playback frame: board + seats, and how long the real move took."""
    game, specs, n = load(base, upto=at)
    match_name, game_index = base.rsplit("_g", 1)[0], int(base.rsplit("_g", 1)[1])
    times = action_times(base)
    gap = None
    if 0 < at < len(times) and times[at] and times[at - 1]:
        gap = (times[at] - times[at - 1]) * 1000
    rowsd = decisions(match_name, game_index)
    spent = spend(rowsd, game.state.num_turns)
    rows = seat_html(seat_rows(game, specs), spent)
    action = game.state.actions[-1] if game.state.actions else None
    at = min(at, n)
    labels = {c.value: spec for c, spec in zip(COLORS, specs)}
    turns, series = vp_history(base, upto=at)
    total = sum(s["cost"] for s in spent.values())
    shown = [d for d in merge_retries(rowsd) if (d.get("turn") or 0) <= game.state.num_turns]
    return {
        "svg": board_svg(game, action),
        "seats": rows,
        "chart": vp_chart(turns, series, labels),
        "timeline": timeline_html(shown),
        "gap_ms": gap,
        "at": at,
        "n": n,
        "meta": f"turn {game.state.num_turns} · move {at} / {n} · ${total:.4f} spent"
        + (
            f" · {short(labels.get(action.color.value, '?'))} {action.action_type.value.lower()}"
            if action
            else ""
        ),
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == "/frame":
                query = parse_qs(url.query)
                payload = json.dumps(frame(query["base"][0], int(query["at"][0]))).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if url.path == "/game":
                query = parse_qs(url.query)
                at = query.get("at")
                rendered = game_page(query["base"][0], at=int(at[0]) if at else None)
            else:
                rendered = index_page()
        except Exception as exc:  # snapshot mid-write, bad param: stay up
            detail = traceback.format_exc()
            print(detail, file=sys.stderr)
            rendered = page(
                "arena",
                "<div class=page><h1>hold on</h1>"
                f"<div class=tele>{type(exc).__name__}: {html.escape(str(exc))}</div>"
                f"<pre class=tele>{html.escape(detail[-900:])}</pre></div>",
                REFRESH,
            )
        data = rendered.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def selftest():
    """Hex math is right only if tiles sharing a node place it identically."""
    game = Game([RandomPlayer(c) for c in COLORS[:3]], seed=1)
    board_map = game.state.board.map
    seen = {}
    for coord, tile in board_map.land_tiles.items():
        x, _, z = coord
        cx, cy = SIZE * math.sqrt(3) * (x + z / 2), SIZE * 1.5 * z
        for ref, node_id in tile.nodes.items():
            angle = math.radians(ANGLES[ref.value])
            point = (cx + SIZE * math.cos(angle), cy - SIZE * math.sin(angle))
            if node_id in seen:
                assert math.dist(seen[node_id], point) < 0.01, f"node {node_id} placed twice"
            seen[node_id] = point
    assert len(seen) == 54, f"expected 54 nodes, got {len(seen)}"
    assert board_svg(game).startswith("<svg") and "polygon" in board_svg(game)
    print(f"selftest ok: {len(seen)} nodes, {len(board_map.land_tiles)} tiles, svg renders")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        raise SystemExit
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    port = int(args[0]) if args and args[0].isdigit() else 8765
    where = next((a for a in args if not a.isdigit()), None)
    if where:  # match files live wherever the batch wrote them
        try:
            os.chdir(where)
        except OSError as exc:
            raise SystemExit(f"can't watch {where!r}: {exc}\nrun the batch first, or pass its --dir")
    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:  # blank browser tab otherwise: nothing explains itself
        raise SystemExit(
            f"port {port} is not available ({exc}).\n"
            f"something else is already serving it — `lsof -nP -iTCP:{port} -sTCP:LISTEN`"
        )
    print(f"arena dashboard: http://localhost:{port}  (watching {os.getcwd()})", flush=True)
    server.serve_forever()

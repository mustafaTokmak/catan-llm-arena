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


def load(base, upto=None):
    """Replay the recorded actions — all of them, or the first `upto` (scrubbing)."""
    with open(base + ".pkl", "rb") as f:
        blob = pickle.load(f)
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


def vp_history(base, upto=None, samples=140):
    """Victory points per seat over time, up to the move being viewed."""
    with open(base + ".pkl", "rb") as f:
        blob = pickle.load(f)
    actions = blob["actions"]
    if upto is not None:
        actions = actions[:upto]  # scrubbing must not spoil the ending
    game = Game([RandomPlayer(c) for c in COLORS[: len(blob["specs"])]], seed=blob["seed"])
    step = max(1, len(actions) // samples)
    turns, series = [0], {c.value: [0] for c in game.state.colors}
    for i, action in enumerate(actions):
        game.execute(action, validate_action=False)
        if i % step == 0 or i == len(actions) - 1:
            turns.append(game.state.num_turns)
            for color in game.state.colors:
                series[color.value].append(get_actual_victory_points(game.state, color))
    return turns, series


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
    for vp in range(0, top + 1, 2):
        y, win = sy(vp), vp == 10
        out.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" '
            f'stroke="{"#6b6a52" if win else "#2a2a22"}"'
            + (' stroke-dasharray="5 4"' if win else "")
            + "/>"
        )
        out.append(
            f'<text x="{left - 7}" y="{y:.1f}" text-anchor="end" dominant-baseline="central" '
            f'font-size="10" fill="{"#c9c079" if win else "#8a877c"}">{vp}</text>'
        )
    for t in (0, turns[-1]):
        out.append(
            f'<text x="{sx(t):.1f}" y="{foot + 15:.1f}" text-anchor="middle" '
            f'font-size="10" fill="#8a877c">turn {t}</text>'
        )
    legend = []
    for slot, (color, values) in enumerate(series.items()):
        # nudge each line a hair apart so tied scores stay readable
        offset = (slot - (len(series) - 1) / 2) * 1.6
        points = " ".join(
            f"{sx(t):.1f},{sy(v) + offset:.1f}" for t, v in zip(turns, values)
        )
        out.append(
            f'<polyline points="{points}" fill="none" stroke="{SEAT_FILL[color]}" '
            'stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>'
        )
        ex, ey = sx(turns[-1]), sy(values[-1]) + offset
        out.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="3.4" fill="{SEAT_FILL[color]}"/>')
        out.append(
            f'<text x="{ex + 7:.1f}" y="{ey:.1f}" dominant-baseline="central" font-size="11" '
            f'font-weight="500" fill="{SEAT_FILL[color]}">{values[-1]}</text>'
        )
        name = short(labels.get(color, "?"))
        legend.append(
            f"<span class=lg><span class=dot style='background:{SEAT_FILL[color]}'></span>"
            f"{name} <b>{values[-1]}</b></span>"
        )
    out.append("</svg>")
    return "".join(out) + f"<div class=legend>{''.join(legend)}</div>"


def decisions(match_name, game_index):
    path = match_name + "_decisions.jsonl"
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("game") == game_index:
                rows.append(row)
    return rows


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
                f'<circle cx="{spot[0]:.1f}" cy="{spot[1]:.1f}" r="20" fill="none" '
                'stroke="#e8c96a" stroke-width="3" opacity="0.95"/>'
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


def seat_html(rows, spent=None):
    """Seat cards: resources as coloured chips, dev cards held vs played."""
    out = []
    spent = spent or {}
    for r in rows:
        s = spent.get(r["color"])
        money = (
            f"<span class=spend>${s['cost']:.4f}</span> over {s['moves']} moves"
            f" &middot; {s['tokens'] / 1000:.1f}k tokens"
            + (f" &middot; {s['retries']} retries" if s["retries"] else "")
            if s
            else ""
        )
        chips = "".join(
            f"<span class=chip style='background:{TILE_FILL[res]}'>{res[:2].lower()} "
            f"<b>{r['hand'][res]}</b></span>"
            for res in RESOURCES
        )
        cards = "".join(
            f"<span class='chip dev{' spent' if not r['held'][c] else ''}'>{DEV_LABEL[c]}"
            f" <b>{r['held'][c]}</b>"
            + (f"<i> +{r['played'][c]} played</i>" if r["played"][c] else "")
            + "</span>"
            for c in DEV_CARDS
            if r["held"][c] or r["played"][c]
        ) or "<span class=muted>no development cards</span>"
        danger = " danger" if r["cards"] >= 8 else ""  # robber discards at 8+
        out.append(
            f"<div class=seat><div class=seathead>"
            f"<span class=dot style='background:{SEAT_FILL[r['color']]}'></span>"
            f"<b>{short(r['model'])}</b><span class=vp>{r['vp']} vp</span></div>"
            f"<div class=chips>{chips}"
            f"<span class='chip count{danger}'>{r['cards']} cards</span></div>"
            f"<div class=chips>{cards}</div>"
            f"<div class=muted>{r['towns']} settlements &middot; {r['cities']} cities &middot; "
            f"{r['roads']} roads (longest {r['road_len']})"
            + (f" &middot; <b>{html.escape(r['badges'])}</b>" if r["badges"] else "")
            + f"</div><div class=muted>{money}</div></div>"
        )
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

PAGE = """<!doctype html><html><head><meta charset="utf-8">{refresh}<title>{title}</title><style>
body{{background:#14140f;color:#e8e6df;font:15px/1.6 -apple-system,system-ui,sans-serif;margin:0;padding:24px}}
a{{color:#e8c96a}} h1{{font-size:20px;font-weight:500;margin:0 0 4px}} h2{{font-size:16px;font-weight:500;margin:24px 0 8px}}
.muted{{color:#9a978c;font-size:13px}} table{{border-collapse:collapse;width:100%;max-width:900px}}
td,th{{text-align:left;padding:6px 10px;border-bottom:1px solid #2e2e26;font-size:14px}} th{{color:#9a978c;font-weight:500}}
.page{{max-width:1500px;margin:0 auto}}
.wrap{{display:grid;grid-template-columns:minmax(440px,1.05fr) minmax(400px,1fr);
gap:30px;align-items:start}}
@media (max-width:1080px){{.wrap{{grid-template-columns:1fr}}}}
.seatgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:10px}}
.timeline{{max-height:560px;overflow-y:auto;padding-right:6px}}
.dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:7px;vertical-align:middle}}
.log{{font-size:13px;color:#c9c6bb;border-left:2px solid #2e2e26;padding-left:12px;margin:9px 0}}
.warn{{border-left-color:#a4472c;color:#d9a08c}}
.seat{{border:1px solid #2e2e26;border-radius:10px;padding:12px 14px;margin:10px 0}}
.seathead{{display:flex;align-items:center;gap:6px;font-size:14px}}
.vp{{margin-left:auto;font-weight:500;color:#e8c96a}}
.chips{{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}}
.chip{{font-size:12px;padding:2px 8px;border-radius:999px;color:#101008;white-space:nowrap}}
.chip b{{font-weight:500}} .chip i{{font-style:normal;opacity:.75}}
.chip.count{{background:#3a3a30;color:#e8e6df}}
.chip.count.danger{{background:#a4472c;color:#fff}}
.chip.dev{{background:#2e2e26;color:#e8c96a;border:1px solid #4a4a3e}}
.chip.dev.spent{{color:#9a978c}}
.scrub{{display:flex;align-items:center;gap:14px;margin:16px 0;max-width:900px}}
.scrub input{{flex:1;accent-color:#e8c96a}}
.scrub button{{background:#23231c;color:#e8e6df;border:1px solid #3c3c32;border-radius:6px;
padding:4px 10px;font-size:13px;cursor:pointer}}
.scrub button:hover{{background:#2e2e26}}
.live{{color:#7fae4a;font-size:13px}}
.legend{{display:flex;flex-wrap:wrap;gap:14px;margin:2px 0 6px;font-size:12px;color:#c9c6bb}}
.lg{{white-space:nowrap}} .lg b{{font-weight:500;color:#e8e6df}}
.retry{{font-size:11px;color:#d9a08c;background:#2e211c;border:1px solid #5c3527;
border-radius:999px;padding:1px 7px;margin-left:6px;white-space:nowrap;cursor:help}}
.spend{{font-variant-numeric:tabular-nums;color:#7fae4a}}
</style></head><body>{body}</body></html>"""


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
            f"<td>{game.state.num_turns}</td><td>{n}</td>"
            f"<td class=muted>{', '.join(short(s) for s in specs)}</td><td>{status}</td></tr>"
        )
    board = leaderboard()
    lb = "".join(
        f"<tr><td>{html.escape(r['model'])}</td><td><b>{r['wins']}</b></td><td>{r['games']}</td>"
        f"<td class=muted>{r['moves']}</td><td class=muted>{r['retries']}</td>"
        f"<td class=muted>{r['median_s']:.0f}s</td><td class=muted>${r['cost']:.3f}</td></tr>"
        for r in board
    )
    lb_table = (
        "<h2>models</h2><table><tr><th>model</th><th>wins</th><th>games</th><th>moves</th>"
        f"<th>retries</th><th>median move</th><th>spend</th></tr>{lb}</table>"
        if board
        else ""
    )
    body = (
        "<div class=page><h1>Catan LLM arena</h1>"
        "<div class=muted>every match on disk, replayed from its move log"
        f" &middot; refreshes every 5s</div>{lb_table}<h2>matches</h2><table>"
        "<tr><th>game</th><th>turn</th><th>actions</th><th>seats</th><th>status</th></tr>"
        + ("".join(rows) or "<tr><td colspan=5 class=muted>no matches yet — run ./run_match.sh</td></tr>")
        + "</table></div>"
    )
    return PAGE.format(title="Catan LLM arena", body=body, refresh=REFRESH)


def action_times(base):
    """Wall-clock time of each recorded action, for real-time playback."""
    times = []
    path = base + ".jsonl"
    if not os.path.exists(path):
        return times
    with open(path, errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if "i" in row:
                times.append(row.get("t"))
    return times


def last_action(base, upto):
    with open(base + ".pkl", "rb") as f:
        actions = pickle.load(f)["actions"]
    if upto <= 0 or not actions:  # rewound to the empty board: nothing to ring
        return None
    return actions[min(upto, len(actions)) - 1]


def leaderboard():
    """Aggregate every finished game and every logged decision, by model."""
    wins, games = Counter(), Counter()
    for path in sorted(glob.glob("*_results.jsonl")):
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
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
                games[(row.get("model"), path, row.get("game"))] = 1
    rows = []
    for model, s in stats.items():
        played = sum(1 for key in games if key[0] == model)
        rows.append(
            {
                "model": model,
                "wins": wins.get(model, 0),
                "games": played,
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
    """One timeline row: the move a model made, with any retries folded in."""
    dot = f"<span class=dot style='background:{SEAT_FILL.get(d.get('color'), '#888')}'></span>"
    model = short(d.get("model", "?"))
    waits = d.get("waits") or []
    badge = ""
    if waits:
        gave_up = ", ".join(f"{w.get('gave_up_after_s', '?')}s" for w in waits)
        badge = (
            f"<span class=retry title='{html.escape(str(waits[-1].get('error', ''))[:150])}'>"
            f"&#8635; {len(waits)} retr{'y' if len(waits) == 1 else 'ies'} ({gave_up})</span>"
        )
    if d.get("type") == "waiting":
        return (
            f"<div class='log warn'>{dot}<b>waiting for {model}</b> {badge}<br>"
            f"<span class=muted>turn {d.get('turn', '?')} &middot; still retrying</span></div>"
        )
    return (
        f"<div class=log>{dot}<b>{pretty_action(d.get('action', '?'))}</b> {badge}"
        f"<div class=muted>turn {d.get('turn', '?')} &middot; {model} &middot; "
        f"{d.get('seconds', '?')}s &middot; picked {d.get('chose', '?')} of "
        f"{d.get('options', '?')} options</div>"
        f"{html.escape(str(d.get('reason', '')))}</div>"
    )


def game_page(base, at=None):
    game, specs, n = load(base, upto=at)
    live = at is None or at >= n
    match_name, game_index = base.rsplit("_g", 1)[0], int(base.rsplit("_g", 1)[1])
    winner = game.winning_color()
    rowsd = decisions(match_name, game_index)
    rows = seat_html(seat_rows(game, specs), spend(rowsd, game.state.num_turns))

    shown = n if live else at
    prev_at, next_at = max(0, shown - 1), min(n, shown + 1)
    scrubber = (
        "<div class=scrub>"
        "<button onclick=\"goto(cur-1)\">&larr;</button>"
        f"<input type=range min=0 max={n} value={shown} id=sc "
        "oninput=\"lab.textContent=this.value\" onchange=\"play(null);goto(+this.value)\">"
        "<button onclick=\"goto(cur+1)\">&rarr;</button>"
        "<button onclick=\"play(900)\">&#9654; play</button>"
        "<button onclick=\"play(220)\">&#9193; fast</button>"
        "<button onclick=\"play(0)\">&#9201; real time</button>"
        "<button onclick=\"play(null)\">&#9208; pause</button>"
        f"<span class=muted>move <b id=lab>{shown}</b> / <b id=tot>{n}</b></span>"
        f"<span class=live id=st>{'live' if live else 'paused'}</span>"
        "</div>"
        f"<script>const BASE={base!r};let cur={shown},N={n},timer=null,liveTimer=null;"
        "async function goto(i){const r=await fetch('/frame?base='+BASE+'&at='+Math.max(0,i));"
        "const d=await r.json();cur=d.at;N=d.n;sc.max=N;sc.value=cur;lab.textContent=cur;"
        "tot.textContent=N;board.innerHTML=d.svg;seats.innerHTML=d.seats;"
        "chart.innerHTML=d.chart;meta.textContent=d.meta;"
        "if(d.timeline)tl.innerHTML=d.timeline;return d;}"
        "function halt(){clearTimeout(timer);clearInterval(timer);timer=null;"
        "clearInterval(liveTimer);liveTimer=null;}"
        "async function step(ms){if(cur>=N){halt();follow();return;}const d=await goto(cur+1);"
        "if(ms===0)timer=setTimeout(()=>step(0),Math.min(d.gap_ms||700,6000));}"
        # wall-clock playback: background tabs clamp timers to 1s, so jump to the
        # move the elapsed time calls for instead of trusting one tick per move
        "function play(ms){halt();if(ms===null){st.textContent='paused';return;}"
        "if(cur>=N)cur=0;st.textContent=ms===0?'real time':(ms<500?'fast':'playing');"
        "if(ms===0){step(0);return;}const start=Date.now(),from=cur;let busy=false;"
        "timer=setInterval(async()=>{if(busy)return;busy=true;"
        "const want=Math.min(N,from+Math.floor((Date.now()-start)/ms));"
        "if(want>cur)await goto(want);if(cur>=N){halt();follow();}busy=false;},"
        "Math.min(ms,120));}"
        "function follow(){halt();st.textContent='live';liveTimer=setInterval(()=>goto(1e9),5000);}"
        f"{'follow();' if live else ''}</script>"
    )

    timeline = "".join(
        entry_html(d) for d in merge_retries(rowsd)[-25:][::-1]
    ) or "".join(f"<div class=log>{html.escape(line)}</div>" for line in commentary(match_name))

    labels = {c.value: spec for c, spec in zip(COLORS, specs)}
    turns, series = vp_history(base, upto=shown)
    banner = (
        f"<h2>winner: {short(dict(zip(COLORS, specs)).get(winner, winner.value))}</h2>"
        if winner
        else ""
    )
    body = (
        "<div class=page>"
        f"<h1><a href='/'>arena</a> / {html.escape(base)}</h1>"
        f"<div class=muted id=meta>turn {game.state.num_turns} &middot; {n} actions recorded</div>{banner}"
        f"{scrubber}"
        "<div class=wrap>"
        f"<div><div id=board>{board_svg(game, last_action(base, shown))}</div>"
        f"<h2>victory points</h2><div id=chart>{vp_chart(turns, series, labels)}</div></div>"
        f"<div><h2>seats</h2><div id=seats class=seatgrid>{rows}</div>"
        "<h2>decisions</h2>"
        f"<div class=timeline id=tl>{timeline or '<div class=muted>no decisions logged yet</div>'}</div>"
        "</div></div></div>"
    )
    return PAGE.format(title=base, body=body, refresh="")  # JS follows live, no reload


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
    action = last_action(base, at)
    at = min(at, n)
    labels = {c.value: spec for c, spec in zip(COLORS, specs)}
    turns, series = vp_history(base, upto=at)
    total = sum(s["cost"] for s in spent.values())
    shown = [d for d in merge_retries(rowsd) if (d.get("turn") or 0) <= game.state.num_turns]
    return {
        "svg": board_svg(game, action),
        "seats": rows,
        "chart": vp_chart(turns, series, labels),
        "timeline": "".join(entry_html(d) for d in shown[-25:][::-1]),
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
                page = game_page(query["base"][0], at=int(at[0]) if at else None)
            else:
                page = index_page()
        except Exception as exc:  # snapshot mid-write, bad param: stay up
            detail = traceback.format_exc()
            print(detail, file=sys.stderr)
            page = PAGE.format(
                title="arena",
                body=(
                    f"<h1>hold on</h1><div class=muted>{type(exc).__name__}: {html.escape(str(exc))}"
                    f"</div><pre class=log>{html.escape(detail[-900:])}</pre>"
                ),
                refresh=REFRESH,
            )
        data = page.encode()
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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"arena dashboard: http://localhost:{port}")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()

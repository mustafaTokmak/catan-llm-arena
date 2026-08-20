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
import sys
import time
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


def load(base, upto=None):
    """Replay the recorded actions — all of them, or the first `upto` (scrubbing)."""
    with open(base + ".pkl", "rb") as f:
        blob = pickle.load(f)
    actions = blob["actions"]
    game = Game([RandomPlayer(c) for c in COLORS[: len(blob["specs"])]], seed=blob["seed"])
    for action in actions[:upto] if upto is not None else actions:
        game.execute(action, validate_action=False)
    return game, blob["specs"], len(actions)


def vp_history(base, samples=90):
    """Victory points per seat over the game, sampled for the progress chart."""
    with open(base + ".pkl", "rb") as f:
        blob = pickle.load(f)
    actions = blob["actions"]
    game = Game([RandomPlayer(c) for c in COLORS[: len(blob["specs"])]], seed=blob["seed"])
    step = max(1, len(actions) // samples)
    series = {c.value: [] for c in game.state.colors}
    marks = []
    for i, action in enumerate(actions):
        game.execute(action, validate_action=False)
        if i % step == 0 or i == len(actions) - 1:
            marks.append(i + 1)
            for color in game.state.colors:
                series[color.value].append(get_actual_victory_points(game.state, color))
    return marks, series


def vp_chart(marks, series, width=560, height=150):
    if len(marks) < 2:
        return "<div class=muted>not enough moves yet</div>"
    top = max(10, max(max(v) for v in series.values()))
    sx = lambda i: 34 + (width - 44) * marks[i] / marks[-1]
    sy = lambda v: height - 24 - (height - 40) * v / top
    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img"><title>victory points</title>']
    for vp in range(0, top + 1, 2):
        y = sy(vp)
        out.append(f'<line x1="34" y1="{y:.1f}" x2="{width - 10}" y2="{y:.1f}" stroke="#2e2e26"/>')
        out.append(f'<text x="26" y="{y:.1f}" text-anchor="end" dominant-baseline="central" font-size="10" fill="#9a978c">{vp}</text>')
    for color, values in series.items():
        points = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(values))
        out.append(
            f'<polyline points="{points}" fill="none" stroke="{SEAT_FILL[color]}" '
            'stroke-width="2.5" stroke-linejoin="round"/>'
        )
    out.append("</svg>")
    return "".join(out)


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


def board_svg(game):
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
    out.append("</svg>")
    return "".join(out)


def seat_rows(game, specs):
    state = game.state
    seats = dict(zip(COLORS, specs))
    rows = []
    for color in state.colors:
        key = player_key(state, color)
        hand = " ".join(
            f"{r[:2].lower()} {state.player_state[f'{key}_{r}_IN_HAND']}" for r in RESOURCES
        )
        badges = []
        if get_longest_road_color(state) == color:
            badges.append("longest road")
        if get_largest_army(state)[0] == color:
            badges.append("largest army")
        rows.append(
            {
                "color": color.value,
                "model": seats.get(color, "?"),
                "vp": get_actual_victory_points(state, color),
                "hand": hand,
                "dev": player_num_dev_cards(state, color),
                "towns": len(get_player_buildings(state, color, "SETTLEMENT")),
                "cities": len(get_player_buildings(state, color, "CITY")),
                "roads": len(get_player_buildings(state, color, "ROAD")),
                "badges": ", ".join(badges),
            }
        )
    return sorted(rows, key=lambda r: -r["vp"])


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
.wrap{{display:flex;gap:28px;flex-wrap:wrap}} .board{{flex:1 1 520px;max-width:640px}} .side{{flex:1 1 380px}}
.dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:7px;vertical-align:middle}}
.log{{font-size:13px;color:#c9c6bb;border-left:2px solid #2e2e26;padding-left:12px;margin:9px 0}}
.scrub{{display:flex;align-items:center;gap:14px;margin:16px 0;max-width:900px}}
.scrub input{{flex:1;accent-color:#e8c96a}}
.live{{color:#7fae4a;font-size:13px}}
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
        lead = seat_rows(game, specs)[0]
        status = (
            f"<b>winner: {winner.value}</b>" if winner else f"leading: {html.escape(lead['model'])} ({lead['vp']} vp)"
        )
        rows.append(
            f"<tr><td><a href='/game?base={html.escape(base)}'>{html.escape(base)}</a></td>"
            f"<td>{game.state.num_turns}</td><td>{n}</td>"
            f"<td class=muted>{html.escape(', '.join(specs))}</td><td>{status}</td></tr>"
        )
    body = (
        "<h1>Catan LLM arena</h1><div class=muted>every match on disk, replayed from its move log"
        " &middot; refreshes every 5s</div><h2>matches</h2><table>"
        "<tr><th>game</th><th>turn</th><th>actions</th><th>seats</th><th>status</th></tr>"
        + ("".join(rows) or "<tr><td colspan=5 class=muted>no matches yet — run ./run_match.sh</td></tr>")
        + "</table>"
    )
    return PAGE.format(title="Catan LLM arena", body=body, refresh=REFRESH)


def game_page(base, at=None):
    game, specs, n = load(base, upto=at)
    live = at is None or at >= n
    match_name, game_index = base.rsplit("_g", 1)[0], int(base.rsplit("_g", 1)[1])
    winner = game.winning_color()
    rows = "".join(
        f"<tr><td><span class=dot style='background:{SEAT_FILL[r['color']]}'></span>{html.escape(r['model'])}</td>"
        f"<td><b>{r['vp']}</b></td><td class=muted>{html.escape(r['hand'])}</td>"
        f"<td class=muted>{r['towns']}t {r['cities']}c {r['roads']}r dev{r['dev']}</td>"
        f"<td class=muted>{html.escape(r['badges'])}</td></tr>"
        for r in seat_rows(game, specs)
    )

    shown = n if live else at
    prev_at, next_at = max(0, shown - 1), min(n, shown + 1)
    scrubber = (
        f"<div class=scrub><a href='/game?base={base}&at={prev_at}'>&larr; prev</a>"
        f"<input type=range min=0 max={n} value={shown} id=sc "
        f"oninput=\"document.getElementById('lab').textContent=this.value\" "
        f"onchange=\"location='/game?base={base}&at='+this.value\">"
        f"<a href='/game?base={base}&at={next_at}'>next &rarr;</a>"
        f"<span class=muted>move <b id=lab>{shown}</b> / {n}</span>"
        + ("<span class=live>live</span>" if live else f"<a href='/game?base={base}'>back to live</a>")
        + "</div>"
    )

    rowsd = decisions(match_name, game_index)
    timeline = "".join(
        f"<div class=log><span class=dot style='background:{SEAT_FILL[d['color']]}'></span>"
        f"<b>{html.escape(d['action'])}</b> "
        f"<span class=muted>turn {d.get('turn', '?')} &middot; {d.get('seconds', '?')}s &middot; "
        f"${d.get('cost_usd', 0):.4f} &middot; {html.escape(d['model'].split('/')[-1])}</span><br>"
        f"{html.escape(d.get('reason', ''))}</div>"
        for d in rowsd[-25:][::-1]
    ) or "".join(f"<div class=log>{html.escape(line)}</div>" for line in commentary(match_name))

    marks, series = vp_history(base)
    banner = f"<h2>winner: {winner.value}</h2>" if winner else ""
    body = (
        f"<h1><a href='/'>arena</a> / {html.escape(base)}</h1>"
        f"<div class=muted>turn {game.state.num_turns} &middot; {n} actions recorded</div>{banner}"
        f"{scrubber}"
        f"<div class=wrap><div class=board>{board_svg(game)}</div>"
        f"<div class=side><h2>seats</h2><table>{rows}</table>"
        f"<h2>victory points over the game</h2>{vp_chart(marks, series)}"
        f"<h2>decisions</h2>{timeline or '<div class=muted>no decisions logged yet</div>'}</div></div>"
    )
    return PAGE.format(title=base, body=body, refresh=REFRESH if live else "")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == "/game":
                query = parse_qs(url.query)
                at = query.get("at")
                page = game_page(query["base"][0], at=int(at[0]) if at else None)
            else:
                page = index_page()
        except Exception as exc:  # snapshot mid-write, bad param: stay up
            page = PAGE.format(
                title="arena",
                body=f"<h1>hold on</h1><div class=muted>{html.escape(str(exc))}</div>",
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

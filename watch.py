"""Live terminal tracker: reconstructs exact game state from a match snapshot.

Usage:
  python watch.py match1_g0            # one snapshot
  python watch.py match1_g0 --follow   # refresh every 3s until the game ends

Reads <base>.pkl (written after every action by arena.py), replays the actions
through the engine (bit-exact), and renders the board and player states.
"""

import pickle
import sys
import time

from catanatron import Color, Game, RandomPlayer
from catanatron.state_functions import (
    get_actual_victory_points,
    get_largest_army,
    get_longest_road_color,
    get_player_buildings,
    player_num_dev_cards,
)
from llm_player import RESOURCES

COLORS = [Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE]


def load_game(base):
    with open(base + ".pkl", "rb") as f:
        blob = pickle.load(f)
    players = [RandomPlayer(c) for c in COLORS[: len(blob["specs"])]]
    game = Game(players, seed=blob["seed"])
    for action in blob["actions"]:
        game.execute(action, validate_action=False)
    return game, blob["specs"], len(blob["actions"])


def short(spec):
    """Seats read as model names, not colours."""
    return str(spec).removeprefix("llm:").split("/")[-1]


def render(game, specs, n_actions, base):
    state = game.state
    seats = {c: short(s) for c, s in zip(COLORS, specs)}
    lines = [f"=== {base} | turn {state.num_turns} | {n_actions} actions ==="]

    board = state.board
    by_number = {}
    for coord, tile in board.map.land_tiles.items():
        robber = " (ROBBER)" if board.robber_coordinate == coord else ""
        if tile.resource is None:
            by_number.setdefault("--", []).append(f"DESERT{robber}")
        else:
            by_number.setdefault(tile.number, []).append(f"{tile.resource}{robber}")
    lines.append("board tiles by dice number:")
    for num in sorted(by_number, key=str):
        lines.append(f"  {str(num):>2}: {', '.join(by_number[num])}")

    for color in state.colors:
        hand = " ".join(
            f"{r[:2]}:{state.player_state[f'P{state.color_to_index[color]}_{r}_IN_HAND']}"
            for r in RESOURCES
        )
        settlements = get_player_buildings(state, color, "SETTLEMENT")
        cities = get_player_buildings(state, color, "CITY")
        roads = get_player_buildings(state, color, "ROAD")
        badges = []
        if get_longest_road_color(state) == color:
            badges.append("LONGEST ROAD")
        if get_largest_army(state)[0] == color:
            badges.append("LARGEST ARMY")
        lines.append(
            f"{seats.get(color, '?'):<22} {color.value.lower():<7} "
            f"VP:{get_actual_victory_points(state, color):>2}  {hand}  "
            f"dev:{player_num_dev_cards(state, color)}  "
            f"towns:{sorted(settlements)} cities:{sorted(cities)} roads:{len(roads)}"
            + (f"  [{' + '.join(badges)}]" if badges else "")
        )

    recent = state.actions[-6:]
    lines.append("last moves:")
    for a in recent:
        value = "" if a.value is None else f" {a.value}"
        lines.append(f"  {seats.get(a.color, a.color.value)}: {a.action_type.value}{value}")

    winner = game.winning_color()
    if winner is not None:
        lines.append(f"*** WINNER: {seats.get(winner, winner.value)} ({winner.value.lower()}) ***")
    return "\n".join(lines), winner


def main():
    base = sys.argv[1].removesuffix(".pkl")
    follow = "--follow" in sys.argv
    while True:
        try:
            game, specs, n = load_game(base)
        except Exception as exc:  # snapshot mid-write: keep waiting
            print(f"({base}: snapshot not readable yet: {exc})")
            game = None
        if game is not None:
            text, winner = render(game, specs, n, base)
            if follow:
                print("\033[2J\033[H", end="")
            print(text, flush=True)
            if not follow or winner is not None:
                return
        time.sleep(3)


if __name__ == "__main__":
    main()

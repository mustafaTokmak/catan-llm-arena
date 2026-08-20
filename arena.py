"""Catan arena: run N games between named players, print standings.

Player specs (comma-separated, 2-4 seats):
  llm:<openrouter-slug> | random | weighted | vp

Examples (needs OPENROUTER_API_KEY for llm seats):
  python arena.py --games 10 --players llm:deepseek/deepseek-v4-flash,llm:tencent/hy3,llm:xiaomi/mimo-v2.5,llm:openai/gpt-5.6-luna
  python arena.py --games 50 --players weighted,random,random,vp   # free bot smoke test
"""

import argparse
from collections import Counter

from catanatron import Color, Game, RandomPlayer
from catanatron.players.search import VictoryPointPlayer
from catanatron.players.weighted_random import WeightedRandomPlayer

COLORS = [Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE]


def make_player(spec, color):
    kind, _, rest = spec.partition(":")
    if kind == "random":
        return RandomPlayer(color)
    if kind == "weighted":
        return WeightedRandomPlayer(color)
    if kind == "vp":
        return VictoryPointPlayer(color)
    if kind == "llm":
        from llm_player import LLMPlayer

        return LLMPlayer(color, model=rest) if rest else LLMPlayer(color)
    raise SystemExit(f"unknown player spec: {spec!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=int, default=10)
    parser.add_argument("--players", default="random,random,weighted,vp")
    args = parser.parse_args()

    specs = [s.strip() for s in args.players.split(",")]
    players = [make_player(spec, color) for spec, color in zip(specs, COLORS)]
    labels = {p.color: f"{spec} ({p.color.value})" for spec, p in zip(specs, players)}

    wins = Counter()
    turns = []
    for i in range(args.games):
        game = Game(players)  # seating/turn order is shuffled by the engine
        winner = game.play()
        wins[winner] += 1
        turns.append(game.state.num_turns)
        print(f"game {i + 1:>3}: {labels.get(winner, 'no winner (turn limit)'):<28} in {game.state.num_turns} turns")

    assert sum(wins.values()) == args.games
    print(f"\n=== standings after {args.games} games (avg {sum(turns) / len(turns):.0f} turns) ===")
    for color, n in wins.most_common():
        print(f"{labels.get(color, 'no winner (turn limit)'):<30} {n:>4} wins  ({100 * n / args.games:.0f}%)")
    for player in players:
        if hasattr(player, "api_calls"):
            print(
                f"[{labels[player.color]}] api_calls={player.api_calls} "
                f"fallbacks={player.fallbacks} "
                f"tokens={player.input_tokens}/{player.output_tokens} "
                f"cost=${player.cost_usd:.4f}"
            )


if __name__ == "__main__":
    main()

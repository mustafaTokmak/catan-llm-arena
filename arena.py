"""Catan arena: durable, resumable matches between named players.

Every executed action is appended to <log>_gN.jsonl (human-readable audit) and
snapshotted to <log>_gN.pkl (exact resume state). Kill the process at any
point and rerun with --resume: the engine replays the recorded actions against
the same seed (verified bit-exact) and play continues where it stopped.
Finished games append to <log>_results.jsonl so tournaments resume too.

Player specs (comma-separated, 2-4 seats):
  llm:<openrouter-slug> | random | weighted | vp

Examples (needs OPENROUTER_API_KEY in env or .env):
  python arena.py --games 1 --players llm:z-ai/glm-5.2,llm:deepseek/deepseek-v4-flash,llm:qwen/qwen3.7-flash --log match1 --resume
  python arena.py --games 50 --players weighted,random,random,vp   # free bot smoke test
"""

import argparse
import json
import logging
import os
import pickle
import random
import statistics
import sys
import time
from collections import Counter, defaultdict

from catanatron import Color, Game, RandomPlayer
from catanatron.players.search import VictoryPointPlayer
from catanatron.players.weighted_random import WeightedRandomPlayer

COLORS = [Color.RED, Color.BLUE, Color.WHITE, Color.ORANGE]
TURN_CAP = 1000
log = logging.getLogger("arena")


def model_stats(decisions_path):
    """Per-model summary from the decision log: speed, retries, spend."""
    moves, retries, seconds, tokens = (
        defaultdict(int),
        defaultdict(int),
        defaultdict(list),
        defaultdict(int),
    )
    cost = {}
    if not os.path.exists(decisions_path):
        return []
    with open(decisions_path, errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            model = row.get("model", "?")
            if row.get("type") == "retry":
                retries[model] += 1
                continue
            moves[model] += 1
            seconds[model].append(row.get("seconds", 0))
            tokens[model] += (row.get("tokens_in") or 0) + (row.get("tokens_out") or 0)
            cost[model] = row.get("cost_usd", cost.get(model, 0))
    return [
        {
            "model": model,
            "moves": moves[model],
            "retries": retries[model],
            "median_s": round(statistics.median(seconds[model]), 1),
            "slowest_s": round(max(seconds[model]), 1),
            "tokens": tokens[model],
            "cost": cost.get(model, 0),
        }
        for model in sorted(moves, key=lambda m: -moves[m])
    ]


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


def run_game(players, specs, base, resume, decisions_path=None, game_index=0):
    """Play one game to completion, persisting every action; resumable."""
    pkl, jsonl = base + ".pkl", base + ".jsonl"
    for player in players:
        if hasattr(player, "decisions_path"):
            player.decisions_path = decisions_path
            player.game_index = game_index
    if resume and os.path.exists(pkl):
        with open(pkl, "rb") as f:
            blob = pickle.load(f)
        if blob["specs"] != specs:
            raise SystemExit(
                f"{pkl} was recorded with players {blob['specs']} — "
                "resume with the same lineup or use a new --log name"
            )
        seed = blob["seed"]
        game = Game(players, seed=seed)
        for action in blob["actions"]:
            game.execute(action, validate_action=False)
        print(
            f"resumed {base} at action {len(blob['actions'])}, turn {game.state.num_turns}",
            file=sys.stderr,
            flush=True,
        )
    else:
        seed = random.randrange(2**31)
        game = Game(players, seed=seed)
        with open(jsonl, "w") as f:
            f.write(json.dumps({"seed": seed, "players": specs}) + "\n")

    seen = len(game.state.actions)
    with open(jsonl, "a") as audit:
        while game.winning_color() is None and game.state.num_turns < TURN_CAP:
            game.play_tick()
            for a in game.state.actions[seen:]:
                audit.write(
                    json.dumps(
                        {
                            "i": seen,
                            "t": time.time(),
                            "color": a.color.value,
                            "type": a.action_type.value,
                            "value": repr(a.value),
                        }
                    )
                    + "\n"
                )
                seen += 1
            audit.flush()
            with open(pkl, "wb") as f:
                pickle.dump(
                    {"seed": seed, "specs": specs, "actions": game.state.actions},
                    f,
                )
    return game


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=int, default=1)
    parser.add_argument("--players", default="random,random,weighted,vp")
    parser.add_argument("--log", default="match")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    specs = [s.strip() for s in args.players.split(",")]
    players = [make_player(spec, color) for spec, color in zip(specs, COLORS)]
    labels = {p.color: f"{spec} ({p.color.value})" for spec, p in zip(specs, players)}
    results_path = args.log + "_results.jsonl"
    decisions_path = args.log + "_decisions.jsonl"
    log.info("match %s: %d game(s), seats %s", args.log, args.games, ", ".join(specs))

    results = []
    if args.resume and os.path.exists(results_path):
        with open(results_path) as f:
            results = [json.loads(line) for line in f]

    for i in range(len(results), args.games):
        try:
            game = run_game(
                players,
                specs,
                f"{args.log}_g{i}",
                args.resume,
                decisions_path=decisions_path,
                game_index=i,
            )
        except Exception as exc:
            if type(exc).__name__ != "FatalSetupError":
                raise
            print(f"fatal: {exc}", file=sys.stderr)
            raise SystemExit(3)  # supervisor stops instead of looping
        winner = game.winning_color()
        record = {
            "game": i,
            "winner": labels.get(winner, "no winner (turn cap)"),
            "turns": game.state.num_turns,
        }
        results.append(record)
        with open(results_path, "a") as f:
            f.write(json.dumps(record) + "\n")
        log.info("game %d finished: %s in %d turns", i + 1, record["winner"], record["turns"])

    wins = Counter(r["winner"] for r in results)
    avg_turns = sum(r["turns"] for r in results) / len(results)
    print(f"\n=== standings after {len(results)} games (avg {avg_turns:.0f} turns) ===")
    for label, n in wins.most_common():
        print(f"{label:<42} {n:>4} wins  ({100 * n / len(results):.0f}%)")
    stats = model_stats(decisions_path)
    if stats:
        print(f"\n{'model':<34}{'moves':>7}{'retries':>9}{'median':>8}{'slowest':>9}{'tokens':>9}{'cost':>9}")
        for s in stats:
            print(
                f"{s['model']:<34}{s['moves']:>7}{s['retries']:>9}{s['median_s']:>7.0f}s"
                f"{s['slowest_s']:>8.0f}s{s['tokens']:>9}{s['cost']:>8.4f}$"
            )


if __name__ == "__main__":
    main()

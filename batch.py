"""Run many matches at once, each in its own process, each resumable.

Every match is an independent arena.py run writing its own files, so parallel
matches never touch each other's state. A match that dies is restarted with
--resume (it continues from its last recorded action); a match that hits a
fatal setup error — bad key, no credit, malformed request — stops for good
and is reported, because retrying it would just burn the same error 50 times.

Seats rotate: match i starts the lineup at i % seats, so over any multiple of
four matches each model sits in each turn position equally often. Turn order
is worth real points in Catan, and without rotation it rides along with model
identity into the win rate.

    python batch.py --matches 50 --dir runs
    python batch.py --matches 50 --dry-run          # free bots, no API calls
    python dashboard.py 8765 runs                   # watch them all

Resuming the whole batch is the same command: finished games are skipped.
"""

import argparse
import glob
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from collections import Counter

LINEUP = (
    "llm:deepseek/deepseek-v4-flash,llm:tencent/hy3,"
    "llm:google/gemini-3.7-flash,llm:openai/gpt-5.6-luna"
)
BOTS = "weighted,random,random,vp"  # --dry-run: exercises the orchestration for free
FATAL = 3  # arena.py's exit code for "do not retry"


def rotate(players, by):
    """Cycle the lineup so each model takes each turn position equally often."""
    seats = players.split(",")
    at = by % len(seats)
    return ",".join(seats[at:] + seats[:at])


class Match:
    """One arena.py process, restarted until its games are done or it's fatal."""

    def __init__(self, index, base, players, games):
        self.index, self.base, self.players, self.games = index, base, players, games
        self.proc, self.restarts, self.fatal = None, 0, None

    def done(self):
        try:
            with open(self.base + "_results.jsonl") as f:
                return sum(1 for line in f if line.strip()) >= self.games
        except OSError:
            return False

    def start(self):
        self.log = open(self.base + ".out", "a")
        self.proc = subprocess.Popen(
            [
                sys.executable, "arena.py",
                "--games", str(self.games),
                "--players", self.players,
                "--log", self.base,
                "--resume",
            ],
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # Ctrl-C reaches us, not each child mid-write
        )

    def poll(self):
        """True while this match still needs a running process."""
        if self.fatal or self.done():
            return False
        if self.proc is None:
            self.start()
            return True
        code = self.proc.poll()
        if code is None:
            return True
        if code == FATAL:
            self.fatal = f"fatal setup error (see {self.base}.out)"
            return False
        if self.done():
            return False
        self.restarts += 1  # crashed or was killed: resume from the last action
        self.start()
        return True

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def spend(directory):
    """Total logged cost so far, across every decision log in the batch."""
    total = 0.0
    for path in glob.glob(os.path.join(directory, "*_decisions.jsonl")):
        seats = {}
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") == "retry":
                    continue
                if "cost_total" in row:  # per-move delta
                    seats[row.get("color")] = seats.get(row.get("color"), 0) + (row.get("cost_usd") or 0)
                else:  # older logs carried the running total
                    seats[row.get("color")] = max(seats.get(row.get("color"), 0), row.get("cost_usd") or 0)
        total += sum(seats.values())
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--matches", type=int, default=50)
    parser.add_argument("--games", type=int, default=1, help="games per match")
    parser.add_argument("--players", default=LINEUP)
    parser.add_argument("--dir", default="runs")
    parser.add_argument("--concurrency", type=int, default=0, help="0 = all at once")
    parser.add_argument("--dry-run", action="store_true", help="free bot seats, no API calls")
    parser.add_argument("--no-rotate", action="store_true", help="keep one fixed seat order")
    args = parser.parse_args()

    players = BOTS if args.dry_run else args.players
    os.makedirs(args.dir, exist_ok=True)
    matches = [
        Match(
            i,
            os.path.join(args.dir, f"m{i:03d}"),
            players if args.no_rotate else rotate(players, i),
            args.games,
        )
        for i in range(args.matches)
    ]
    cap = args.concurrency or args.matches
    started = time.time()
    print(
        f"batch: {args.matches} matches x {args.games} game(s), "
        f"{cap} at a time, into {args.dir}/\n"
        f"seats:  {players}{'' if args.no_rotate else '  (rotated per match)'}\n"
        f"watch:  {sys.executable} dashboard.py 8765 {args.dir}",
        flush=True,
    )

    def shutdown(*_):
        print("\nstopping; rerun the same command to resume", flush=True)
        for m in matches:
            m.stop()
        raise SystemExit(130)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    pending = list(matches)
    live, last = [], 0
    while pending or live:
        while pending and len(live) < cap:
            m = pending.pop(0)
            if m.poll():
                live.append(m)
                # OpenRouter documents no concurrency cap, but Cloudflare in front
                # of it blocks bursts; starting 50 processes in lockstep looks like
                # one. A fifth of a second apart does not.
                time.sleep(0.2)
        live = [m for m in live if m.poll()]
        if time.time() - last > 15:
            last = time.time()
            done = sum(1 for m in matches if m.done())
            fatal = [m for m in matches if m.fatal]
            restarts = sum(m.restarts for m in matches)
            print(
                f"[{(time.time() - started) / 60:5.1f}m] {done:>3}/{args.matches} done  "
                f"{len(live):>3} running  {len(pending):>3} queued  "
                f"{restarts} restarts  {len(fatal)} fatal  ${spend(args.dir):.2f}",
                flush=True,
            )
            if fatal and len(fatal) == len(matches):
                print("every match hit a fatal error; stopping", flush=True)
                break
        time.sleep(1)

    mins = (time.time() - started) / 60
    done = sum(1 for m in matches if m.done())
    print(f"\n{done}/{args.matches} matches finished in {mins:.0f} min, ${spend(args.dir):.2f} logged")
    for m in matches:
        if m.fatal:
            print(f"  {m.base}: {m.fatal}")
    standings(args.dir)


def wilson(wins, games, z=1.959964):
    """95% interval on a win rate. A bare percentage from 30 games means nothing."""
    if not games:
        return 0.0, 0.0
    p, d = wins / games, 1 + z * z / games
    centre = (p + z * z / (2 * games)) / d
    half = z * math.sqrt(p * (1 - p) / games + z * z / (4 * games * games)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def standings(directory):
    """Wins per model with intervals — seat rotation means colours must be ignored."""
    wins, games = Counter(), Counter()
    for results in sorted(glob.glob(os.path.join(directory, "*_results.jsonl"))):
        base = results[: -len("_results.jsonl")]
        with open(results, errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                for model in lineup_of(f"{base}_g{row.get('game')}"):
                    games[model] += 1
                # "llm:openai/gpt-5.6-luna (ORANGE)" -> the model, whatever seat it drew
                winner = re.sub(r"\s*\([A-Z]+\)$", "", row.get("winner", ""))
                if winner:
                    wins[strip(winner)] += 1
    if not games:
        print("\nno finished games yet")
        return
    print(f"\n{'model':<26}{'wins':>6}{'games':>7}{'rate':>8}   95% interval")
    for model in sorted(games, key=lambda m: -wins[m] / max(games[m], 1)):
        lo, hi = wilson(wins[model], games[model])
        beats = "  beats chance" if lo > 0.25 else ""
        print(
            f"  {model:<24}{wins[model]:>6}{games[model]:>7}"
            f"{100 * wins[model] / games[model]:>7.0f}%   "
            f"{100 * lo:4.1f}% - {100 * hi:4.1f}%{beats}"
        )
    print("  (chance is 25% in a four-player game)")


def strip(spec):
    return spec.removeprefix("llm:")


def lineup_of(base):
    """The seats a game was played with, from its move log header."""
    try:
        with open(base + ".jsonl", errors="replace") as f:
            return [strip(s) for s in json.loads(f.readline()).get("players", [])]
    except (OSError, ValueError):
        return []


if __name__ == "__main__":
    main()

"""Freeze the dashboard into a static site for Cloudflare Pages.

The tournament is finished and its records never change, so none of this needs
a server: render each page once, rewrite the four dynamic URLs to file paths,
and copy the records across.

Playback frames are the only awkward part. Every move is its own JSON frame,
and 23,961 of them would blow Pages' 20,000-file limit on their own — so full
move-by-move replay is built for --featured games, and every other game ships
as a final-state page that still links to its complete records.

    python export_site.py --dir runs --out site --featured 6
"""

import argparse
import glob
import json
import os
import re
import shutil
import sys

import dashboard as dash


def staticize(html, depth=0):
    """Point the server's four dynamic routes at files on disk."""
    html = re.sub(r"/game\?base=([\w.-]+)", r"/g/\1.html", html)
    html = re.sub(r"/raw\?f=([\w./-]+)", lambda m: "/raw/" + os.path.basename(m.group(1)), html)
    html = html.replace("href='/stats'", "href='/stats.html'")
    html = html.replace('href="/stats"', 'href="/stats.html"')
    # the scrubber fetches a frame per move; clamp to the range that exists
    html = html.replace(
        "fetch('/frame?base='+BASE+'&at='+Math.max(0,i))",
        "fetch('/f/'+BASE+'/'+Math.min(Math.max(0,i),N)+'.json')",
    )
    # a frozen page is never "live"; the 5s meta refresh would just reload it
    html = html.replace('<meta http-equiv="refresh" content="5">', "")
    return html


REPO = "https://github.com/mustafaTokmak/catan-llm-arena"

INTRO = """<div class='card rise'>
<h2>what this is</h2>
<p class=sub style='font-size:15px;line-height:1.6;max-width:70ch'>
Four language models played 50 four-player games of Settlers of Catan against each other
&mdash; no scripted bots, no humans, no judges. Every move was chosen by the model whose
seat it was: a failed API call is retried until the model answers, never replaced by a
random move. <b>23,911 moves, 9,484 model decisions, 3.3 hours, $5.54.</b>
</p>
<p class=sub style='font-size:15px;line-height:1.6;max-width:70ch'>
<b>What the run supports:</b> the four models did not win equally often (p&nbsp;&asymp;&nbsp;0.003),
and DeepSeek and Luna together took 38 of 50 games (p&nbsp;=&nbsp;0.0003).
<b>What it does not:</b> no single model clears the 25% chance line once you correct for
testing four of them, and 19&ndash;19 between the leaders is 50 games failing to separate them.
</p>
<p class=sub style='font-size:15px;line-height:1.6;max-width:70ch'>
<b>The caveat that matters most:</b> this version of the prompt sent no board &mdash; no tiles,
no dice numbers, no adjacency &mdash; only a numbered list of legal moves. Catanatron numbers
nodes 0&ndash;23 as exactly the 24 interior three-tile intersections and lists actions in node
order, so &ldquo;prefer the top of the list&rdquo; is a decent opening heuristic on its own.
Read the ranking with that in mind.
</p>
<p class=sub style='font-size:15px;line-height:1.6'>
<a href='/stats.html'>full statistics</a> &middot;
<a href='https://github.com/mustafaTokmak/catan-llm-arena'>source and the review that found three of my mistakes</a>
</p>
</div>"""

NO_REPLAY = (
    "<div class=controls><span class=counter>"
    "Move-by-move replay is built for the featured games. "
    "This game's full move log and every model decision are in its records below."
    "</span></div>"
)


def freeze_controls(html):
    """Drop the scrubber (and its script) from a game with no frames on disk."""
    html = re.sub(r"<div class=controls>.*?</div>(?=<script>)", NO_REPLAY, html, flags=re.S)
    return re.sub(r"<script>const BASE=.*?</script>", "", html, flags=re.S)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def pick_featured(bases, want):
    """Show off the run: a win for each model, then the longest games."""
    by_winner, lengths = {}, {}
    for base in bases:
        match = base.rsplit("_g", 1)[0]
        try:
            with open(f"{match}_results.jsonl", errors="replace") as f:
                winner = json.loads(f.readline()).get("winner", "")
        except (OSError, ValueError):
            winner = ""
        lengths[base] = sum(1 for _ in open(base + ".jsonl", errors="replace"))
        by_winner.setdefault(dash.short(re.sub(r"\s*\([A-Z]+\)$", "", winner)), base)
    chosen = list(dict.fromkeys(by_winner.values()))[:want]
    for base in sorted(lengths, key=lambda b: -lengths[b]):
        if len(chosen) >= want:
            break
        if base not in chosen:
            chosen.append(base)
    return chosen


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="runs")
    ap.add_argument("--out", default="site")
    ap.add_argument("--featured", type=int, default=6, help="games with full replay")
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    root = os.path.abspath(os.path.dirname(__file__))
    os.chdir(args.dir)  # every dashboard function resolves bases against cwd

    if os.path.isdir(out):
        shutil.rmtree(out)
    bases = sorted(b[:-6] for b in glob.glob("*_g*.jsonl") if "_decisions" not in b)
    featured = set(pick_featured(bases, args.featured))
    print(f"{len(bases)} games, {len(featured)} featured for replay", flush=True)

    index = staticize(dash.index_page())
    index = index.replace("<div class=stack>", "<div class=stack>" + INTRO, 1)
    index = index.replace(" &middot; refreshes every 5s", "")
    index = index.replace("no matches yet &mdash; run ./run_match.sh", "no matches recorded")
    write(os.path.join(out, "index.html"), index)
    write(os.path.join(out, "stats.html"), staticize(dash.stats_page()))
    print("  index + stats", flush=True)

    frames = 0
    for base in bases:
        html = staticize(dash.game_page(base))
        if base not in featured:
            html = freeze_controls(html)
        write(os.path.join(out, "g", base + ".html"), html)
        if base in featured:
            _, _, n = dash.load(base)
            for i in range(n + 1):
                payload = json.dumps(dash.frame(base, i), separators=(",", ":"))
                write(os.path.join(out, "f", base, f"{i}.json"), payload)
            frames += n + 1
            print(f"  {base}: {n + 1} frames", flush=True)

    records = os.path.join(out, "raw")
    os.makedirs(records, exist_ok=True)
    kept = 0
    for path in glob.glob("*"):
        if path.endswith((".jsonl", ".out")):  # never the pickles: they are not records
            shutil.copy2(path, records)
            kept += 1

    # Pages needs a 404; reuse the index so a stale link still lands somewhere useful
    shutil.copy2(os.path.join(out, "index.html"), os.path.join(out, "404.html"))
    files = sum(len(f) for _, _, f in os.walk(out))
    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(out) for f in fs)
    print(f"\n{out}\n  {files} files, {size / 1e6:.0f} MB "
          f"({len(bases)} games, {frames} frames, {kept} records)")
    if files > 20000:
        print("  WARNING: over Cloudflare Pages' 20,000-file limit — lower --featured")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()

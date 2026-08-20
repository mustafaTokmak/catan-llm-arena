"""OpenRouter-backed Catan player: any model on openrouter.ai can take a seat.

Each decide() is one stateless chat-completions call: compact game summary +
numbered list of legal actions in, JSON {"reason", "action_index"} out.

Every move is the model's own. A slow or failing call is retried until the
model answers -- never substituted with a random move, because a substituted
move measures the arena, not the model. Retries use an escalating deadline
(a hung connection is abandoned quickly; a genuinely slow provider is given
more room each attempt). Only unrecoverable setup errors (bad key, unknown
model, no credit) stop a match, loudly.
"""

import itertools
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

import httpx

from catanatron import RESOURCES, Player
from catanatron.state_functions import (
    get_actual_victory_points,
    get_visible_victory_points,
    player_key,
    player_num_resource_cards,
)

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "catan_move",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "One short sentence."},
                "action_index": {
                    "type": "integer",
                    "description": "Index of the chosen action from the legal list.",
                },
            },
            "required": ["reason", "action_index"],
            "additionalProperties": False,
        },
    },
}

log = logging.getLogger("arena")

_schema_models = None


def supports_schema(model):
    """Which models can be held to the JSON schema; the rest get asked nicely."""
    global _schema_models
    if _schema_models is None:
        try:
            catalog = httpx.get(MODELS_URL, timeout=30).json()["data"]
            _schema_models = {
                m["id"]
                for m in catalog
                if "structured_outputs" in (m.get("supported_parameters") or [])
            }
        except Exception:
            _schema_models = set()  # catalog unreachable: prompt-only is fine
    return model in _schema_models

SYSTEM = (
    "You are an expert Settlers of Catan player. You get the current game "
    "situation and a numbered list of legal actions; pick the one that "
    "maximizes your chance of reaching 10 victory points first.\n"
    "Reminders: settle high-probability numbers (6/8, then 5/9), secure "
    "ore+wheat for cities, avoid holding 8+ cards (robber discard), and use "
    "the robber to block the current leader.\n"
    'Answer with ONLY a JSON object: {"reason": "<one short sentence>", '
    '"action_index": <integer>}'
)

JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)  # first flat JSON object

MAX_ACTIONS_LISTED = 80


def _api_key():
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:  # ponytail: 5-line .env reader beats a python-dotenv dependency
        with open(env_path) as f:
            for line in f:
                name, _, value = line.strip().partition("=")
                if name == "OPENROUTER_API_KEY" and value:
                    return value.strip().strip("'\"")
    except OSError:
        pass
    raise KeyError("OPENROUTER_API_KEY not set (env var or .env file)")


FATAL_STATUS = {401, 402, 403}  # bad key, no credit, blocked: retrying can't help


class FatalSetupError(Exception):
    """Wrong key, unknown model, or empty account — stop rather than spin."""


class LLMPlayer(Player):
    def __init__(
        self,
        color,
        model="deepseek/deepseek-v4-flash",
        timeout=180.0,
        deadline=20.0,
        max_deadline=120.0,
    ):
        super().__init__(color)
        self.model = model
        self.timeout = timeout
        # First attempt gives up at `deadline` — a hung socket shouldn't cost a
        # minute. Each retry allows longer, so a merely slow provider still lands.
        self.deadline = deadline
        self.max_deadline = max_deadline
        self.use_schema = supports_schema(model)
        self._http = None
        self.api_calls = 0
        self.retries = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.last_reason = ""
        self.last_error = ""
        self.decisions_path = None  # set by arena.py: timeline log for the UI
        self.game_index = 0

    def decide(self, game, playable_actions):
        actions = list(playable_actions)
        if len(actions) == 1:  # forced move (roll, end turn): skip the API
            return actions[0]
        started = time.time()
        before = (self.input_tokens, self.output_tokens, self.cost_usd)
        for attempt in itertools.count():  # every move is the model's own
            deadline = min(self.deadline * 1.6**attempt, self.max_deadline)
            try:
                index = self._ask(game, actions, deadline)
                action = actions[index]
            except FatalSetupError:
                raise
            except Exception as exc:
                self.retries += 1
                self.last_error = repr(exc)[:200]
                wait = self._retry_wait(exc, attempt)
                log.warning(
                    "[%s] retry %d after %.0fs: %s (waiting %.0fs)",
                    self.model,
                    attempt + 1,
                    deadline,
                    self.last_error[:90],
                    wait,
                )
                self._write(
                    {
                        "type": "retry",
                        "turn": game.state.num_turns,
                        "attempt": attempt + 1,
                        "gave_up_after_s": round(deadline, 1),
                        "error": self.last_error[:200],
                    }
                )
                time.sleep(wait)
                continue
            value = "" if action.value is None else f" {action.value}"
            seconds = time.time() - started
            log.info(
                "[%s] %s%s | %.0fs $%.4f | %s",
                self.model,
                action.action_type.value,
                value,
                seconds,
                self.cost_usd,
                self.last_reason[:80],
            )
            self._write(
                {
                    "type": "move",
                    "turn": game.state.num_turns,
                    "action": f"{action.action_type.value}{value}",
                    "reason": self.last_reason,
                    "chose": index,
                    "options": len(actions),
                    "seconds": round(seconds, 1),
                    "attempts": attempt + 1,
                    "schema": self.use_schema,
                    "tokens_in": self.input_tokens - before[0],
                    "tokens_out": self.output_tokens - before[1],
                    "cost_usd": round(self.cost_usd - before[2], 6),  # this move
                    "cost_total": round(self.cost_usd, 5),  # this seat so far
                }
            )
            return action

    def _write(self, row):
        """Machine-readable timeline: one line per move or retry, for the UI."""
        if not self.decisions_path:
            return
        row = {
            "t": time.time(),
            "game": self.game_index,
            "color": self.color.value,
            "model": self.model,
            **row,
        }
        with open(self.decisions_path, "a") as f:
            f.write(json.dumps(row) + "\n")

    @staticmethod
    def _retry_wait(exc, attempt):
        response = getattr(exc, "response", None)
        if response is not None and response.status_code == 429:
            return min(float(response.headers.get("retry-after") or 5 * (attempt + 1)), 30)
        return min(2 * (attempt + 1), 15)

    def _ask(self, game, actions, deadline):
        prompt = self.build_prompt(game, actions)
        if self._http is None:
            try:
                key = _api_key()
            except KeyError as exc:
                raise FatalSetupError(str(exc)) from None
            self._http = httpx.Client(
                headers={"Authorization": f"Bearer {key}"}, timeout=self.timeout
            )
        payload = {
            "model": self.model,
            "max_tokens": 6000,  # reasoning models need headroom before content
            "reasoning": {"effort": "low"},  # a move choice needs no essay
            "usage": {"include": True},  # exact cost accounting per call
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
        }
        if self.use_schema:
            payload["response_format"] = RESPONSE_FORMAT
            payload["provider"] = {"require_parameters": True}
        response = self._post_with_deadline(payload, deadline)
        if response.status_code in (400, 404) and self.use_schema:
            self.use_schema = False  # this endpoint won't enforce the schema
            payload.pop("response_format")
            payload.pop("provider")
            response = self._post_with_deadline(payload, deadline)
        if response.status_code in FATAL_STATUS or response.status_code == 404:
            raise FatalSetupError(f"HTTP {response.status_code}: {response.text[:200]}")
        response.raise_for_status()  # 429/5xx: the caller retries
        data = response.json()
        self.api_calls += 1
        usage = data.get("usage") or {}
        self.input_tokens += usage.get("prompt_tokens") or 0
        self.output_tokens += usage.get("completion_tokens") or 0
        self.cost_usd += usage.get("cost") or 0.0
        message = data["choices"][0]["message"]
        choice = None  # reasoning models sometimes leave the JSON outside content
        for text in (message.get("content") or "", message.get("reasoning") or ""):
            for candidate in JSON_RE.findall(text):
                try:
                    parsed = json.loads(candidate)
                except ValueError:
                    continue
                if "action_index" in parsed:
                    choice = parsed
                    break
            if choice is not None:
                break
        if choice is None:
            raise ValueError(f"no action JSON in reply: {(message.get('content') or '')[:120]!r}")
        self.last_reason = str(choice.get("reason", ""))
        index = int(choice["action_index"])
        if not 0 <= index < len(actions):
            raise ValueError(f"action_index {index} out of range")
        return index

    def _post_with_deadline(self, payload, deadline):
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            return pool.submit(self._http.post, API_URL, json=payload).result(
                timeout=deadline
            )
        except FutureTimeout:
            raise TimeoutError(f"no response within {deadline:.0f}s") from None
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def build_prompt(self, game, actions):
        # ponytail: v0 prompt = scores + hand + action list, no board geometry.
        # Upgrade path: serialize tiles/ports/buildings when moves look blind.
        state = game.state
        me = player_key(state, self.color)
        hand = ", ".join(
            f"{r}:{state.player_state[f'{me}_{r}_IN_HAND']}" for r in RESOURCES
        )
        opponents = "; ".join(
            f"{c.value}: {get_visible_victory_points(state, c)} VPs, "
            f"{player_num_resource_cards(state, c)} cards"
            for c in state.colors
            if c != self.color
        )
        lines = [
            f"Turn {state.num_turns}. You are {self.color.value}.",
            f"Your hand: {hand}.",
            f"Your victory points: {get_actual_victory_points(state, self.color)}/10.",
            f"Opponents: {opponents}.",
        ]
        recent = state.actions[-12:]
        if recent:
            lines.append("Recent moves:")
            for a in recent:
                value = "" if a.value is None else f" {a.value}"
                lines.append(f"- {a.color.value}: {a.action_type.value}{value}")
        lines += ["", "Legal actions:"]
        for i, action in enumerate(actions[:MAX_ACTIONS_LISTED]):
            value = "" if action.value is None else f" {action.value}"
            lines.append(f"{i}: {action.action_type.value}{value}")
        if len(actions) > MAX_ACTIONS_LISTED:
            lines.append(f"(+{len(actions) - MAX_ACTIONS_LISTED} more actions omitted)")
        lines.append('Reply with ONLY the JSON object for your chosen action.')
        return "\n".join(lines)


if __name__ == "__main__":  # smoke test: one real decision, plus the retry path
    from catanatron import Color, Game, RandomPlayer

    llm = LLMPlayer(Color.RED)
    others = [RandomPlayer(c) for c in (Color.BLUE, Color.WHITE, Color.ORANGE)]
    game = Game([llm, *others])
    real_post, failures = llm._post_with_deadline, itertools.count()

    def flaky(payload, deadline):  # first two calls fail: the move must survive
        if next(failures) < 2:
            raise TimeoutError("simulated stall")
        return real_post(payload, deadline)

    llm._post_with_deadline = flaky
    for _ in range(40):  # tick until the LLM faces one multi-option decision
        if llm.api_calls:
            break
        game.play_tick()
    assert llm.api_calls == 1, (llm.api_calls, llm.last_error)
    assert llm.retries >= 2, f"expected the 2 stalls to be retried, got {llm.retries}"
    assert llm.last_reason, "model returned no reasoning"
    print(
        f"OK model={llm.model} schema={llm.use_schema} retries={llm.retries} "
        f"tokens={llm.input_tokens}/{llm.output_tokens} "
        f"cost=${llm.cost_usd:.5f} reason={llm.last_reason!r}"
    )

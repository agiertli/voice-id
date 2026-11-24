# Scoring

These are the rules as implemented in the code, not as intended. Sources:

- `apps/voice-id/src/templates/index.html`: the browser computes **points**
  (constants `POINTS_BASE`, `POINTS_SPEED_MAX`, `SPEED_WINDOW_SEC`,
  `OPPONENT_BONUS`; functions `submitGuess`, `battleSubmitGuess`,
  `battleRevealTruth`, `battleShowFinal`, `completeGame`).
- `apps/voice-id/src/app.py`: the server judges each guess, counts **correct
  answers**, decides the **prize** and serves the **leaderboard**
  (`/api/game/<id>/guess`, `/api/game/<id>/complete`, `/api/leaderboard`).

Checked against the code on 2026-11-24, including the redesigned UI (the
redesign changed presentation, navigation and the results screen, not the scoring
constants or rules). The results screen shows the breakdown: correct answers ×
100, total speed bonus, opponent bonus, rank and prize. If you change a scoring constant, update this
page, the portal calculator (`portal/src/scoring.js`) and the skill together.

## Two numbers, two purposes

Every game produces two independent results:

| Result | What it is | Who computes it | Used for |
|---|---|---|---|
| **Correct answers** (`score`, e.g. 4/5) | How many clips you labelled correctly | Server, from the guesses it recorded | **Prize** |
| **Points** (`points`, e.g. 1040) | Correct answers weighted by speed, plus an opponent bonus | Browser, sent to the server at the end | **Leaderboard rank** |

A player can win a prize and rank low, or rank above someone with more correct
answers. Both are expected.

## The points formula

Constants (in `index.html`):

```text
POINTS_BASE      = 100   points for every correct answer
POINTS_SPEED_MAX = 200   maximum speed bonus per correct answer
SPEED_WINDOW_SEC = 10    the speed bonus shrinks to zero over 10 seconds
OPPONENT_BONUS   = { qwen: 50, gemma: 100, gary-base: 150, gary-finetuned: 250 }
```

For each clip:

```text
t           = seconds from the FIRST press of play on this clip to the guess click
speed_bonus = max(0, round(200 * (1 - t / 10)))
clip_points = 100 + speed_bonus    if the guess is correct
            = 0                    if the guess is wrong
```

For the game:

```text
solo game   points = sum of clip_points
battle game points = sum of clip_points
                   + OPPONENT_BONUS[opponent]   if you beat the opponent,
                                                or tied while answering every clip correctly
```

### When the clock runs

- The clock for a clip starts the **first time the player presses play** on that
  clip. Pausing does not stop it and replaying does not restart it.
- It stops when the player clicks **AI generated** or **Natural voice**.
- The guess buttons are enabled the moment play is pressed (in battle mode, once
  the browser confirms playback started). Players may answer **before the clip
  finishes**. Listening to the whole clip costs speed bonus: game clips are
  roughly 3 seconds long, so waiting for the end typically costs about 60 points
  per clip.
- Reveal animations, the "Next clip" screen, loading and the opponent's thinking
  animation are not timed.

### Points per correct answer by answer time

| Answer time t | Speed bonus | Points for a correct answer |
|---:|---:|---:|
| 0 s | 200 | 300 |
| 1 s | 180 | 280 |
| 2 s | 160 | 260 |
| 3 s | 140 | 240 |
| 4 s | 120 | 220 |
| 5 s | 100 | 200 |
| 6 s | 80 | 180 |
| 7 s | 60 | 160 |
| 8 s | 40 | 140 |
| 9 s | 20 | 120 |
| 10 s or more | 0 | 100 |
| wrong answer (any time) | 0 | 0 |

Rounding is JavaScript `Math.round` (halves round up): 2.5 s gives 150, 2.6 s
gives 148. Wrong answers never cost points; they just earn none.

Limits with the default 5 clips: the theoretical maximum is 1,500 points solo and
1,750 in a battle against Gary 2.0 (5 × 300 + 250). A realistic excellent game
(answers at about 3 s) is 5 × 240 = 1,200 solo.

## Worked examples: solo

All examples use the defaults (5 clips, prize at 4 correct).

| Player | Correct | Answer time per correct clip | Points | Prize |
|---|---:|---|---:|---|
| Anna (fast) | 4/5 | 2 s each | 4 × 260 = **1,040** | Yes (4 ≥ 4) |
| Ben (careful) | 4/5 | 12 s each (listens twice) | 4 × 100 = **400** | Yes |
| Cyril (perfect, steady) | 5/5 | 6 s each | 5 × 180 = **900** | Yes |
| Dana | 3/5 | 1 s each | 3 × 280 = **840** | No (3 < 4) |

Leaderboard order: Anna 1,040, Cyril 900, Dana 840, Ben 400. Anna ranks above
Cyril with fewer correct answers because she answered much faster. Dana ranks
above Ben but gets no prize. Mixed times simply add up: correct at 2 s, 4 s, 7 s
and 11 s plus one wrong answer is 260 + 220 + 160 + 100 + 0 = 740.

## Worked examples: battle

In a battle the player and an AI opponent label the same clips. The opponent
analyses all clips during the loading screen; its answer for each clip is revealed
after the player has answered. The **opponent's score is a count of correct
answers (0 to 5), not points**. It only decides whether the player earns the
opponent bonus. The opponent never appears on the leaderboard.

The player below always gets 4/5 correct, answering at 3 s (240 points per correct
clip), so 960 points before any bonus.

| Opponent (id) | Opponent correct | Outcome | Bonus | Final points |
|---|---:|---|---:|---:|
| Teddy (`qwen`), easy | 1/5 | Win | +50 | **1,010** |
| Iris (`gemma`), medium | 3/5 | Win | +100 | **1,060** |
| Gary (`gary-base`), hard | 3/5 | Win | +150 | **1,110** |
| Gary 2.0 (`gary-finetuned`), boss | 4/5 | Tie, not perfect | 0 | **960** |
| Gary 2.0 (`gary-finetuned`), boss | 5/5 | Loss | 0 | **960** |

Perfect game against the boss, answers at 3 s: 5 × 240 = 1,200. If Gary 2.0 also
gets 5/5 it is a tie with a perfect score, so the bonus applies: **1,450**. Losing
never removes points; it only withholds the bonus. The opponent bonus is the only
difference between solo and battle scoring, so a battle against a stronger
opponent is worth more only if you beat it.

The prize rule is identical in battle mode: 4 correct answers win a prize whether
or not the player beat the opponent.

## Prize

```text
prize = correct_answers >= PRIZE_THRESHOLD        (server, /api/game/<id>/complete)
```

- `correct_answers` is counted by the server from the guesses it recorded, so it
  cannot be inflated by the points value the browser sends.
- Points, answer time and opponent result have **no effect** on the prize.
- The results screen (solo and battle) shows "You won a prize! Show this to the
  booth staff." when the server says so. If the completion request failed, the
  screen shows neither banner (and an error toast); apply the rule by hand from
  the correct-answer count.
- Prize eligibility is per game. Nothing in the app stops a player from replaying
  until they reach the threshold or limits prizes per person; that is a booth
  policy decision.

## Leaderboard and rank

`GET /api/leaderboard`:

1. Takes every completed game from the `leaderboard` table.
2. Keeps **one row per player**: the game with the most points; among games with
   equal points, the one with more correct answers.
3. Sorts those rows by points (high to low), then correct answers (high to low).
4. Returns the **top 20** plus `total_players` (distinct nicknames with at least
   one completed game).

The TV view (`/tv`) refreshes every 10 seconds and shows the top 10 of those rows,
with correct answers, points, opponent (if any) and answer time.

- **Replaying:** only the best game counts. A worse replay never lowers a player's
  position; a better one replaces it. Points are not summed across games.
- **Player identity** is the exact nickname string (trimmed, case-sensitive). "Anna"
  and "anna" are two players; two people typing the same nickname share one entry.
- **Ties:** equal points are broken by more correct answers. If both are equal, the
  order is whatever SQLite returns and is not guaranteed. Agree a tie-break for the
  prize ceremony in advance (for example, shorter answer time shown on the TV).
- **Results screen rank** ("rank N of M") comes from `/complete`:
  `N = 1 + number of players whose best game has strictly more points than this
  player's best game`, and `M` = distinct players. This is the same basis as the
  leaderboard (best game per player), so ties share a rank and N never exceeds M.
  (Fixed in v3.0.0; earlier versions counted games, not players.)
- **Quitting** a game (Quit, or leaving mid-game) abandons it: no completion is
  sent and nothing reaches the leaderboard. An idle booth screen returns to
  registration after 90 seconds outside a game.

## Configuration knobs

Set in `apps/voice-id/helm/voice-id/values.yaml` (or your own values file) and
apply with Helm. No image rebuild.

| Value | Env var | Default | Effect |
|---|---|---|---|
| `game.size` | `GAME_SIZE` | `5` | Clips per game. Each game always contains at least one AI clip and one human clip; the split is random. The database must hold at least `game.size` clips. |
| `game.prizeThreshold` | `PRIZE_THRESHOLD` | `4` | Correct answers needed for a prize. Not validated: a value above `game.size` makes prizes impossible. |

```bash
helm upgrade voice-id apps/voice-id/helm/voice-id -n voice-id \
  -f apps/voice-id/helm/voice-id/values.yaml \
  --set game.prizeThreshold=5
```

Use `-f` (or `--reset-values`) on every upgrade; a bare `helm upgrade` reuses the
previous release's values and silently ignores edits to `values.yaml`.

The point constants and opponent bonuses are hard-coded in `index.html`. Changing
them needs a new image. Changing `game.size` changes the maximum points
proportionally (300 per clip plus the bonus).

## Known limitations

Factual observations from the code, without fixes:

1. **Points are computed in the browser.** `/api/game/<id>/complete` stores the
   `points` integer it receives without validating or recomputing it, so anyone
   who can reach the route can post arbitrary points and top the leaderboard. The
   correct-answer count and the prize are computed server-side.
2. **Completion is not idempotent.** Calling `/complete` again for the same game
   inserts another leaderboard row. The server does not check that all clips were
   answered: `total` is the number of guesses recorded.
3. **Guesses are not tied to the game's clips.** `/api/game/<id>/guess` accepts any
   existing `clip_id` and overwrites earlier guesses for the same clip, so a
   scripted client could record more than `game.size` correct guesses.
4. **The speed clock includes listening time** and is measured in the browser
   (`performance.now()`), starting at the first press of play and ignoring pauses.
5. **Not pressing play:** the guess buttons stay disabled until play is pressed,
   so a player cannot answer without starting the clip. The code's fallback for a
   missing start time is 10 seconds, which means zero speed bonus, not maximum.
   In battle mode an elapsed time of exactly 0 would also be treated as 10 seconds
   (`_lastElapsed || SPEED_WINDOW_SEC`); this is practically unreachable.
6. **Opponent failures become coin flips.** If an opponent's analysis request fails
   for a clip, the browser substitutes a random guess ("Analysis failed"). That
   random answer counts towards the opponent's score and therefore the bonus.
7. **A failed completion request loses the game.** If `/complete` fails, the
   results screen falls back to the browser's own numbers with no prize banner and
   an error toast; there is no retry, so the game is missing from the leaderboard.
   (Before the 2026-11 UI redesign, battle mode did not wait for this request at
   all.)
8. **Answer time on the TV** is `duration_sec`, the sum of per-clip answer times
   sent by the browser, not wall-clock time. If the browser sends 0, the server
   falls back to the time since the game started.
9. **Two reset endpoints with different protection.** `POST /api/admin/reset`
   (no authentication) and `POST /api/admin/reset-scores` (admin token) both delete
   every game and leaderboard row. See [booth operations](booth-operations.md).

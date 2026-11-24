# Scoring

These are the rules as implemented in the code, not as intended. All scoring is
**server-side** in `apps/voice-id/src/app.py`:

- constants `POINTS_BASE`, `POINTS_SPEED_MAX`, `SPEED_WINDOW_SEC`, `OPPONENT_BONUS`
  (top of the file);
- `POST /api/game/<id>/play` starts the clock for a clip, `POST /api/game/<id>/guess`
  judges the guess and stores its points, the `ai-analyze*` endpoints record the
  battle opponent's answers, `POST /api/game/<id>/complete` computes the result,
  `GET /api/leaderboard` serves the board.

The browser (`index.html`) only displays what the server returns; it receives the
constants from the server for labels such as "+250 if you win". The results screen
shows the server's breakdown: correct answers × 100, total speed bonus, opponent
bonus, rank and prize.

Checked against the code on 2026-11-24 (server-side scoring introduced the same
day; the rules and constants did not change). If you change a scoring constant,
update this page, the portal calculator (`portal/src/scoring.js`) and the skill
together.

## Two numbers, two purposes

Every game produces two independent results:

| Result | What it is | Who computes it | Used for |
|---|---|---|---|
| **Correct answers** (`score`, e.g. 4/5) | How many clips you labelled correctly | Server, from the guesses it recorded | **Prize** |
| **Points** (`points`, e.g. 1040) | Correct answers weighted by speed, plus an opponent bonus | Server, from the play/guess times and opponent answers it recorded | **Leaderboard rank** |

A player can win a prize and rank low, or rank above someone with more correct
answers. Both are expected.

## The points formula

Constants (in `app.py`):

```text
POINTS_BASE      = 100   points for every correct answer
POINTS_SPEED_MAX = 200   maximum speed bonus per correct answer
SPEED_WINDOW_SEC = 10    the speed bonus shrinks to zero over 10 seconds
OPPONENT_BONUS   = { qwen: 50, gemma: 100, gary-base: 150, gary-finetuned: 250 }
```

For each clip:

```text
t           = seconds from the FIRST press of play on this clip to the guess,
              measured by the server (POST /play -> POST /guess)
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
  clip: the browser sends `POST /api/game/<id>/play` and the server stores the
  time (first call wins). Pausing does not stop it and replaying does not restart it.
- It stops when the server receives the guess, sent the moment the player clicks
  **AI generated** or **Natural voice** (in battle mode too: the opponent's
  thinking animation runs after the guess is recorded). Network latency counts,
  typically a few hundredths of a second.
- If the server never received a play for the clip, t = 10 s: zero speed bonus.
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

Rounding is half up (the same as JavaScript `Math.round`): 2.5 s gives 150,
2.6 s gives 148. Wrong answers never cost points; they just earn none.

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
analyses all clips during the loading screen and the **server records** each
answer; the analysis response does not contain it (sealed), and the server reveals
it in the `/guess` response after the player has answered. The opponent of a game
is fixed by the first analysis request and must be chosen before the first guess. The **opponent's score is a count of correct
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

- `correct_answers` is counted by the server from the guesses it recorded (one
  guess per clip of the game).
- Points, answer times, game time and opponent result have **no effect** on the prize.
- The results screen (solo and battle) shows "You won a prize! Show this to the
  booth staff." when the server says so. If the completion request failed, the
  screen shows neither banner (and an error toast) and falls back to the running
  totals from the per-guess responses; apply the rule by hand from the
  correct-answer count.
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
with correct answers, points, opponent (if any) and game time.

### Game time

The leaderboard and TV column **Game time** (i18n key `lb_time`: en "Game time",
cs/sk "Čas hry") is `duration_sec`, computed by the server on `/complete`:

```
game time = last answer (latest guessed_at) − first play press (earliest played_at)
```

It is the wall-clock time of actual play: it starts when the player first presses
play and ends with the last answer. It **excludes** registration, loading and
"Preparing the game" (opponent pre-analysis); it **includes** listening, thinking,
reveal animations and the "Next clip" screens. A clip answered without pressing play
counts from its answer time. If the game has no play timestamps at all (for example
a client that never sends `/play`), the server falls back to the sum of the
per-clip answer times (10 s for a clip never played). Game time is informational:
it does not change points, rank or the prize. The per-clip speed bonus still uses
each clip's own play-to-guess time. (Up to v3.3.2 the server stored the sum of
per-clip answer times and the column was labelled "Answer time".)

- **Replaying:** only the best game counts. A worse replay never lowers a player's
  position; a better one replaces it. Points are not summed across games.
- **Player identity** is the exact nickname string (trimmed, case-sensitive). "Anna"
  and "anna" are two players; two people typing the same nickname share one entry.
- **Ties:** equal points are broken by more correct answers. If both are equal, the
  order is whatever SQLite returns and is not guaranteed. Agree a tie-break for the
  prize ceremony in advance (for example, shorter game time shown on the TV).
- **Completion is idempotent:** a second `/complete` for the same game returns the
  stored result (`already_completed: true`) and adds no leaderboard row. Points,
  `duration_sec` and the opponent sent by a client are ignored.
- **Results screen rank** ("rank N of M") comes from `/complete`:
  `N = 1 + number of players whose best game has strictly more points than this
  player's best game`, and `M` = distinct players. This is the same basis as the
  leaderboard (best game per player), so ties share a rank and N never exceeds M.
  (Fixed in v3.0.0; earlier versions counted games, not players.)
- **Quitting** a game (Quit, or leaving mid-game) abandons it: no completion is
  sent and nothing reaches the leaderboard. An idle booth screen returns to
  registration after 90 seconds outside a game.

## Which clips a game contains

`select_game_clips` in `app.py` (since v3.3.0) picks the clips for every game:

- **The human/AI split is random:** 1 to 4 AI clips out of 5 (always at least one of
  each). A fixed split would let players deduce the last answers ("I already had three
  AI, so this one is human").
- **At least `game.minCloneClips` (default 1) AI clips are clones of real people**,
  that is, AI clips whose speaker also has human recordings. This keeps the "real
  person vs their own clone" moment in every game.
- **At most `game.maxOtherAiClips` (default 1) other AI voices**: XTTS stock voices
  and clones of public-dataset voices (speakers marked AI-only). They stay a minority
  so colleague voices dominate.
- If the clip pools are too small to satisfy the rules, selection falls back to the
  plain random mix.

The mix does not change scoring: every clip is worth the same.

## Configuration knobs

Set in `apps/voice-id/helm/voice-id/values.yaml` (or your own values file) and
apply with Helm. No image rebuild.

| Value | Env var | Default | Effect |
|---|---|---|---|
| `game.size` | `GAME_SIZE` | `5` | Clips per game. Each game contains at least one AI and one human clip; the split is random. The database must hold at least `game.size` clips. |
| `game.prizeThreshold` | `PRIZE_THRESHOLD` | `4` | Correct answers needed for a prize. Not validated: a value above `game.size` makes prizes impossible. |
| `game.minCloneClips` | `GAME_MIN_CLONE_CLIPS` | `1` | Minimum AI clones of real people per game (see [which clips a game contains](#which-clips-a-game-contains)). |
| `game.maxOtherAiClips` | `GAME_MAX_OTHER_AI_CLIPS` | `1` | Maximum other AI voices (XTTS stock, public-dataset voices) per game. |

```bash
helm upgrade voice-id apps/voice-id/helm/voice-id -n voice-id \
  -f apps/voice-id/helm/voice-id/values.yaml \
  --set game.prizeThreshold=5
```

Use `-f` (or `--reset-values`) on every upgrade; a bare `helm upgrade` reuses the
previous release's values and silently ignores edits to `values.yaml`.

The point constants and opponent bonuses are hard-coded in `app.py` (the browser
reads them from the server). Changing them needs a new image. Changing `game.size` changes the maximum points
proportionally (300 per clip plus the bonus).

## Known limitations

Factual observations from the code, without fixes:

1. **The speed clock trusts the moment the browser says "play".** Clips are
   preloaded (downloaded) before the game starts, and the audio endpoint
   `/api/clip/<id>/audio` is public, so a scripted client could listen first and
   then send play and guess back to back for the maximum speed bonus. The server
   can only guarantee that points follow the formula from the times it saw, and
   that each clip is guessed once.
2. **The speed clock includes listening time** and network latency, starting at the
   first play request and ignoring pauses.
3. **Completion does not require all clips.** `total` is the number of guesses
   recorded; a game completed early simply has fewer points.
4. **Opponent failures become coin flips.** If an opponent's analysis fails for a
   clip (or never reaches the server), the server records a random answer
   (`fallback: true`, reasoning "Analysis failed"). It counts towards the
   opponent's score and therefore the bonus, and is the same answer the player sees.
5. **A failed completion request leaves the game off the leaderboard** unless the
   browser retries: there is no automatic retry. Calling `/complete` again later is
   safe (idempotent).
6. **Game time on the TV** is wall-clock from the first play press to the last
   answer, so a player who lingers on reveal screens gets a longer game time
   without losing points. See [game time](#game-time).
7. **The admin token is shared.** All `/api/admin/*` endpoints, including
   `POST /api/admin/reset` and `POST /api/admin/reset-scores` (both delete every
   game and leaderboard row), need the admin token. The image default is public;
   override it per event. See [booth operations](booth-operations.md).
8. **Games open during the upgrade** to server-side scoring complete with the
   correct-answer count, but clips guessed before the new pod started earn no
   points, and a browser still running the old page sends no play requests (no
   speed bonus). Leaderboard rows from older versions are kept unchanged.

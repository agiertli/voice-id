# Booth operations

One application, three screens, one shared database:

| URL | Who looks at it | What it does |
|---|---|---|
| `https://<route>/` | Players, on the booth laptop or tablet | The game: nickname, solo or battle, five clips, result |
| `https://<route>/tv` | Everyone walking past, on the booth monitor | Full-screen leaderboard, top 10, refreshes every 10 s, plus the opponent ladder |
| `https://<route>/admin` | Booth staff only | Players, game history, CSV export, score reset, speaker profiles (password protected) |

Get the route:

```bash
export KUBECONFIG=~/.kube/<cluster>.kubeconfig
oc get route voice-id -n voice-id -o jsonpath='https://{.spec.host}{"\n"}'
```

## Admin password

The `/admin` page asks for a password and sends it as a Bearer token. The same
token protects **every** `/api/admin/*` endpoint: the player list, per-player game
history, CSV export, **Reset all scores** (and the older `POST /api/admin/reset`),
clip management (`GET /api/admin/clips`, `POST /api/admin/upload`,
`DELETE /api/admin/delete-clip/<id>`), spectrograms, seed packs, backups and the
speaker endpoints. Scripts send it as `-H "Authorization: Bearer ${TOKEN}"`;
`tools/import_from_clone_studio.py` takes `--game-token` or `VOICEID_ADMIN_TOKEN`.

- The image has a built-in default password: `v01c3@r3dh4t!`. It is in the public
  source code, so treat it as known to everyone.
- **Override it for every event** with the Helm value `adminToken` (sets the
  `ADMIN_TOKEN` env var; empty keeps the image default).

For a terminal installation, keep the password out of shell history and versioned
files:

```bash
umask 077
read -r -s -p 'Choose admin password: ' P; printf '\n'; printf '%s' "$P" > ~/.voice-id-admin-token; unset P

helm upgrade voice-id apps/voice-id/helm/voice-id -n voice-id \
  -f apps/voice-id/helm/voice-id/values.yaml \
  --set-file adminToken=$HOME/.voice-id-admin-token
```

For an agent, the owner may write the password in the prompt
(`Admin password: <choice>`); the agent writes it to a mode-600 temporary file,
uses `--set-file adminToken=...` and deletes the file. A literal password stays in
the chat history.

Limits of this mechanism:

- The value is stored in the Helm release Secret and as a plain env var on the
  Deployment: anyone with view access to the namespace can read it
  (`oc set env deploy/voice-id -n voice-id --list | grep ADMIN_TOKEN`).
- Changing it rolls the pod: a short interruption. See the RWO volume note in
  [INSTALLATION.md troubleshooting](../INSTALLATION.md#troubleshooting) if the new
  pod hangs in `ContainerCreating`.
- Every later `helm upgrade` must pass it again (use the same `--set-file` or keep
  it in a private values file). A plain `helm upgrade -f values.yaml` without it
  resets the password to the image default.
- The browser remembers the password in a cookie scoped to `/admin`.

## Admin page features

`/admin` has four tabs; the selected tab is kept in the URL (`/admin#speakers`), so
you can bookmark or share a direct link. Arrow keys move between tabs. Help text sits
behind the small **?** buttons.

| Tab | What you do there |
|---|---|
| **Players** | Nickname, email, games, best points; click (or Enter) a row for the game history. **Export CSV** (`rank, nickname, email, best_score, best_points, games_played`, by best points) for prize draws; contains emails. |
| **Speakers** | Display names and photos for the reveal ([details](clips-and-speakers.md#speaker-display-names-and-photos)); human/AI clip counts; **Merge or rename** speaker keys; **AI-only voice** flag. The tab shows a red counter and the page a banner when the speaker integrity check finds problems. Only upload photos of people who agreed to be shown. |
| **Clips & seed packs** | Clip counts per language (human/AI) and AI model; **Export** / **Import seed pack** (clips + speaker profiles, no personal data). See [SEEDING.md](../SEEDING.md). |
| **Danger zone** | **Reset all scores** (deletes every game and leaderboard row, keeps players and clips; asks twice) and **Download full backup** (everything incl. emails, operator use only; see [backups](backups.md)). |

## Known risks

- **One shared admin token** protects all admin endpoints, and the image default
  is public. If you keep the default, anyone can list, upload or delete clips and
  wipe the leaderboard. Override `adminToken` for every event.
- **Points are computed server-side**, one guess per clip of the game, and
  `/complete` is idempotent. A scripted client can still listen to the (public,
  preloaded) clip audio before starting the clock, so a very fast answer time is
  not proof of a human reaction. See
  [scoring known limitations](scoring.md#known-limitations).
- The game endpoints are public by design: anyone who can reach the route can start
  games, fetch the audio of the clips in a game and trigger opponent analyses (GPU
  load).
- The stats endpoints `/api/stats/most-mistaken-humans` and
  `/api/stats/most-mistaken-clips` are public (no personal data).

Mitigations that need no code change: keep the route URL off public slides and
QR codes where possible, export results regularly ([backups](backups.md)), and
remove the route or scale the deployment to zero after the event.

## Before the doors open

1. Warm Fish Speech if Clone Studio will be used live (first request takes about
   4 minutes; see [INSTALLATION.md](../INSTALLATION.md#fish-speech-warm-up)).
2. Run the smoke test in [INSTALLATION.md](../INSTALLATION.md#6-verify): clip count,
   one game, each opponent answering.
3. **No speaker warnings.** `/admin` must show no red banner. One person = one speaker
   key (lowercase first name); merge aliases or mark synthetic voices **AI-only voice** on
   the Speakers tab ([why](clips-and-speakers.md#speaker-identity-one-person--one-key-hard-rule)).
4. Play one solo game and one battle on the booth laptop with a test nickname,
   then reset scores (admin page) so the board starts empty. Battle start time
   is dominated by the slowest opponent analysing five clips.
5. Open `/tv` on the monitor in full-screen (F11) and confirm it updates.
6. Tell staff the prize rule: **4 of 5 correct** (default) wins, regardless of
   points or opponent.
7. Agree how ties are handled for any end-of-day prize ([scoring](scoring.md#leaderboard-and-rank)).

## During the event

- A player keeps their identity by typing the same nickname again ("welcome
  back" shows their best points). Only their best game counts on the board.
- If an opponent misbehaves (slow, errors), disable it with Helm
  ([AI opponents](ai-opponents.md#enable-or-disable-an-opponent)) rather than let
  it play coin flips.
- At the end of each day take a full backup and export the CSV ([backups](backups.md)).

## Reset between days or events

Take a full backup and export the CSV first ([backups](backups.md)), then reset from `/admin` → **Danger zone** → **Reset all scores**, or:

```bash
curl -sk -X POST "https://${ROUTE}/api/admin/reset-scores" -H "Authorization: Bearer ${TOKEN}"
```

To also remove players (nicknames and emails) there is no endpoint. The only way
is a new, empty PVC, which also deletes all clips; import the seed pack again afterwards.

## Branding and language

- `conference.name` (Helm) sets the event name shown in the game and on the TV.
- `game.defaultLang` sets the UI language a new browser starts with; players can
  switch. See [languages](languages.md).

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

The `/admin` page asks for a password and sends it as a Bearer token. It protects
the player list, per-player game history, CSV export, **Reset all scores** and the
speaker endpoints.

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

- **Players:** nickname, email, games played, best correct count and best points;
  click a player for their game history.
- **Export CSV:** `rank, nickname, email, best_score, best_points, games_played`,
  ordered by best points. This is the list to use for prize draws or follow-up;
  it contains personal data (emails), so handle it accordingly.
- **Reset all scores:** deletes every game and leaderboard row. Players (nicknames,
  emails) and clips are kept.
- **Seed pack & backup:** export or import a seed pack (clips and speaker
  profiles, no personal data) and download a full backup (everything, including
  emails; operator use only). See [SEEDING.md](../SEEDING.md) and
  [backups](backups.md).
- **Speakers:** display names and photos for the personal reveal. See
  [clips and speakers](clips-and-speakers.md#speaker-display-names-and-photos).
  Only upload photos of people who agreed to be shown.

## Known risks

- **Clip management is unauthenticated.** `GET /api/admin/clips`,
  `POST /api/admin/upload` and `DELETE /api/admin/delete-clip/<id>` accept requests
  from anyone who can reach the route. Someone could add, list or delete clips.
- **`POST /api/admin/reset` is unauthenticated** and deletes all games and the
  leaderboard, exactly like the protected **Reset all scores** button
  (`/api/admin/reset-scores`).
- **Points are trusted from the browser.** See
  [scoring known limitations](scoring.md#known-limitations).
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
3. Play one solo game and one battle on the booth laptop with a test nickname,
   then reset scores (admin page) so the board starts empty. Battle start time
   is dominated by the slowest opponent analysing five clips.
4. Open `/tv` on the monitor in full-screen (F11) and confirm it updates.
5. Tell staff the prize rule: **4 of 5 correct** (default) wins, regardless of
   points or opponent.
6. Agree how ties are handled for any end-of-day prize ([scoring](scoring.md#leaderboard-and-rank)).

## During the event

- A player keeps their identity by typing the same nickname again ("welcome
  back" shows their best points). Only their best game counts on the board.
- If an opponent misbehaves (slow, errors), disable it with Helm
  ([AI opponents](ai-opponents.md#enable-or-disable-an-opponent)) rather than let
  it play coin flips.
- At the end of each day take a full backup and export the CSV ([backups](backups.md)).

## Reset between days or events

Take a full backup and export the CSV first ([backups](backups.md)), then reset from `/admin` → **Reset all scores**, or:

```bash
curl -sk -X POST "https://${ROUTE}/api/admin/reset-scores" -H "Authorization: Bearer ${TOKEN}"
```

To also remove players (nicknames and emails) there is no endpoint. The only way
is a new, empty PVC, which also deletes all clips; import the seed pack again afterwards.

## Branding and language

- `conference.name` (Helm) sets the event name shown in the game and on the TV.
- `game.defaultLang` sets the UI language a new browser starts with; players can
  switch. See [languages](languages.md).

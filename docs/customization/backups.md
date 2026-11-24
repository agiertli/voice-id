# Seed packs, backups and restore

Everything the game knows lives in one SQLite file on the `voice-id-data` PVC:
clips (audio BLOBs), players (nicknames, emails), games, the leaderboard, clip
statistics and speaker profiles. Clone Studio has its own SQLite file on the
`voice-clone-studio-data` PVC (cloning references, generated clips, sentence
recordings). A PVC is persistence, not a backup.

For the end-to-end seeding workflow (get, import, verify, unpack, share) see
[SEEDING.md](../SEEDING.md). This page focuses on backup and recovery.

## Rules

1. **Audio never goes to Git.** Seed packs, backups and exports contain voices of
   real people; full backups also contain player emails. Keep them only in a local
   directory **outside** the repository (default `~/voice-id-audio/`), never in
   Git, issues, pull requests, chat tools or public storage. Delete them when the
   event's retention period ends.
2. **Never `oc cp` a database** to or from a running pod. SQLite runs in WAL mode
   and the copy corrupts it or the running app. Use the API.
3. Use a dedicated kubeconfig per cluster (`export KUBECONFIG=...`) when you look
   up routes; do not `oc login` into a shared kubeconfig.

## Three kinds of export

| Export | Contains | Personal data | Purpose | Share? |
|---|---|---|---|---|
| **Seed pack** (zip) | `manifest.json`, all clips, speaker profiles (display names, photos) | Voices and photos of consenting speakers; **no** players, emails or results | Give another team the clip set; restore clips after a PVC loss | Only privately, to a team that will run the game |
| **Full backup** (`GET /api/admin/backup`) | Consistent SQLite snapshot of everything | **Yes**: player nicknames and emails, results | Operator disaster recovery | **Never distribute** |
| **Results CSV** (`GET /api/admin/export`, or admin page **Export CSV**) | Rank, nickname, email, best score, best points, games played | **Yes** | Prizes, follow-up | Event owner only |

All three need the admin password ([booth operations](booth-operations.md#admin-password)).

## How a new team gets the clips

The repository contains no audio. A team that wants to run the game receives a
**seed pack** from the game owner through a private channel, then imports it:

- **Admin UI:** `/admin` → **Seed pack & backup** → **Import seed pack**, choose
  the zip and a mode.
- **Headless:**

```bash
export VOICEID_ADMIN_TOKEN=...   # admin password; read it with `read -s`, not in history
python3 apps/voice-id/tools/seedpack.py import \
  --game "https://${ROUTE}" --file ~/voice-id-audio/seedpacks/<pack>.seedpack.zip --mode skip
```

| Mode | Effect |
|---|---|
| `skip` (default) | Adds new clips; same filename with different content is reported as a conflict and left alone |
| `replace` | Also overwrites clips whose filename matches but content differs (clip id kept) |

Import is idempotent (filename plus SHA-256) and all-or-nothing: an invalid pack is
rejected before anything is written. Check the result:

```bash
curl -sk -H "Authorization: Bearer ${VOICEID_ADMIN_TOKEN}" "https://${ROUTE}/api/admin/clips" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)), "clips")'
```

## Export a seed pack

- **Admin UI:** `/admin` → **Seed pack & backup** → **Export seed pack**.
- **Headless:**

```bash
python3 apps/voice-id/tools/seedpack.py export --game "https://${ROUTE}"   # -> ~/voice-id-audio/seedpacks/
```

The tool writes outside the repository by default and refuses to write inside a
Git working tree unless `--force` is given (don't). Export a seed pack:

- right after loading or changing the clip set;
- before handing the game to another team (only to that team, privately);
- before any PVC change or cluster teardown.

## Full backup (disaster recovery)

```bash
umask 077
curl -sk -H "Authorization: Bearer ${TOKEN}" \
  "https://${ROUTE}/api/admin/backup" -o ~/voice-id-audio/backups/voiceid-$(date +%Y%m%d-%H%M).db
```

This is a consistent snapshot taken by the application, not a file copy, so it is
safe while players are active. Take one at the end of every booth day and before
any `helm upgrade`. It is the only export that keeps game history and the
leaderboard. Restoring it means replacing the database of a stopped app; treat
that as an owner-approved recovery step, do it on a fresh PVC with the deployment
scaled to zero, and never by `oc cp` into a running pod. There is no documented
restore procedure for it yet (open question).

## Low-level option: the clip API

The seed pack is the supported path. The underlying API still works and is what
the first install on 2026-11-24 used:

| Data | Export | Restore |
|---|---|---|
| Clips | `GET /api/admin/clips`, `GET /api/clip/<id>/audio` | `POST /api/admin/upload` per clip (no deduplication; restore into an empty game only) |
| Speakers | `GET /api/speakers`, `GET /api/speakers/<name>/photo` | Admin speaker endpoints (token) |
| Results | `GET /api/admin/players`, `GET /api/admin/players/<id>/games` (token) | No import endpoint |

See [clips and speakers](clips-and-speakers.md#low-level-bulk-upload-from-a-local-database)
for the upload loop.

## Back up Clone Studio

Clone Studio's API is unauthenticated and has no seed pack. Its data:

| Data | Export |
|---|---|
| Cloning references (~30 s per voice) | `GET /api/voices`, `GET /api/voices/<voice_id>/audio` |
| Generated clips | `GET /api/clips`, `GET /api/clips/<clip_id>/audio` |
| Sentence recordings | `GET /api/recordings`, `GET /api/recordings/<recording_id>/audio` |

The import tool's `--out-dir` exports sentence recordings and AI pairs with a
`manifest.json`; point it at `~/voice-id-audio/...`. References can be restored with
`POST /api/voices` (multipart `audio`, `name`), which re-registers them with XTTS
and Fish and creates new voice ids.

## Restore drill

Before an important event, rehearse once on a scratch namespace or cluster:
install, import the latest seed pack, check the clip count, play one game. A
backup nobody has restored is a hope.

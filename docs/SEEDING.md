# Seeding the game with clips

The game is useless without clips, and the repository contains none on purpose.
This guide covers the whole life of seed data: getting it, loading it, inspecting
it, growing it, sharing it and recovering it.

## Create a seed pack (copy-paste)

A seed pack is one file, `voice-id-seedpack-<date>.seedpack.zip`. You never zip files by hand.
Run everything from the repo root (`cd ~/Documents/work/voice-id`).

**From audio files on your disk** (for example `apps/voice-id/samples/`):

```bash
python3 apps/voice-id/tools/seedpack.py pack --dir apps/voice-id/samples
```

The folder must look like this (the tool reads metadata from paths and names):

```text
<folder>/
  natural/<cs|sk|en>/<speaker>-<anything>.wav       human clip   e.g. natural/cs/anton-pozdrav.wav
  ai/<cs|sk|en>/<fish|xtts>-<speaker>-<anything>.wav AI clip      e.g. ai/en/fish-anton-hello.wav
  speakers/<speaker>.jpg                            optional photo for the reveal
```

**From the running game** (exactly the clips it plays now, incl. speaker names and photos):
`/admin` → **Seed pack & backup** → **Export seed pack**, or

```bash
python3 apps/voice-id/tools/seedpack.py export --game https://<game-route> --token <admin-password>
```

Either way the file lands in `~/voice-id-audio/seedpacks/` (outside Git, readable only by you).

**Share it:** optionally encrypt (`age -p -o pack.zip.age pack.zip`, send the passphrase separately),
put it in a Red Hat Google Drive folder shared with named people only, set an access expiry, and note
who got which version. **Import it:** recipient opens `/admin` → **Seed pack & backup** → **Import seed pack**.

## Rule zero

Seed data is **voices of real people** (and AI clones of them) plus speaker
photos. A leaked sample is enough to clone someone's voice. It is highly sensitive
personal and biometric data; see the rule at the top of [AGENTS.md](../AGENTS.md).

- **Never in Git.** Not in this public repository, not in a private one, not
  renamed, zipped or base64-encoded. The repository's `samples/` folders were
  removed on purpose; `.gitignore`, the pre-commit hook
  (`git config core.hooksPath .githooks`) and the `no-audio` CI workflow enforce it.
- **Never public.** No issues, pull requests, gists, the GitHub Pages portal, chat
  tools, public buckets, container images, Helm charts or ConfigMaps.
- **Only through a private channel** chosen by the game owner, to a team that will
  run the game.
- **Locally, only outside any Git working tree.** Default: `~/voice-id-audio/`.

## What seed data is

A **seed pack** is a zip (`voice-id-seedpack-<YYYYMMDD-HHMM>.seedpack.zip`):

```text
manifest.json     format "voice-id-seedpack", version 1, exported_at, app_version,
                  clips:    [{filename, is_ai, language, source, speaker, content_type, sha256, bytes, file}]
                  speakers: [{name, display_name, photo_file, photo_type}]
clips/...         the audio files
speakers/...      speaker photos (only of people who agreed)
```

It contains **no** players, emails, games, leaderboard or statistics. Those exist
only in the running game and in full backups.

## I want to... → use...

| I want to... | Use |
|---|---|
| Seed a fresh install | Seed pack import, mode `skip` ([below](#seed-a-fresh-install)) |
| Refresh clips on a running booth | Import a newer seed pack: `skip` adds new clips only; `replace` also overwrites changed clips |
| Hand the clips to another team | Export a seed pack, send it through a private channel |
| Add new voices | Clone Studio recordings → `import_from_clone_studio.py` → export a fresh seed pack |
| Retrain the Gary 2.0 detector | `import_from_clone_studio.py --out-dir ~/voice-id-audio/training/...` (or unpack a seed pack) |
| Look at the files locally | `seedpack.py unpack` → folder; `seedpack.py pack` → back to a seed pack |
| Recover after losing the PVC | Seed pack for clips and speakers; full backup for results ([backups](customization/backups.md)) |
| Load one clip by hand | `/api/admin/upload` (low level) |

## Get a seed pack

Ask the Voice ID game owner. You receive a `.seedpack.zip` through a private
channel. Store it in `~/voice-id-audio/seedpacks/` with permissions for you only
(`chmod 600`). Do not forward it.

## Seed a fresh install

Prerequisite: the game is installed ([INSTALLATION.md](INSTALLATION.md), step 4) and
you know the admin password.

**Admin UI:** open `https://<route>/admin`, log in, section **Seed pack & backup** →
**Import seed pack**, choose the file and the mode.

**Headless:**

```bash
export KUBECONFIG=~/.kube/<cluster>.kubeconfig         # dedicated file per cluster
ROUTE=$(oc get route voice-id -n voice-id -o jsonpath='{.spec.host}')
read -r -s -p 'Admin password: ' VOICEID_ADMIN_TOKEN; printf '\n'; export VOICEID_ADMIN_TOKEN

python3 apps/voice-id/tools/seedpack.py import \
  --game "https://${ROUTE}" --file ~/voice-id-audio/seedpacks/<pack>.seedpack.zip --mode skip
```

| Mode | Same filename, same content (SHA-256) | Same filename, different content | New filename |
|---|---|---|---|
| `skip` (default) | skipped | **conflict**, left untouched | added |
| `replace` | skipped | replaced in place (clip id kept) | added |

Import is **all or nothing**: the server validates the whole pack (zip, format and
version, SHA-256 of every clip, real audio content, metadata, max 1 GB) before one
write transaction. A rejected pack writes nothing and reports why. Importing the
same pack twice adds nothing. The response counts clips added, skipped, conflicts,
replaced and invalid, and speakers added, updated, skipped.

**Verify:**

```bash
curl -sk -H "Authorization: Bearer ${VOICEID_ADMIN_TOKEN}" "https://${ROUTE}/api/admin/clips" | python3 -c '
import json, sys, collections
c = json.load(sys.stdin)
print(len(c), "clips"); print(collections.Counter((x["language"], "AI" if x["is_ai"] else "human") for x in c))'
```

Then play one test game in the browser (each game needs at least `game.size`
clips including one AI and one human clip), check **Speakers** on `/admin` for
display names and photos, and reset scores afterwards.

## Export a seed pack (share or back up)

**Admin UI:** `/admin` → **Seed pack & backup** → **Export seed pack**.

**Headless:**

```bash
python3 apps/voice-id/tools/seedpack.py export --game "https://${ROUTE}"
# default output: ~/voice-id-audio/seedpacks/   (or --out DIR)
```

The tool refuses to write inside a Git working tree unless you pass `--force`;
don't. Export after every change to the clip set and before tearing a cluster down.

**Full backup** (everything, including player nicknames and emails; disaster
recovery only, never distribute):

```bash
umask 077; mkdir -p ~/voice-id-audio/backups
curl -sk -H "Authorization: Bearer ${VOICEID_ADMIN_TOKEN}" \
  "https://${ROUTE}/api/admin/backup" -o ~/voice-id-audio/backups/voiceid-$(date +%Y%m%d-%H%M).db
```

It is a consistent SQLite snapshot made by the app, safe while players are
active. The export endpoints also accept `?token=`; prefer the header, because a
token in a URL ends up in browser history and router logs.

## Work with files locally

For people who prefer folders over zips. Both commands work offline (no server
needed). `unpack` refuses a non-ignored Git working tree and will not overwrite
differing files; `pack` refuses to write inside a Git working tree. Both accept
`--force`; don't use it for audio inside the repository. The folder may omit
`speakers/` and `manifest.json` when you build one by hand.

```bash
# seed pack -> folder
python3 apps/voice-id/tools/seedpack.py unpack \
  --file ~/voice-id-audio/seedpacks/<pack>.seedpack.zip --out ~/voice-id-audio/samples/

# folder -> seed pack (then import it)
python3 apps/voice-id/tools/seedpack.py pack --dir ~/voice-id-audio/samples/ --out ~/voice-id-audio/seedpacks/
```

Folder layout and filename convention:

```text
~/voice-id-audio/samples/
  ai/<lang>/<model>-<speaker>-<description>.<ext>     e.g. ai/cs/fish-anton-devops.wav
  natural/<lang>/<speaker>-<description>.<ext>        e.g. natural/en/miska-en-1.m4a
  speakers/<name>.<ext>                               photo, e.g. speakers/anton.jpg
  manifest.json
```

- `<lang>`: `cs`, `sk` or `en`.
- `<model>` maps to the display name shown in the reveal: `fish` → Fish Audio S2 Pro,
  `xtts` / `coqui` → Coqui XTTS v2, `moss` → MOSS-TTS-Nano, `qwen` → Qwen3-TTS,
  `higgs` → Higgs Audio v3 (same mapping as `apps/voice-id/load_samples.py`).
- `<speaker>`: lower-case key. For AI clips, the person who was cloned (or the
  built-in voice name). A speaker with at least one natural clip counts as a real
  person, so their AI clips are revealed as "clone of <speaker>".
- Accepted audio: wav, mp3, m4a, ogg, flac, webm. Non-WAV is transcoded to WAV by
  the game.

The default location is outside the repository. Any `samples/` folder inside the
repository is git-ignored, but don't rely on that: keep audio out of the tree.

## Grow the collection

1. A speaker records the fixed sentences in Voice Clone Studio's sentence mode
   (`cs`, `sk`, `en`; 15 sentences each), and optionally a ~30 s cloning reference so
   AI versions of the same sentences can be generated. Longer references add nothing.
2. Import into the game, dry run first:

```bash
STUDIO=https://$(oc get route voice-clone-studio -n voice-clone-studio -o jsonpath='{.spec.host}')
python3 apps/voice-id/tools/import_from_clone_studio.py --studio "$STUDIO" --game "https://${ROUTE}"
python3 apps/voice-id/tools/import_from_clone_studio.py --studio "$STUDIO" --game "https://${ROUTE}" \
  --with-ai --providers xtts,fish --apply
```

3. Optional training export for the detector:
   `--out-dir ~/voice-id-audio/training/<date>` (WAVs + `manifest.json`).
4. Export a fresh seed pack and share it privately.

Details (filters, idempotency, re-recorded sentences, Fish warm-up):
[clips and speakers](customization/clips-and-speakers.md#import-clone-studio-recordings-into-the-game).

## Low-level fallback

`POST /api/admin/upload` adds one clip (multipart `audio`, `is_ai`, `language`,
`source`, `speaker`, `filename`). It needs the admin token
(`Authorization: Bearer`) and does not deduplicate.
A bulk loop over a local database is in
[clips and speakers](customization/clips-and-speakers.md#low-level-bulk-upload-from-a-local-database).
**Never `oc cp` a database into or out of the pod**: SQLite in WAL mode corrupts.

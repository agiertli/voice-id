# Add clips and speakers

> **Audio never goes to Git.** Voice recordings of real people, generated clones
> and the SQLite databases (which contain audio BLOBs and player emails) must
> never be committed to any repository, public or private. Audio lives only in
> the cluster databases and in local export directories **outside** the
> repository, by default `~/voice-id-audio/`. Never paste or attach audio to
> issues or pull requests. The repository enforces this with `.gitignore`
> patterns and a pre-commit hook (`.githooks/pre-commit`); enable it once per
> clone with `git config core.hooksPath .githooks`. Before any commit, check that
> `git status` lists no audio or database files.

The game plays audio clips stored in the SQLite database on the `voice-id-data`
PVC (`clips` table). Clips are **not** in the container image and **not** in Git.
A fresh install has no clips. Sources, in order of preference:

1. **A seed pack** (zip with `manifest.json`, clips and speaker profiles) received
   privately from the game owner. Import it on `/admin` → **Clips & seed packs**, or
   with `apps/voice-id/tools/seedpack.py import`. This is the standard fresh-install
   path; see [SEEDING.md](../SEEDING.md).
2. **New recordings** from Voice Clone Studio, imported with
   `import_from_clone_studio.py` (below).
3. **Low level:** the curated local database or the git-ignored `samples/` folder
   on the owner's workstation, uploaded clip by clip through `/api/admin/upload`.
   The 2026-11-24 install used this to load 86 clips (20 human, 66 AI).

**Never `oc cp` a database into or out of the pod.** SQLite runs in WAL mode;
copying the file under a running app corrupts it. Every procedure here uses the
HTTP API.

## Speaker identity: one person = one key (hard rule)

- Every person has exactly **one speaker key: their lowercase first name** (`jakub`,
  `miska`). Never a surname, login, nickname or spelling variant (`jveverka`, `kuba`,
  `Jakub`). Use the same key for their human clips, AI clones, photo and Clone Studio name.
  If two people share a first name, add the surname initial (`jakubv`) and use it everywhere.
- **Why:** the game links a clone to a real person only through the key. On 2026-11 a
  booth had 13 AI clones under `jakub` and his 10 real recordings under `jveverka`;
  players never saw "AI clone of Jakub" and the pair was useless.
- **After every import** (seed pack, Clone Studio, manual upload) open `/admin` →
  **Speakers** and resolve every warning before the booth opens:
  - *AI clips, no real recordings*: merge the alias into the first-name key
    (**Merge `jveverka` → `jakub`**), or tick **AI-only voice** for a synthetic voice with
    no real person. Coqui XTTS stock voices (claribel, daisy, gracie, filip, ...) are
    recognised automatically when all their clips are XTTS.
  - *Real recordings, no AI clips, looks like an alias*: merge it.
- **Merge or rename** (`/admin` → Speakers): moves every clip and the profile in one
  transaction; the target keeps its display name and photo when set. Typing a new key renames.

```bash
AUTH="Authorization: Bearer ${VOICEID_ADMIN_TOKEN}"
curl -sk -H "$AUTH" "https://${ROUTE}/api/admin/speakers/integrity" | python3 -m json.tool
curl -sk -H "$AUTH" -H 'Content-Type: application/json' -X POST \
  "https://${ROUTE}/api/admin/speakers/merge" -d '{"from": "jveverka", "to": "jakub"}'
curl -sk -H "$AUTH" -H 'Content-Type: application/json' -X PUT \
  "https://${ROUTE}/api/admin/speakers/robot" -d '{"ai_only": true}'
```

`seedpack.py pack` refuses to build a pack with an unlinked AI-clone speaker (use
`--alias OLD=NEW` or `--allow-ai-only NAME`); `seedpack.py import`/`export` and
`import_from_clone_studio.py` print the same integrity report. See
[SEEDING.md](../SEEDING.md#create-a-seed-pack-copy-paste).

## What makes a good clip set

- **Same sentences for human and AI clips.** If humans say one thing and AI
  another, content gives the answer away. Clone Studio's sentence mode and
  `apps/deepfake-detector/generate_synthetic_data.py` use the same 15 sentences per
  language for exactly this reason.
- **Clones of real people next to their real recordings.** A clone of someone
  who also has human clips is what makes the game interesting; the reveal tells
  the player "AI clone of <speaker>".
- **Balance.** The human/AI split per game is random (1–4 AI of 5), but every game
  has at least `game.minCloneClips` (default 1) clones of real people and at most
  `game.maxOtherAiClips` (default 1) other AI voices (XTTS stock, public-dataset
  voices). See [scoring: which clips a game contains](scoring.md#which-clips-a-game-contains).
  Keep enough human clips per language that games do not repeat them.
- **Colleague voices dominate.** Public-dataset voices are allowed, marked
  **AI-only voice**, but must stay a minority.
- **Short clips** (about 3 seconds). The speed bonus counts listening time.
- **Consent.** Only use voices of people who agreed to be in the game and, for
  photos, to be shown on the booth screen.

## Clip metadata

| Field | Meaning | Example |
|---|---|---|
| `is_ai` | `1` AI-generated, `0` human | `1` |
| `language` | `cs`, `sk` or `en` (shown on the reveal; games mix languages) | `cs` |
| `source` | TTS model display name for AI clips; empty for human clips. Use the existing strings so reveals stay consistent | `Fish Audio S2 Pro`, `Coqui XTTS v2` |
| `speaker` | Speaker key: the person's lowercase first name, same for their human and AI clips ([rule](#speaker-identity-one-person--one-key-hard-rule)). For AI clips of a built-in voice: the voice name | `anton` |
| `filename` | Logical path; also used by the import tool to detect duplicates | `ai/cs/fish-anton-devops.wav` |

Non-WAV uploads (m4a, mp3, flac, ogg, webm) are transcoded to 44.1 kHz mono WAV by
ffmpeg on upload or first play.

## Upload one clip

```bash
export KUBECONFIG=~/.kube/<cluster>.kubeconfig     # dedicated file per cluster
ROUTE=$(oc get route voice-id -n voice-id -o jsonpath='{.spec.host}')
read -r -s -p 'Admin password: ' VOICEID_ADMIN_TOKEN; printf '\n'; export VOICEID_ADMIN_TOKEN
AUTH="Authorization: Bearer ${VOICEID_ADMIN_TOKEN}"       # all /api/admin/* endpoints need it

curl -sk -H "$AUTH" "https://${ROUTE}/api/admin/upload" \
  -F "audio=@$HOME/voice-id-audio/samples/ai/cs/fish-anton-devops.wav" \
  --form-string "is_ai=1" --form-string "language=cs" \
  --form-string "source=Fish Audio S2 Pro" --form-string "speaker=anton" \
  --form-string "filename=ai/cs/fish-anton-devops.wav"

curl -sk -H "$AUTH" "https://${ROUTE}/api/admin/clips" | python3 -m json.tool | head   # list
curl -sk -H "$AUTH" -X DELETE "https://${ROUTE}/api/admin/delete-clip/<clip_id>"      # delete
```

Deleting a clip does not touch games already played.

## Low-level bulk upload from a local database

Prefer a seed pack. This is how the 2026-11-24 install loaded 86 clips before
seed packs existed. It reads a local database read-only and posts every clip
through the API. The database must never be committed or shared through Git.

```bash
cat > /tmp/upload_clips.py <<'EOF'
import os, sqlite3, subprocess, sys, tempfile
db, route, token = sys.argv[1], sys.argv[2], os.environ["VOICEID_ADMIN_TOKEN"]
out = tempfile.mkdtemp()
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
ok = fail = 0
for cid, fn, is_ai, lang, src, spk, data, ct in con.execute(
        "select id, filename, is_ai, language, source, speaker, audio_data, content_type from clips"):
    p = os.path.join(out, cid + (os.path.splitext(fn)[1] or ".wav"))
    open(p, "wb").write(data)
    r = subprocess.run(["curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}",
        "-H", f"Authorization: Bearer {token}", f"https://{route}/api/admin/upload", "-F", f"audio=@{p};type={ct}",
        "--form-string", f"is_ai={is_ai}", "--form-string", f"language={lang}",
        "--form-string", f"source={src or ''}", "--form-string", f"speaker={spk or ''}",
        "--form-string", f"filename={fn}"], capture_output=True, text=True)
    if r.stdout.startswith("2"): ok += 1
    else: fail += 1; print("FAIL", r.stdout, fn)
print(f"uploaded={ok} failed={fail}")
EOF
python3 /tmp/upload_clips.py apps/voice-id/data/voiceid.db "$ROUTE"     # needs VOICEID_ADMIN_TOKEN exported
curl -sk -H "Authorization: Bearer ${VOICEID_ADMIN_TOKEN}" "https://${ROUTE}/api/admin/clips" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'
```

The upload endpoint does not deduplicate. Run it once against an empty game; a
second run doubles every clip.

## Bulk upload from a local samples folder

The repository's `samples/` folders were removed on purpose; keep sample folders
under `~/voice-id-audio/` instead (`seedpack.py unpack`/`pack` convert between a
folder and a seed pack, see [SEEDING.md](../SEEDING.md#work-with-files-locally)).
`apps/voice-id/load_samples.py` writes to a **local** database only (default
`data/voiceid.db`). Use it to build the curated database on your workstation,
then upload with the script above (or export a seed pack from a local game). Folder and filename convention:

```text
samples/
  ai/<lang>/{model}-{speaker}-{description}.{ext}     model: fish, xtts, moss, ...
  natural/<lang>/{speaker}-{description}.{ext}
```

The model prefix maps to the display name (`fish` → `Fish Audio S2 Pro`, `xtts` →
`Coqui XTTS v2`, `moss` → `MOSS-TTS-Nano`).

## Current collection

As of voice-id v3.3.0 (owner's figures): **109 clips**:

| Group | Clips | Speakers |
|---|---:|---|
| Human recordings of colleagues | 31 | anton, miska, jakub |
| AI clones of colleagues (Fish S2 Pro, XTTS v2) | 47 | anton, miska, jakub |
| XTTS v2 stock voices | 19 | built-in XTTS voices (auto-recognised as not real people) |
| XTTS clones of public-dataset voices | 12 | delta, echo, golf, hotel, kilo, mike (marked AI-only) |

Policy: public datasets are allowed, but colleague voices must dominate. The clip
mix rules above enforce that per game.

## Record new human voices with Voice Clone Studio

Voice Clone Studio (`apps/voice-clone-studio`, namespace `voice-clone-studio`,
v2.2.1) has a **sentence recording mode** (v2.1.0 and later). Since v2.2 it has a
light theme and a Slovak UI; the header's EN / CS / SK switch sets the page language,
while the separate **Recording language** control chooses which sentences are read.
Quiet microphones are normalised before saving, errors are shown inline next to the
control, the voice page has **Play recording** to check the cloning reference, and the
logo and the browser Back button navigate between pages. A speaker picks a language
(`cs`, `sk`, `en`) and reads up to 15 fixed sentences, one recording each.

- Recordings are loudness-normalised (quiet mics), converted to 44.1 kHz WAV with
  silence trimmed; anything under 0.5 s is rejected as empty.
- Re-recording a sentence replaces the previous take in Clone Studio.
- Speaker names are case-insensitive; use the same key as in the game
  (`anton`, not `Anton G.`). Display names are set separately (below).
- To create AI pairs, the speaker also records a ~30 second reference in Clone
  Studio's cloning mode, which registers the voice with XTTS v2 and Fish S2 Pro.
  Longer references do not improve the clone. A sentence recording linked to that
  cloned voice can get AI versions of the same sentence.

Clone Studio's API documentation is served at `/api/docs` on its route.

## Import Clone Studio recordings into the game

Use `apps/voice-id/tools/import_from_clone_studio.py` (Python 3 standard library
only). It talks only to the two HTTP APIs and never opens either database.
**Dry run is the default**; nothing is uploaded or generated until `--apply`.

```bash
STUDIO=https://$(oc get route voice-clone-studio -n voice-clone-studio -o jsonpath='{.spec.host}')
GAME=https://$(oc get route voice-id -n voice-id -o jsonpath='{.spec.host}')
read -r -s -p 'Game admin password: ' VOICEID_ADMIN_TOKEN; printf '\n'; export VOICEID_ADMIN_TOKEN
# The tool reads VOICEID_ADMIN_TOKEN (or --game-token) and sends it only to the game.

# 1. See what would happen
python3 apps/voice-id/tools/import_from_clone_studio.py --studio "$STUDIO" --game "$GAME"

# 2. Upload the human recordings of two speakers, Czech only
python3 apps/voice-id/tools/import_from_clone_studio.py --studio "$STUDIO" --game "$GAME" \
  --speaker anton --speaker miska --language cs --apply

# 3. Also create AI pairs (same sentence, cloned voice) with both TTS engines
python3 apps/voice-id/tools/import_from_clone_studio.py --studio "$STUDIO" --game "$GAME" \
  --with-ai --providers xtts,fish --apply

# 4. Export WAVs + manifest.json for detector training (works in dry run too).
#    Always export OUTSIDE the repository.
python3 apps/voice-id/tools/import_from_clone_studio.py --studio "$STUDIO" --game "$GAME" \
  --with-ai --out-dir ~/voice-id-audio/clone-studio-export
```

| Option | Effect |
|---|---|
| `--speaker NAME` | Only this speaker (repeatable, case-insensitive) |
| `--language cs\|sk\|en` | Only this language |
| `--with-ai` | Also create AI versions of each sentence. Reuses an existing Clone Studio clip with the same voice, text and provider; otherwise generates one (`--apply` only) |
| `--providers xtts,fish` | Engines for `--with-ai` (default both) |
| `--apply` | Really generate and upload. Without it the run is a dry run |
| `--out-dir DIR` | Save every WAV plus a merged `manifest.json` (label, speaker, language, provider, text). Use a path outside the repository, such as `~/voice-id-audio/...` |
| `--verify-tls` | Verify TLS certificates (off by default for demo clusters) |
| `--game-token TOKEN` | Game admin token (Helm `adminToken`), sent as Bearer to the game only. Default: env `VOICEID_ADMIN_TOKEN`. **Required for `--apply`**; a dry run without it cannot list existing game clips and shows everything as planned |

Behaviour to know:

- **Idempotent by filename.** Game filenames are `natural/<lang>/<speaker>-sNN.wav`
  and `ai/<lang>/<provider>-<speaker>-sNN.wav`. Anything already in the game is
  reported as `exists` and skipped.
- **Re-recorded sentences are skipped**, because the filename is unchanged. To
  replace one, delete the old game clip first (`/api/admin/delete-clip/<id>`),
  then re-run.
- **AI pairs need a linked cloned voice.** Recordings without one show `no-voice`.
- **Fish generation is slow:** 30–120 s per sentence in the worst case, about
  5–7 s per short sentence once warm, and up to ~5 minutes for the first call
  after a Fish restart (torch.compile). The tool waits up to 600 s per request.
- **Fish voices live in memory.** After a Fish pod restart, Clone Studio
  re-registers stored voices when Clone Studio itself starts. If Fish restarted
  but Clone Studio did not, restart Clone Studio
  (`oc rollout restart deploy/voice-clone-studio -n voice-clone-studio`) before
  generating.
- **Speaker keys come from the Clone Studio speaker name**, lowercased. Make sure the
  person entered their first name there; otherwise fix it with a merge afterwards.
- The run ends with a **speaker integrity report** (dry run: the projected state after
  `--apply`). Resolve warnings on `/admin` → Speakers before the booth opens.
- Exit code is 1 if any item failed, 2 if the APIs could not be reached.

After adding a new speaker or engine, Gary 2.0 has never heard it. Expect its
accuracy on those clips to differ and consider retraining
([AI opponents](ai-opponents.md#retraining-gary-20)).

## Feed new clips to detector retraining

Gary 2.0 only knows the voices and engines it was trained on. To retrain:

1. Export a labelled set outside the repository:
   `import_from_clone_studio.py ... --with-ai --out-dir ~/voice-id-audio/training/<date>`
   (WAVs plus `manifest.json` with label, speaker, language, provider and text).
2. Follow `apps/deepfake-detector/FINDINGS.md` and `experiment_v4.py` (V4 recipe:
   16 kHz mono, RMS-normalised, amplitude-only augmentation; about 11 minutes on an
   Apple M3). Keep a speaker held out to measure generalisation.
3. Build a new `quay.io/agiertli/deepfake-detector:<tag>`, update
   `infra/deepfake-detector/manifests.yaml`, re-measure accuracy and false positive
   rate on real voices, and update the opponent texts if the numbers change.

Training data stays outside Git like every other audio file.

## Speaker display names and photos

The reveal after each guess can show a speaker's display name and photo
("AI clone of Anton G."). Profiles are keyed by the lower-case speaker name and
managed on `/admin` → Speakers (also merge/rename and the AI-only flag, see
[speaker identity](#speaker-identity-one-person--one-key-hard-rule)), or by API with the admin token:

```bash
TOKEN=...   # the admin password, see booth-operations.md
curl -sk -X PUT "https://${ROUTE}/api/admin/speakers/anton" \
  -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
  -d '{"display_name": "Anton G."}'
curl -sk -X POST "https://${ROUTE}/api/admin/speakers/anton/photo" \
  -H "Authorization: Bearer ${TOKEN}" -F "photo=@anton.jpg"
curl -sk -X DELETE "https://${ROUTE}/api/admin/speakers/anton/photo" \
  -H "Authorization: Bearer ${TOKEN}"
curl -sk "https://${ROUTE}/api/speakers"       # public list
```

Photos: JPEG, PNG or WebP, up to 5 MB; downscaled to 512 px and stripped of EXIF
(GPS) data on upload. A speaker counts as a "real person" when at least one human
clip exists for them, which changes the AI reveal wording to "clone of".
**Only upload photos of people who explicitly agreed to be shown on the booth
screen.** Photos are public at `/api/speakers/<name>/photo`.

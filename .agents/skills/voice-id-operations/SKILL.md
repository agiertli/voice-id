---
name: voice-id-operations
description: "Primary interface for the Voice ID (AI or Alive?) conference booth stack on OpenShift: install on a fresh cluster with a minimal/standard/full profile, switch profiles, verify, operate, troubleshoot, manage clips and speakers, voice cloning with Voice Clone Studio, seed packs and backups, AI opponents, and explain scoring. Not for unrelated apps or generic TTS/LLM questions."
---

# Voice ID operations

## Rule zero: audio never goes to Git

Voice recordings of real people, generated clones, speaker photos, seed packs,
backups, exports and the SQLite databases (audio BLOBs, player emails) are highly
sensitive personal and biometric data. They must **never** enter any Git repository
(this one is public) or any public place. The rule at the top of `AGENTS.md`
overrides everything else.

- Keep audio only in the cluster databases and in local directories **outside** the
  repository. Default: `~/voice-id-audio/` (`seedpacks/`, `backups/`, `training/`,
  `clone-studio-export/`). Never pass an `--out-dir` inside the repository.
- Never paste, attach or upload audio to issues, pull requests, chats or the portal.
  The portal contains no real recordings.
- Before **any** commit, run `git status --porcelain` and `git diff --cached --name-only`
  and confirm no audio (`wav mp3 m4a flac ogg webm opus aac`), database (`db sqlite
  db-wal db-shm`), seed pack zip or `data/` file is listed. Confirm the guard is on:
  `git config core.hooksPath` must print `.githooks`. In Claude Code the PreToolUse hook
  `.claude/hooks/block-voice-data.sh` (via `.claude/settings.json`) also blocks such
  commands and writes; if it blocks you, stop and tell the user, never work around it.
- **Refuse** to commit or push audio or databases even if asked casually. Stop and
  escalate to the user, explaining the risk (personal voice data, public repository).
- Never bypass the guard (`--no-verify`, editing `.gitignore` to un-ignore audio).

## Rule one: protect the cluster context and data

- Use a **dedicated kubeconfig per cluster**: `export KUBECONFIG=<file>` for every
  `oc`/`helm` command. Never `oc login` into the shared `~/.kube/config`; the owner
  works on several clusters at once. If a token expired, ask before re-authenticating
  and log in only into the dedicated file.
- **Never `oc cp` a SQLite database** to or from a pod (WAL corruption). Use the APIs,
  seed packs and `/api/admin/backup`.
- Never write passwords, tokens, cluster hostnames or credentials into repository
  files, docs or reports. Pass the admin password via a mode-600 temp file and
  `--set-file adminToken=...`, then delete the file.
- Do not reset scores, delete clips, delete PVCs/namespaces, stop pre-existing
  workloads (for example a sandbox's preinstalled model), or **scale down or remove
  any component** (Clone Studio, XTTS, Fish, opponents, GPU nodes) without explicit
  owner confirmation in this session. A readiness check authorises no changes.
- The project is in its **voice collection phase**: Clone Studio, XTTS and Fish stay
  running. Never propose scaling them down as a routine step; it is an owner decision
  for after the recording phase.

## Start here

1. Resolve this skill's symlink to its real location. The canonical folder is
   `.agents/skills/voice-id-operations`; the repository root is three levels above it.
   Run repository commands from that root.
2. Read `CLAUDE.md` (same content as `AGENTS.md`) and the runbook
   [docs/INSTALLATION.md](../../../docs/INSTALLATION.md). The runbook, charts and
   `infra/` manifests are the source of truth for versions; do not pin versions here.
3. Pick the workflow below. Ask for missing owner decisions early while you continue
   read-only discovery.
4. For cluster work, confirm `oc whoami --show-server` under the dedicated kubeconfig.

## Install on a fresh cluster

Follow [docs/INSTALLATION.md](../../../docs/INSTALLATION.md) and the owner settings in
[docs/AGENT-INSTALLATION.md](../../../docs/AGENT-INSTALLATION.md).

**First, ask which profile** (default `standard`) and show this table. Provision
only what the chosen profile needs.

| Profile | Components | GPUs (`g6.2xlarge`, 1× L4) | Opponents | Approx. GPU cost |
|---|---|---:|---|---|
| minimal | game + Gary + Gary 2.0 (CPU) | 0 | Gary, Gary 2.0 | none |
| standard (recommended for booths) | minimal + Teddy (qwen2-audio) + Iris (gemma) | 2 | all four | ≈ $2/h |
| full (voice collection, cloning) | standard + xtts-v2 + fish-native + voice-clone-studio | 4 | all four | ≈ $4/h |

About $1/h per `g6.2xlarge` on demand; approximate, tell the owner to check current
AWS pricing. Helm flags: minimal = `aiJudge.enabled=false`, `aiJudge2.enabled=false`;
standard/full = both true. `detector`/`detectorBase` true in all profiles. Opponent
lists in the API, game picker and `/tv` (refresh 10 s) follow these flags automatically.

Preferred path: `./install.sh --profile <p> --kubeconfig <file> --admin-token-file
<file> --dry-run`, show the plan, then run it without `--dry-run` (add `--yes` only
after the owner confirmed). It never runs `oc login`, clones the worker MachineSet
with replicas = profile GPU count (none for minimal), labels it
`voice-id/gpu-machineset=true`, never scales it down, and prints URLs plus the seed
pack command. It is **untested on a live cluster**: watch each step and fall back to
the manual runbook steps if it stops.

Manual order (target ~35 min for full, minutes for minimal):

| t | Action |
|---|---|
| 0 | standard/full: clone the cluster's **own** worker MachineSet into a GPU MachineSet: `g6.2xlarge`, replicas 2 (standard) or 4 (full), taint `nvidia.com/gpu=true:NoSchedule`, labels `node-role.kubernetes.io/gpu-worker` and `voice-id/gpu-machineset=true`, 200 GiB root. `infra/gpu-machineset.yaml` is only an example from another cluster. minimal: skip. |
| 0 | `oc new-project voice-id`; apply `infra/deepfake-detector-base`, `infra/deepfake-detector`; standard/full also `infra/qwen2-audio`, `infra/gemma` (download Jobs start on CPU). |
| 0 | `helm upgrade --install voice-id apps/voice-id/helm/voice-id -n voice-id -f <values> --set-file adminToken=<file>` plus the profile's `aiJudge*` flags; then import the seed pack. |
| ~5 min | GPU nodes Ready, driver up about 4 min after join. full: apply `infra/xtts`, `infra/fish-speech`, install Clone Studio. |
| ~15–30 min | vLLM loads; full: Fish downloads ~9 GB then needs a ~4 min compile on first request: warm it up. |
| end | Smoke test, report. |

Hard-won facts (do not rediscover):

- Schedule GPU pods on `nvidia.com/gpu.present: "true"`; custom MachineSet labels can
  be reset by the Machine controller. All GPU manifests tolerate `nvidia.com/gpu`.
- Demo sandboxes may have a preinstalled model on the control-plane GPU; don't count
  on that GPU and don't stop it without approval.
- LLMInferenceService shows `GatewayPreconditionNotMet` without **Red Hat
  Connectivity Link** (AuthPolicy CRD). Expected. The vLLM workload runs; the game
  uses `https://<name>-kserve-workload-svc.voice-id.svc.cluster.local:8000/v1` with
  `verify=False`. Connectivity Link is the supported path for external exposure.
- vLLM args in LLMInferenceService lose JSON quotes: use `--limit-mm-per-prompt.image=1`.
  Set `spec.model.name` instead of `--served-model-name` (both = duplicate flag).
  `aiJudge.model` / `aiJudge2.model` must equal `/v1/models` ids (`gemma-4-12b`,
  `Qwen2-Audio-7B-Instruct`).
- Gemma must be `RedHatAI/gemma-4-12B-it-FP8-dynamic` (12.75 GiB, fits one L4).
- Changing a single-GPU LLMInferenceService deadlocks the rolling update (new pod
  Pending): scale the old ReplicaSet to 0.
- Detectors need the 4 CPU limit (~5 s/clip; 2 CPU was ~20 s).
- XTTS needs `COQUI_TOS_AGREED=1`, `HOME=/tmp`, `TMPDIR=/tmp`, `NUMBA_CACHE_DIR`,
  `MPLCONFIGDIR`, `workingDir: /tmp`. `/tts` returns base64 WAV in JSON quotes.
- Fish: first request ~4 min torch.compile, then ~5–7 s per short sentence (30–120 s
  long texts). Warm up from inside the cluster (route timeout 300 s):
  `oc exec deploy/voice-id -n voice-id -- python3 -c "import requests;print(requests.post('http://fish-native:8080/v1/tts',json={'text':'Dobrý den.','format':'wav'},timeout=900).status_code)"`.
  Fish references live in memory; after a Fish restart, restart Clone Studio so it
  re-registers voices.
- Helm: an upgrade without `-f`/`--set` reuses previous values; use `-f <values>` or
  `--reset-values`, and always pass `adminToken` again (else the public image default
  returns).
- Clips: **seed pack** import (admin UI tab "Clips & seed packs" or
  `python3 apps/voice-id/tools/seedpack.py import --game URL --token T --file PACK --mode skip`).
  Low-level fallback: loop over `POST /api/admin/upload` (86 clips at first install).
  Upload does not deduplicate. All `/api/admin/*` calls need
  `-H "Authorization: Bearer ${VOICEID_ADMIN_TOKEN}"`.
- Opponents are listed whenever their URL is set, healthy or not; failed analyses
  become server-recorded coin flips (`fallback: true`). Verify each before opening;
  disable unhealthy ones via Helm.

Smoke test: runbook section "6. Verify" (clip count, `/api/battle/opponents`, every
opponent analyses with `fallback: false` in its own game, one game). Analysis
responses are sealed (no `guess`); the answer comes with `/guess`. Report URLs (game, `/tv`, `/admin`, Clone
Studio), versions, verified items, owner browser checks still needed. Do not print the
admin password unless the owner explicitly asks for the handoff.

## Switch profiles, add or remove Clone Studio

Guide: [INSTALLATION.md#switch-profiles](../../../docs/INSTALLATION.md#switch-profiles).
Every scale-down or removal needs **explicit owner confirmation**; restate what will
stop, what persists and the GPU saving, then wait for "yes".

- Scale up: re-run `./install.sh --profile <bigger>` (scales the labelled MachineSet
  up, applies missing manifests, upgrades Helm flags). Re-adding Clone Studio:
  MachineSet to 4, `oc scale deploy/xtts-v2 deploy/fish-native -n voice-id --replicas=1`,
  warm Fish, then `oc scale deploy/voice-clone-studio -n voice-clone-studio --replicas=1`
  (it re-registers stored voices with XTTS/Fish on startup; recordings and voices
  persist on its PVC; Fish weights persist on `fish-data`).
- Remove Clone Studio (only after the recording phase, owner-confirmed): scale
  Clone Studio, then XTTS/Fish to 0, annotate the machines that hosted XTTS/Fish with
  `machine.openshift.io/delete-machine=true`, then scale the MachineSet to 2. Never
  scale the MachineSet down first (a node running Teddy or Iris could be removed).
- standard → minimal: `helm upgrade` with `aiJudge.enabled=false aiJudge2.enabled=false`
  first, then delete the two LLMInferenceServices (weights PVCs stay), then the
  MachineSet to 0.

## Operate the booth

Guide: [booth-operations.md](../../../docs/customization/booth-operations.md).

- Screens: `/` game, `/tv` top 10 refreshing every 10 s, `/admin` (Bearer token):
  tabs Players (history, Export CSV), Speakers (names, photos, merge/rename, AI-only flag,
  integrity banner), Clips & seed packs (counts, opponent cache status + Warm now,
  export/import), Danger zone (Reset all
  scores, full backup). Tab is in the URL hash (`/admin#speakers`).
- Admin password: Helm value `adminToken` (env `ADMIN_TOKEN`); empty = public image
  default. Override per event.
- Every `/api/admin/*` endpoint (incl. clips, upload, delete-clip, `POST
  /api/admin/reset`, spectrogram) needs the admin token. Known risks to state, not
  fix: one shared token whose image default is public; a scripted client can listen
  to the public clip audio before starting the speed clock; Clone Studio's API is
  unauthenticated.
- Daily: full backup + CSV before any reset; reset only with owner approval.

## Clips, speakers and voice cloning

Guides: [clips-and-speakers.md](../../../docs/customization/clips-and-speakers.md),
[backups.md](../../../docs/customization/backups.md).

**Hard rule: one person = one speaker key** = lowercase first name (`jakub`), never a
surname/login/nickname variant (`jveverka`, `kuba`); same key for human clips, AI clones,
photo and Clone Studio name. A split key means the game never links clone and real person
(real incident: 13 `jakub` clones vs 10 `jveverka` recordings). After ANY import (seed pack,
Clone Studio, upload) run the integrity check and resolve every warning before the booth
opens: `GET /api/admin/speakers/integrity` or the red banner on `/admin#speakers`; fix with
**Merge** (`POST /api/admin/speakers/merge {"from":"jveverka","to":"jakub"}`, also renames
when `to` is new) or, only for a synthetic voice with no real person, **AI-only voice**
(`PUT /api/admin/speakers/<name> {"ai_only": true}`). XTTS stock voices are auto-recognised.
Never mark a real person AI-only to silence the warning; merges need owner consent.

- Game clip API (admin token, `Authorization: Bearer`): `GET /api/admin/clips`,
  `POST /api/admin/upload` (multipart `audio`, `is_ai`, `language`, `source` =
  `Fish Audio S2 Pro` | `Coqui XTTS v2`, `speaker` lower case, `filename`),
  `DELETE /api/admin/delete-clip/<id>`. Use `--form-string` for text fields.
- Voice Clone Studio (`apps/voice-clone-studio`, namespace `voice-clone-studio`):
  - Cloning: a **~30 s reference** is enough for XTTS and Fish; longer adds nothing.
    `POST /api/voices` (multipart `audio`, `name`) registers with both engines;
    `GET /api/voices`; `POST /api/generate {voice_id, text, provider_id: xtts|fish, language}`;
    `GET /api/clips[?voice_id=]`, `/api/clips/<id>/audio`.
  - Sentence mode: `GET /api/sentences?lang=cs|sk|en` (15 fixed sentences, the same as
    the AI clips); `POST /api/recordings` (`audio`, `speaker`, `language`,
    `sentence_idx`, optional `voice_id`); `GET /api/recordings`,
    `/api/recordings/<id>/audio`. Re-recording replaces the take.
  - API docs at `/api/docs`; keep its OpenAPI spec in sync when endpoints change.
- Import into the game: `python3 apps/voice-id/tools/import_from_clone_studio.py
  --studio URL --game URL [--speaker X] [--language cs] [--with-ai --providers xtts,fish]
  [--out-dir ~/voice-id-audio/...] [--apply]`. Always dry run first, show the summary,
  then `--apply` with owner consent. Idempotent by filename
  (`natural/<lang>/<speaker>-sNN.wav`, `ai/<lang>/<provider>-<speaker>-sNN.wav`);
  re-recorded sentences are skipped until the old game clip is deleted. `no-voice` =
  recording has no linked cloned voice. Warm Fish first. Speaker key = Clone Studio
  speaker name lowercased: check it is the first name. The run ends with a speaker
  integrity report (dry run: projected state); resolve warnings before `--apply`/opening.
- Speakers: `PUT /api/admin/speakers/<name> {"display_name", "ai_only"}`, `POST|DELETE
  /api/admin/speakers/<name>/photo`, `POST /api/admin/speakers/merge {from, to}`,
  `GET /api/admin/speakers/integrity` (token). Only photos of people who agreed.
- Retraining Gary 2.0 after new speakers/engines: export with `--out-dir
  ~/voice-id-audio/training/<date>`, follow `apps/deepfake-detector/FINDINGS.md` and
  `experiment_v4.py`, new image tag, update `infra/deepfake-detector/manifests.yaml`,
  re-measure accuracy and false positives on real voices.

## Create a seed pack (task)

Trigger: "create a seed pack", "make a seedpack", "package the clips", "I want to share the clips".
A seed pack is ONE file `voice-id-seedpack-<YYYYMMDD-HHMM>.seedpack.zip`. Never zip files by
hand; the tool builds and validates it.

1. **Ask the source** (one question, default A if the user says "my samples/files"):
   - A) **A local folder** of audio files (e.g. `apps/voice-id/samples/`, which is git-ignored, or `~/voice-id-audio/samples/`).
   - B) **The running game** (exactly the clips it plays now, incl. speaker names/photos).
2. **A — from a local folder:**
   - Check the layout first and list problems (don't rename the user's files without asking):
     `natural/<cs|sk|en>/<speaker>-<desc>.<ext>`, `ai/<cs|sk|en>/<model>-<speaker>-<desc>.<ext>`
     (`<model>`: `fish` → "Fish Audio S2 Pro", `xtts` → "Coqui XTTS v2"), optional `speakers/<speaker>.jpg|png|webp`,
     optional `manifest.json` (takes precedence over filenames). Audio: wav, mp3, m4a, flac, ogg.
   - **Speaker keys**: one person = one key = lowercase first name, identical in `natural/`, `ai/`
     and `speakers/`. List every speaker with human/AI counts and flag surname/nickname variants
     or AI-only speakers before packing.
   - Run (from the repo root):
     ```bash
     python3 apps/voice-id/tools/seedpack.py pack --dir <folder>
     ```
     Output goes to `~/voice-id-audio/seedpacks/` (outside Git; the tool refuses a Git tree unless the target is git-ignored).
     Exit code 1 = some files were skipped: show the skipped list and why.
     Exit code 4 = **refused: AI-clone speaker without real recordings** (integrity report printed,
     no pack written). Never work around it blindly: ask the user which key is the person's first
     name, then re-run with `--alias OLD=NEW` (repeatable, e.g. `--alias jveverka=jakub`), or, only if
     the user confirms it is a synthetic voice with no real person, `--allow-ai-only NAME` (stored in
     the pack as `ai_only`). XTTS stock voices (claribel, daisy, ...) pass automatically.
3. **B — from the running game** (needs the cluster's route and admin token; dedicated KUBECONFIG, never `oc login`):
   ```bash
   ROUTE=$(oc get route voice-id -n voice-id -o jsonpath='{.spec.host}')
   python3 apps/voice-id/tools/seedpack.py export --game "https://${ROUTE}" --token "$VOICEID_ADMIN_TOKEN"
   ```
   Or tell the user: `/admin` → **Clips & seed packs** → **Export seed pack**.
   `export` prints the live speaker integrity report first; if it shows warnings, resolve them
   in `/admin#speakers` (merge / AI-only) and export again rather than shipping a broken pack.
4. **Report**: full path of the zip, size, number of clips (human/AI per language), speaker profiles,
   the integrity result (aliases applied, AI-only voices),
   skipped files, and — for source A — how it differs from the live game's clip list if a cluster is available
   (clips never played yet). Remind: contains real voices, Rule zero applies.
5. **Offer protection + sharing** (don't do it unasked): encrypt with `age -p -o <pack>.age <pack>`
   (passphrase via a different channel); share via a Red Hat Google Drive folder with named @redhat.com
   people only (never "anyone with the link"), set an access expiry; record version/recipient/date in the owner's
   private register (needed if someone withdraws consent). Never GitHub, Quay, public Slack, external email.
6. **Recipient side**: `/admin` → **Clips & seed packs** → **Import seed pack** (mode `skip`), or
   `seedpack.py import --game URL --token T --file PACK --mode skip` (prints the integrity report);
   resolve any speaker warning on `/admin#speakers`; then delete the zip.

## Loudness standard

All game clips: -20 LUFS integrated, true peak <= -1.5 dBTP (linear gain, true-peak limiter only if needed).
Before importing new clips from any source other than Clone Studio sentences (which are already levelled to -20),
normalize them the same way and verify with ffmpeg `loudnorm=print_format=json` (all within ±1 LU). Never ship
mixed loudness: it is an audible AI/human giveaway.

## Seeding (clips into the game)

Guide: [docs/SEEDING.md](../../../docs/SEEDING.md). Seed pack =
`*.seedpack.zip` with `manifest.json` + `clips/` + `speakers/`; no players, emails or
results. Sensitive: private channel only, stored in `~/voice-id-audio/seedpacks/`
(mode 600), never in Git or anywhere public. The repo's `samples/` folders were
removed on purpose.

Fresh install (deploy → `/admin` → import → verify):

```bash
ROUTE=$(oc get route voice-id -n voice-id -o jsonpath='{.spec.host}')
read -r -s -p 'Admin password: ' VOICEID_ADMIN_TOKEN; printf '\n'; export VOICEID_ADMIN_TOKEN
python3 apps/voice-id/tools/seedpack.py import --game "https://${ROUTE}" \
  --file ~/voice-id-audio/seedpacks/<pack>.seedpack.zip --mode skip
curl -sk -H "Authorization: Bearer ${VOICEID_ADMIN_TOKEN}" "https://${ROUTE}/api/admin/clips" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'
```

Or admin UI: `/admin` → **Clips & seed packs** → **Import seed pack**. Modes: `skip`
(default; same filename + different content = conflict, untouched) or `replace`
(overwrite in place). Idempotent (filename + SHA-256), all-or-nothing validation.
Then **resolve speaker integrity warnings** (one person = one key, see "Clips, speakers and
voice cloning"; the import output and `/admin` banner list them), a browser test game and a
look at Speakers on `/admin`.

Export / back up:

```bash
python3 apps/voice-id/tools/seedpack.py export --game "https://${ROUTE}"     # -> ~/voice-id-audio/seedpacks/
umask 077; curl -sk -H "Authorization: Bearer ${VOICEID_ADMIN_TOKEN}" \
  "https://${ROUTE}/api/admin/backup" -o ~/voice-id-audio/backups/voiceid-$(date +%Y%m%d-%H%M).db
```

`export` refuses to write inside a Git tree; never use `--force`. The full backup
contains emails: operator disaster recovery only, never share. Results CSV:
`GET /api/admin/export`.

Local folder view (offline, no server): `seedpack.py unpack --file PACK --out ~/voice-id-audio/samples/` →
`ai/<lang>/<model>-<speaker>-<desc>.<ext>`, `natural/<lang>/<speaker>-<desc>.<ext>`,
`speakers/<name>.<ext>`, `manifest.json`; `seedpack.py pack --dir DIR --out
~/voice-id-audio/seedpacks/` → pack to import. Model prefixes: `fish` → Fish Audio S2
Pro, `xtts`/`coqui` → Coqui XTTS v2, `moss` → MOSS-TTS-Nano.

Grow: Clone Studio recordings → `import_from_clone_studio.py` (dry run, `--apply`,
`--with-ai`; `--apply` requires `--game-token` or env `VOICEID_ADMIN_TOKEN`) →
export a fresh seed pack. Low level: `POST /api/admin/upload` per clip
(no dedup). If a tool or subcommand is missing in the checkout, say so; never invent
options.

## AI opponents

Guide: [ai-opponents.md](../../../docs/customization/ai-opponents.md). **Only Iris
(Gemma) uses spectrograms.** Teddy `qwen` easy, raw audio, 24.4%; Iris `gemma` medium,
spectrogram image, 59.3%; Gary `gary-base` hard, Wav2Vec2 base, 67.4%; Gary 2.0
`gary-finetuned` boss, Wav2Vec2 V4-A, 91.9%. Detector `UNCERTAIN` maps to human.
Enable/disable with `aiJudge`, `aiJudge2`, `detectorBase`, `detector` values.

Answer cache: each opponent's answer per clip is stored in `opponent_cache`, valid only
while its audio sha256 matches `clips.audio_sha256` (upload, seed pack replace,
transcode and direct DB updates all invalidate; deleting a clip deletes its rows). A
background thread (one gunicorn worker at a time via a DB lease) pre-warms every missing
(opponent, clip) one request at a time per opponent, every `opponentCache.warmIntervalSec`
(env `OPPONENT_CACHE_WARM_INTERVAL_SEC`, default 600 s). A cache hit is recorded and sealed
exactly like a live model answer; coin-flip fallbacks are never cached. Without the cache,
"Preparing the game" took Teddy 4 s, Iris 8 s, Gary 8 s, Gary 2.0 23 s; warm it is well
under 1 s. Admin (token): `GET /api/admin/opponent-cache/status` (per opponent
cached/total, last run, errors), `POST .../warm`, `POST .../clear` (optional
`{"opponent": id}`; clear an opponent after retraining/swapping its model). Admin
page Clips tab shows the status line + Warm now. Off switch: `opponentCache.enabled=false`
(env `OPPONENT_CACHE_ENABLED`). After a fresh install or seed pack import, warming takes
about (clips × per-clip latency) per opponent, minutes for Gary 2.0 on CPU.

## Clip mix per game

Since v3.3.0 `select_game_clips` (app.py): the human/AI split is random (1–4 AI of 5;
a fixed split would let players deduce answers), but every game has at least
`game.minCloneClips` (env `GAME_MIN_CLONE_CLIPS`, default 1) AI clones of real people
(AI clips whose speaker has human recordings) and at most `game.maxOtherAiClips` (env
`GAME_MAX_OTHER_AI_CLIPS`, default 1) other AI voices (XTTS stock, public-dataset
voices marked AI-only). Too-small pools fall back to plain random. Helm only, no
rebuild. Collection at v3.3.0: 109 clips (31 human from colleagues anton/miska/jakub,
47 AI clones of colleagues, 19 XTTS stock, 12 XTTS clones of public-dataset voices
delta/echo/golf/hotel/kilo/mike marked AI-only). Owner stance: public datasets allowed,
colleague voices must dominate; keep that ratio when adding clips.

Clone Studio v2.2.x: light theme, SK UI, header EN/CS/SK = page language vs the
separate "Recording language" control, quiet-mic normalisation with inline errors,
"Play recording" on the voice page, clickable logo and browser Back navigation.

## Explain scoring

Guide: [scoring.md](../../../docs/customization/scoring.md). Re-read the constants at
the top of `apps/voice-id/src/app.py` (scoring is server-side; the browser only
displays) before answering; flag any drift.

- Correct answer = 100 + max(0, round(200 × (1 − t/10))), t = seconds the server
  measures from the first `POST /play` of the clip to its `/guess` (includes
  listening; never played = 10 s). Wrong = 0. 300 at 0 s, 240 at 3 s, 100 at ≥ 10 s.
  One guess per clip of the game (repeat 409, foreign clip 400).
- Battle bonus if the player beats the opponent's correct count, or ties with a perfect
  score: Teddy +50, Iris +100, Gary +150, Gary 2.0 +250. Opponent score = correct
  count, not points.
- Prize = server-counted correct answers ≥ `game.prizeThreshold` (default 4 of 5);
  points irrelevant. Leaderboard = best game per nickname by points, then correct;
  API top 20, TV top 10; ties beyond that unordered.
- `/complete` is idempotent, ignores client points and returns a breakdown
  (`correct`, `speed`, `opp_bonus`, `opponent`, `opponent_score`).
- Game time (leaderboard/TV column "Game time", `duration_sec`) = last answer minus
  first play press (wall clock, excludes loading/"Preparing the game", includes
  listening, thinking and reveals). No play timestamps -> sum of per-clip times.
  Informational only; speed bonus still uses each clip's own time.
- Known limitations: a scripted client can pre-listen (public, preloaded audio) and
  then play+guess instantly; a failed completion request is not retried by the
  browser; opponent failures become server-recorded coin flips.

## Diagnose an incident

Collect read-only evidence first: `oc get pods,llminferenceservice -n voice-id`,
events, `oc logs deploy/voice-id` (GAME_START, GUESS, AI_JUDGE, DETECTOR lines), GPU
allocatable on nodes, `oc adm top pods`. Use the runbook's troubleshooting table.
State evidence and hypothesis before a scoped fix; verify the original symptom.
Never delete PVCs, reset scores or reinstall as a retry.

## Check booth readiness

Record each item as verified, failed or untested with evidence and time: pods ready,
clip count, opponents answering (smoke test sends real inference but creates no
leaderboard entry), opponent cache warm (`GET /api/admin/opponent-cache/status`: every
opponent `cached == total`, no errors), admin password not default, speaker integrity clean
(`GET /api/admin/speakers/integrity` → `"ok": true`), Fish warm (if Clone Studio), recent
seed pack and full backup in `~/voice-id-audio/`, browser game and `/tv` on the booth
devices (owner check if you cannot see them).

## Report

Lead with ready / blocked / partially verified and why. Include cluster (no
hostnames in files), namespaces, versions, evidence, and remaining owner actions.
Describe the stack as a custom conference demo, not a Red Hat supported solution.

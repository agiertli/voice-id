# Install and operate the full stack

The canonical runbook for technical owners and AI agents. It installs, on a fresh
OpenShift cluster, everything the Voice ID booth needs: GPU nodes, two TTS engines,
two LLM opponents, two detector opponents, the game and Voice Clone Studio. For
having an agent do it, see [AGENT-INSTALLATION.md](AGENT-INSTALLATION.md); the agent
follows this page through the `voice-id-operations` skill.

This is a custom conference demo built from Red Hat and community components. A
working install is not a Red Hat supported configuration; see
[supportability notes](#supportability-notes).

## Audio never goes to Git

The repository contains **no audio and no databases**. Voice recordings of real
people, generated clones, seed packs, backups and exports live only in the cluster
databases and in local directories **outside** the repository (default
`~/voice-id-audio/`). Never commit them, attach them to issues or pull requests, or
paste them into chats. Enable the repository's pre-commit guard once per clone:

```bash
git config core.hooksPath .githooks
```

The game's clips come from a private **seed pack** you receive from the game owner
(step 5, [SEEDING.md](SEEDING.md)).

## What you get

All in namespace `voice-id` unless noted.

| Component | Kind | Runs on | Purpose |
|---|---|---|---|
| `voice-id` | Helm chart `apps/voice-id/helm/voice-id`, Flask/gunicorn, SQLite on 1 Gi PVC | CPU (0.5–2 cores) | The game, `/tv`, `/admin` |
| `qwen2-audio` | LLMInferenceService (vLLM), `Qwen/Qwen2-Audio-7B-Instruct` | 1× NVIDIA L4 | Opponent Teddy |
| `gemma` | LLMInferenceService (vLLM), `RedHatAI/gemma-4-12B-it-FP8-dynamic` | 1× NVIDIA L4 | Opponent Iris (spectrograms) |
| `deepfake-detector-base` | Deployment `quay.io/agiertli/deepfake-detector-base:v1.0.3` | CPU (2–4 cores) | Opponent Gary |
| `deepfake-detector-fine-tuned` | Deployment `quay.io/agiertli/deepfake-detector:v2.0.0` (model V4-A) | CPU (2–4 cores) | Opponent Gary 2.0 |
| `xtts-v2` | Deployment, Coqui XTTS streaming server | 1× NVIDIA L4 | Voice cloning for Clone Studio |
| `fish-native` | Deployment, Fish Speech S2 Pro with torch.compile, 20 Gi PVC | 1× NVIDIA L4 | Voice cloning for Clone Studio |
| `voice-clone-studio` (namespace `voice-clone-studio`) | Helm chart, Flask, SQLite on 1 Gi PVC | CPU | Record voices, clone them, sentence recordings |

The game does not call the TTS engines at runtime; clips are pre-made. XTTS and
Fish are needed only to create new clips with Clone Studio. Component details and
data flow: [ARCHITECTURE.md](ARCHITECTURE.md).

## Validated environment

First full install on **2026-11-24**:

- OpenShift on AWS (installer-provisioned, MachineSets available), an OpenTLC/RHDP
  demo sandbox with **Red Hat OpenShift AI 3.x** (3.5.3 recorded at install time),
  NVIDIA GPU Operator and Node Feature Discovery already configured, storage
  class `gp3-csi`.
- 4 added `g6.2xlarge` workers (8 vCPU, 32 GiB, 1× NVIDIA L4 24 GB each), one per
  GPU workload.
- Image tags at install: voice-id `v2.8.2`, voice-clone-studio `v2.1.0`,
  deepfake-detector `v2.0.0`, deepfake-detector-base `v1.0.3`. The chart's
  `values.yaml` is the source of truth for current tags.
- Wall-clock about **35 minutes**, dominated by node provisioning, image pulls,
  model downloads, vLLM start-up and the Fish compile. The order below runs the
  slow parts in parallel.

Other clouds, GPU sharing (MIG/time-slicing), disconnected clusters and OpenShift
AI 2.x are untested.

## Owner decisions

1. **Target cluster** and a dedicated kubeconfig file for it.
2. **Admin password** for `/admin` (the image default is public; override it).
3. **Event name** (`conference.name`) and **default UI language** (`en`, `cs`, `sk`).
4. **Prize rule** (`game.prizeThreshold`, default 4 of 5 correct). See [scoring](customization/scoring.md).
5. **Which opponents** to run (all four by default).
6. **Seed pack** to load and who provides it.
7. Whether **Voice Clone Studio** is needed (it needs the two TTS GPUs).

## Prerequisites

Workstation: `oc`, **Helm 3** (`helm version` must report v3), Python 3.9+ (standard
library only), `curl`, Git. No container engine unless you build images.

Cluster: cluster-admin; OpenShift AI 3.x with KServe `LLMInferenceService`
(`oc get crd llminferenceservices.serving.kserve.io`); GPU Operator with a healthy
`ClusterPolicy`; Node Feature Discovery; a default RWO storage class (the manifests
name `gp3-csi`); quota for four more GPU instances in one availability zone. The
LLMInferenceService manifests reference hardware profile `gpu-profile` in
`redhat-ods-applications`; confirm it exists (`oc get hardwareprofile -n redhat-ods-applications`)
or adjust the annotations.

Demo sandboxes may ship a preinstalled model (for example
`my-first-model/llama-32-3b-instruct`) that occupies the GPU on the control-plane
node. Do not count on that GPU and do not stop that model without the owner's
approval; this install brings its own four GPU nodes.

## 0. Isolate your cluster credentials

Never `oc login` into the shared `~/.kube/config` when you work with more than one
cluster: it switches the context under every other terminal. Use one kubeconfig
file per cluster:

```bash
export KUBECONFIG=~/.kube/voice-id-<event>.kubeconfig
oc login --server=https://api.<cluster-domain>:6443 -u <admin-user>   # writes only to that file
oc whoami && oc whoami --show-server
```

Run every `oc` and `helm` command of this runbook in a shell with that variable
set. Keep credentials out of the repository and out of shared notes.

## 1. Add four GPU nodes (start first, about 5 minutes)

`infra/gpu-machineset.yaml` is an **example from another cluster**: AMI, subnet,
security groups, IAM profile and infrastructure id differ per cluster, and it has
no taint. Clone your cluster's own worker MachineSet instead:

```bash
oc get machineset -n openshift-machine-api
SRC=$(oc get machineset -n openshift-machine-api -o name | grep worker | head -1)

oc get "$SRC" -n openshift-machine-api -o json | python3 -c '
import json, sys
ms = json.load(sys.stdin)
name = ms["metadata"]["name"].replace("worker", "gpu")
for k in ("uid", "resourceVersion", "creationTimestamp", "generation", "managedFields", "annotations"):
    ms["metadata"].pop(k, None)
ms.pop("status", None)
ms["metadata"]["name"] = name
ms["metadata"]["annotations"] = {"machine.openshift.io/GPU": "1", "machine.openshift.io/vCPU": "8",
                                 "machine.openshift.io/memoryMb": "32768"}
ms["spec"]["replicas"] = 4
ms["spec"]["selector"]["matchLabels"]["machine.openshift.io/cluster-api-machineset"] = name
t = ms["spec"]["template"]
t["metadata"]["labels"]["machine.openshift.io/cluster-api-machineset"] = name
t["spec"].setdefault("metadata", {}).setdefault("labels", {})["node-role.kubernetes.io/gpu-worker"] = ""
t["spec"]["taints"] = [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}]
p = t["spec"]["providerSpec"]["value"]
p["instanceType"] = "g6.2xlarge"
p["blockDevices"][0]["ebs"]["volumeSize"] = 200
print(json.dumps(ms, indent=1))
' > /tmp/gpu-machineset.json

oc apply -f /tmp/gpu-machineset.json
oc get machines -n openshift-machine-api -w        # Provisioning -> Provisioned -> Running
```

- One node per GPU workload: XTTS, Fish, Qwen, Gemma. Reduce `replicas` if you skip
  Clone Studio (2 nodes for the two LLM opponents).
- The taint keeps ordinary pods off GPU nodes; all GPU manifests tolerate
  `nvidia.com/gpu`.
- Schedule on `nvidia.com/gpu.present: "true"` (set by GPU feature discovery).
  Custom node labels from a MachineSet can be reset by the Machine controller.
- If the AZ has no `g6.2xlarge` capacity, clone a worker MachineSet from another AZ.

New nodes show zero GPUs for about 4 minutes while the GPU Operator builds the
driver and starts the device plugin. Wait for:

```bash
oc get nodes -l node-role.kubernetes.io/gpu-worker          # STATUS Ready
oc get nodes -l node-role.kubernetes.io/gpu-worker \
  -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu   # GPU 1
```

Continue with steps 2–4 while the nodes come up.

## 2. Namespace, model downloads and CPU components (parallel with step 1)

```bash
oc new-project voice-id

# LLM opponents: PVC + download Job + LLMInferenceService in one file each.
# The Jobs start now (CPU only); the vLLM pods wait for GPU nodes.
oc apply -f infra/qwen2-audio/manifests.yaml
oc apply -f infra/gemma/manifests.yaml

# Detector opponents (CPU only)
oc apply -f infra/deepfake-detector-base/manifests.yaml
oc apply -f infra/deepfake-detector/manifests.yaml

oc get jobs,pods -n voice-id -w
```

Facts that cost time on the first install:

- **Gemma must be the FP8 model** `RedHatAI/gemma-4-12B-it-FP8-dynamic`: 12.75 GiB of
  weights, fits one L4.
- **vLLM arguments lose JSON quotes** inside an LLMInferenceService. Use dotted
  flags such as `--limit-mm-per-prompt.image=1`, never a JSON value.
- **Set the served name with `spec.model.name`** (Gemma: `gemma-4-12b`), not with
  `--served-model-name`; giving both duplicates the flag. The Qwen manifest still
  uses `--served-model-name=Qwen2-Audio-7B-Instruct` without `spec.model.name`. The
  game's `aiJudge.model`/`aiJudge2.model` must equal the served names.
- **Detectors need the 4 CPU limit** in the manifests: about 5 s per clip; with 2 CPU
  it was about 20 s, which makes the battle loading screen crawl.

## 3. TTS engines for Clone Studio (after GPU nodes are Ready)

Skip if you do not run Voice Clone Studio.

```bash
oc apply -f infra/xtts/manifests.yaml
oc apply -f infra/fish-speech/manifests.yaml
oc get pods -n voice-id -l 'app in (xtts-v2,fish-native)' -w
```

- **XTTS** needs `COQUI_TOS_AGREED=1`, `HOME=/tmp`, `TMPDIR=/tmp`,
  `NUMBA_CACHE_DIR`, `MPLCONFIGDIR` and `workingDir: /tmp` (all in the manifest) to
  run under the restricted SCC. Its `/tts` returns base64 WAV wrapped in JSON quotes.
  The manifest uses the upstream tag `latest-cuda121`; pin a digest for repeatable
  installs.
- **Fish** has an init container that downloads about 9 GB of weights into the
  `fish-data` PVC on first start (skipped later). The server runs with `--compile`:
  the **first request triggers a torch.compile of about 4 minutes**; afterwards a
  short sentence takes about 5–7 s (up to 30–120 s for long texts). Warm it up (see
  [Fish warm-up](#fish-speech-warm-up)) before visitors arrive.
- Fish voice references are stored **in memory** and lost on every Fish restart.
  Clone Studio re-registers all stored voices when Clone Studio starts.
- The Fish route has `haproxy.router.openshift.io/timeout: 300s`; Clone Studio uses
  the internal service, not the route.

## 4. Install the game

From the repository root:

```bash
umask 077
read -r -s -p 'Admin password: ' P; printf '\n'; printf '%s' "$P" > ~/.voice-id-admin-token; unset P

helm upgrade --install voice-id apps/voice-id/helm/voice-id -n voice-id \
  -f apps/voice-id/helm/voice-id/values.yaml \
  --set-file adminToken=$HOME/.voice-id-admin-token \
  --set conference.name="<Event name>" --set game.defaultLang=cs

oc rollout status deploy/voice-id -n voice-id --timeout=180s
oc get route voice-id -n voice-id -o jsonpath='https://{.spec.host}{"\n"}'
```

- The chart's opponent URLs point at internal services in `voice-id`. LLM opponents
  use `https://<name>-kserve-workload-svc.voice-id.svc.cluster.local:8000/v1`; the
  app calls them with TLS verification off (cluster-internal certificate).
- **Helm reuses old values.** `helm upgrade` without `-f`/`--set` reuses the previous
  release's values and ignores edits to `values.yaml`. Always pass `-f` (or
  `--reset-values`), and pass `adminToken` again, or the password falls back to the
  image default.
- Opponents appear in the game as soon as their URL is set, even if the backend is
  not ready yet; they then play random answers. Verify (step 6) before opening.

## 5. Load the clips from a seed pack

The game starts empty. Import the private seed pack you received. The complete
seeding guide (modes, verification, local folders, sharing) is
[SEEDING.md](SEEDING.md).

- Open `https://<route>/admin`, log in, **Seed pack & backup** → **Import seed pack**,
  mode `skip`; or headless:

```bash
ROUTE=$(oc get route voice-id -n voice-id -o jsonpath='{.spec.host}')
VOICEID_ADMIN_TOKEN="$(cat ~/.voice-id-admin-token)" python3 apps/voice-id/tools/seedpack.py import \
  --game "https://${ROUTE}" --file ~/voice-id-audio/seedpacks/<pack>.seedpack.zip --mode skip
curl -sk "https://${ROUTE}/api/admin/clips" | python3 -c 'import json,sys; c=json.load(sys.stdin); print(len(c), "clips,", sum(x["is_ai"] for x in c), "AI")'
```

The first install (before seed packs existed) loaded 86
clips (20 human, 66 AI) with the low-level upload loop in
[clips and speakers](customization/clips-and-speakers.md#low-level-bulk-upload-from-a-local-database).
**Never `oc cp` a database into the pod.**

A game needs at least `game.size` clips including at least one AI and one human clip.

## 6. Verify

Wait until every component is ready:

```bash
oc get pods -n voice-id
oc get llminferenceservice -n voice-id
```

`READY` on the LLMInferenceServices can stay `False` with reason
`GatewayPreconditionNotMet`: without Red Hat Connectivity Link (which provides the
`AuthPolicy` CRD) the external gateway route is not created. **The vLLM workload
still runs** and the game uses the internal service, so this is expected. Check the
workload pods instead and query the models from inside the game pod:

```bash
for m in gemma qwen2-audio; do
  oc exec deploy/voice-id -n voice-id -- python3 -c "import requests; print('$m', requests.get('https://$m-kserve-workload-svc.voice-id.svc.cluster.local:8000/v1/models', verify=False, timeout=10).json()['data'][0]['id'])"
done
```

Expected: `gemma gemma-4-12b` and `qwen2-audio Qwen2-Audio-7B-Instruct`.

End-to-end smoke test through the route (creates a player named `install-e2e` with no
completed game, so nothing appears on the leaderboard):

```bash
cat > /tmp/voice-id-e2e.py <<'EOF'
import json, ssl, sys, time, urllib.request
R = sys.argv[1]; ctx = ssl._create_unverified_context()
def call(path, body=None):
    req = urllib.request.Request(f"https://{R}{path}", json.dumps(body).encode() if body is not None else None,
                                 {"Content-Type": "application/json"})
    try: return json.load(urllib.request.urlopen(req, context=ctx, timeout=180))
    except urllib.error.HTTPError as e: return {"HTTP": e.code}
print("clips:", len(call("/api/admin/clips")))
opps = call("/api/battle/opponents"); print("opponents:", [o["id"] for o in opps])
g = call("/api/game/start", {"player_name": "install-e2e", "email": ""})
print("game", g.get("game_id"), "with", len(g.get("clips", [])), "clips")
for c in g.get("clips", [])[:2]:
    for o in opps:
        t = time.time(); r = call(f"/api/game/{g['game_id']}/{o['analyzeEndpoint']}", {"clip_id": c["id"]})
        print(f"  {o['id']:15s} {time.time()-t:5.1f}s guess={r.get('guess')} {'ERROR '+str(r) if 'guess' not in r else ''}")
EOF
python3 /tmp/voice-id-e2e.py "$ROUTE"
```

Every opponent must return a `guess`. Rough timings at install: detectors about
5 s per clip (on 4 CPU), LLM opponents a few seconds once loaded. Then:

- [ ] Clip count matches the seed pack.
- [ ] `/api/battle/opponents` lists the four ids (or the ones you chose).
- [ ] Each opponent answers in the smoke test; Iris returns a spectrogram.
- [ ] A browser game works over HTTPS on the booth device: play, guess, reveal,
      result with prize banner (solo), and one battle.
- [ ] `/tv` shows the test game; `/admin` accepts the new password (not the default).
- [ ] Reset scores on `/admin` after testing.
- [ ] Fish is warm and Clone Studio generates with both engines (if used).
- [ ] A seed pack export and a full backup were downloaded to `~/voice-id-audio/`
      ([backups](customization/backups.md)).

## 7. Voice Clone Studio (optional)

```bash
helm upgrade --install voice-clone-studio apps/voice-clone-studio/helm/voice-clone-studio \
  -n voice-clone-studio --create-namespace \
  -f apps/voice-clone-studio/helm/voice-clone-studio/values.yaml
oc rollout status deploy/voice-clone-studio -n voice-clone-studio
oc get route voice-clone-studio -n voice-clone-studio -o jsonpath='https://{.spec.host}{"\n"}'
```

The chart's defaults point at the internal TTS services
`http://xtts-v2.voice-id.svc.cluster.local` and
`http://fish-native.voice-id.svc.cluster.local:8080`. Clone Studio's UI is Czech by
default (`defaultLanguage`); its API docs are at `/api/docs`. Its API has **no
authentication**: do not publish its route. Recording and importing:
[clips and speakers](customization/clips-and-speakers.md).

## Fish Speech warm-up

Run after every Fish (re)start, from inside the cluster so the route timeout does
not interrupt the 4-minute compile:

```bash
oc exec deploy/voice-id -n voice-id -- python3 -c "
import requests, time; t = time.time()
r = requests.post('http://fish-native:8080/v1/tts', json={'text': 'Dobrý den, toto je zkouška.', 'format': 'wav'}, timeout=900)
print(r.status_code, len(r.content), 'bytes', round(time.time() - t), 's')"
```

Expect `200` after roughly 240–260 s the first time, then a few seconds on a second
run. A `503` or HTML error page during the compile is not a routing problem; wait and
retry. This request uses Fish's default voice (no reference) and is unverified as a
warm-up path; if it fails, generate one clip through Clone Studio instead. After a
Fish restart also restart Clone Studio so it re-registers its voices:
`oc rollout restart deploy/voice-clone-studio -n voice-clone-studio`.

## Exposing the models externally

The supported way to expose an OpenShift AI 3.x LLMInferenceService outside the
cluster is through its gateway, which needs **Red Hat Connectivity Link**
installed (it provides `AuthPolicy`). Install it from OperatorHub and follow the
OpenShift AI documentation if you need external, authenticated model endpoints.
The game does not need it. Not tested in this project.

## Upgrades

- **Game:** change `image.tag` (or other values) in your values file, then
  `helm upgrade voice-id apps/voice-id/helm/voice-id -n voice-id -f <values> --set-file adminToken=...`.
  Take a full backup first ([backups](customization/backups.md)).
- **LLM opponents with one GPU each:** changing an LLMInferenceService starts a new
  pod that cannot get a GPU while the old one holds it; the rolling update hangs with
  the new pod `Pending`. Scale the old ReplicaSet to zero:
  `oc get rs -n voice-id | grep gemma`, then `oc scale rs/<old-rs> -n voice-id --replicas=0`.
- **Detectors / TTS:** edit the tag in `infra/.../manifests.yaml` and `oc apply`.
  XTTS and Fish hold a GPU each and have the same single-GPU constraint; use
  `oc scale deploy/<name> --replicas=0` then back to 1 if a rollout hangs.

## Troubleshooting

| Symptom | Likely cause | Check / fix |
|---|---|---|
| GPU pods `Pending`, nodes show 0 GPU | Driver not ready yet (about 4 min after node join) | `oc get pods -n nvidia-gpu-operator`; wait for driver and device-plugin pods |
| GPU pods `Pending` with taint message | Missing toleration in a custom manifest | Add the `nvidia.com/gpu` toleration |
| New LLM pod `Pending` after a change | Single-GPU rolling update deadlock | Scale old ReplicaSet to 0 (see Upgrades) |
| LLMInferenceService `GatewayPreconditionNotMet` | No Connectivity Link | Expected; verify the workload pod and internal `/v1/models` |
| vLLM fails on argument parsing | JSON-valued flag lost its quotes | Use dotted flags, e.g. `--limit-mm-per-prompt.image=1` |
| vLLM "served-model-name" duplicated | Both `spec.model.name` and the flag | Keep only `spec.model.name` |
| Opponent always wrong or random; game log shows `AI analysis failed` | Backend down, wrong model name or URL | `oc logs deploy/voice-id -n voice-id | grep -i 'fail'`; compare `aiJudge*.model` with `/v1/models` |
| Battle loading screen takes very long | Detector CPU-starved or LLM cold | Check detector limits (4 CPU); first LLM request after start is slower |
| Fish returns 503/HTML or times out | torch.compile in progress | Wait about 4 min after first request; warm it up from inside the cluster |
| Clone Studio: "voice not registered with Fish" | Fish restarted, references lost | Restart Clone Studio to re-register voices |
| XTTS permission errors | Missing env/workingDir | Keep the manifest's env vars and `workingDir: /tmp` |
| New `voice-id` pod stuck `ContainerCreating` with Multi-Attach | RWO PVC still attached to the old pod on another node (rolling update) | Scale the old ReplicaSet to 0 or delete the old pod; untested on this chart, observed generally with RWO volumes |
| `values.yaml` change has no effect | Helm reused old values | Upgrade with `-f <values>` or `--reset-values` |
| Admin password is the default again | Upgrade without `adminToken` | Re-run the upgrade with `--set-file adminToken=...` |
| `Not enough clips` on game start | Fewer clips than `game.size`, or no AI/human clip | Import the seed pack; check `/api/admin/clips` |
| Database errors after someone copied files | `oc cp` into a live SQLite | Stop; restore from seed pack and full backup with owner approval |

Useful commands:

```bash
oc logs deploy/voice-id -n voice-id --tail=100          # GAME_START / GUESS / AI_JUDGE / DETECTOR lines
oc get events -n voice-id --sort-by=.lastTimestamp | tail -20
oc adm top pods -n voice-id
```

## Teardown

Export a seed pack and a full backup first. Then delete the namespaces
(`oc delete project voice-clone-studio voice-id`, which deletes the PVCs and all
data) and scale the GPU MachineSet to zero or delete it to stop GPU costs.

## Supportability notes

- Red Hat components: OpenShift, OpenShift AI (KServe LLMInferenceService, vLLM
  runtime), GPU Operator, UBI base images, `RedHatAI/gemma-4-12B-it-FP8-dynamic`
  (published by Red Hat AI). Their support follows the respective product terms.
- Community / third-party: Qwen2-Audio (Alibaba), Coqui XTTS v2 (Coqui Public Model
  License; the project is no longer maintained), Fish Speech S2 Pro, the Wav2Vec2
  detectors, the game and Clone Studio themselves. No vendor support.
- Single replicas everywhere, SQLite on RWO volumes, unauthenticated clip and
  Clone Studio APIs: suitable for a booth, not for production.

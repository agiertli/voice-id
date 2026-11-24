# Installation through an AI agent

Use this with Claude Code, Codex or another terminal-capable agent. The agent needs
this repository, a dedicated kubeconfig for the target cluster and the owner
settings below. It needs no prior conversation, personal memory, registry push
access or container engine. It follows [INSTALLATION.md](INSTALLATION.md).

## Skill and agent

The repository includes the skill
[voice-id-operations](../.agents/skills/voice-id-operations/SKILL.md): install,
verify, operate and troubleshoot the full stack, manage clips, voice cloning, seed
packs and backups, and explain scoring. Codex discovers it in `.agents/skills`;
Claude Code discovers the same folder through the relative symlink
`.claude/skills/voice-id-operations`. Edit the canonical file in `.agents/skills`.

- Claude Code: `/voice-id-operations <task>`
- Codex: `$voice-id-operations <task>`

If the skill is not listed, restart the agent session inside the repository.

Claude Code also has a background agent type, `.claude/agents/voice-id.md`, for
fire-and-forget tasks where inputs are known (readiness check, diagnostics):

```text
Agent({ subagent_type: "voice-id", prompt: "Check booth readiness in namespace voice-id; change nothing" })
```

Use the skill for interactive work where you make decisions as you go.

## Before starting

1. Order or obtain an OpenShift on AWS cluster with OpenShift AI 3.x and the NVIDIA
   GPU Operator (for example an RHDP / OpenTLC OpenShift AI sandbox) and its admin
   credentials.
2. Log in **into a dedicated kubeconfig file**, not your shared one:

```bash
git clone <repository-url> voice-id && cd voice-id
git config core.hooksPath .githooks            # blocks audio/db commits
export KUBECONFIG=~/.kube/voice-id-<event>.kubeconfig
oc login --server=https://api.<cluster-domain>:6443 -u <admin-user>
oc whoami --show-server
```

3. Start the agent **in that shell** so it inherits `KUBECONFIG`. Tell it the file
   path anyway; the skill refuses to `oc login` into the shared kubeconfig.
4. Have the private **seed pack** zip in `~/voice-id-audio/seedpacks/` (outside the
   repository). The repository contains no audio. See [SEEDING.md](SEEDING.md).

## Copyable prompt

Edit the lines under OWNER SETTINGS, then paste the whole block.

```text
/voice-id-operations
OWNER SETTINGS — EDIT THESE
Kubeconfig file: ~/.kube/voice-id-<event>.kubeconfig
Event name: <e.g. DevConf 2026>
Default UI language: <en | cs | sk>
Prize threshold: <4 of 5 correct, or another number>
Install profile: <minimal | standard | full>   (standard = 2 GPUs, recommended; full = 4 GPUs incl. Clone Studio)
Admin password: <chosen password, or path to a private mode-600 file>
Seed pack: <~/voice-id-audio/seedpacks/<file>.seedpack.zip>

INSTRUCTIONS — KEEP THESE
Install the Voice ID stack with the profile above on this fresh cluster following
docs/INSTALLATION.md (install.sh --profile <profile> or the manual steps).
- Show me the profile/GPU/cost table and confirm the profile before provisioning anything.
- Provision only what the profile needs (no GPU MachineSet for minimal; replicas 2 for
  standard, 4 for full).
- Use only the kubeconfig above (export KUBECONFIG); never oc login into ~/.kube/config.
- Parallelise: GPU MachineSet first (cloned from this cluster's worker MachineSet,
  4x g6.2xlarge, taint nvidia.com/gpu), model downloads and CPU components while nodes
  come up, GPU workloads once GPUs are allocatable.
- Write a literal admin password to a mode-600 temp file, use --set-file adminToken=,
  delete the file afterwards. Never print it.
- Load clips only from the seed pack following docs/SEEDING.md (admin UI or
  apps/voice-id/tools/seedpack.py import, token via $VOICEID_ADMIN_TOKEN). Never oc cp a database.
- Never commit, attach or copy audio, seed packs, backups or databases into the repository.
- Do not stop pre-existing models on the cluster without asking me.
- Warm up Fish Speech if the profile is full.
- Never scale down or remove any component without my explicit confirmation.
- Verify with the runbook's smoke test: clip count, every opponent answers, one game.
- Report: URLs (game, /tv, /admin, Clone Studio), versions, what was verified, what I
  still must check in a browser, and anything that deviated from the runbook.
```

## Install profiles

| Profile | GPUs | Opponents | Clone Studio | Approx. GPU cost |
|---|---:|---|---|---|
| minimal | 0 | Gary, Gary 2.0 | no | none |
| standard (default) | 2 | all four | no | ≈ $2/h |
| full | 4 | all four | yes (XTTS, Fish) | ≈ $4/h |

About $1/h per `g6.2xlarge`, approximate; check current AWS pricing. The skill asks
for the profile at the start of every install (default `standard`) and shows this
table. Details and switching: [install profiles](INSTALLATION.md#install-profiles).
The project is in its voice collection phase, so `full` is the current setting for
the recording cluster.

## What the agent does

1. Checks identity and server with the given kubeconfig; confirms the cluster with
   you if anything looks unexpected (existing `voice-id` namespace, GPU nodes in use).
2. Confirms the profile, then clones the worker MachineSet into a GPU MachineSet with
   the profile's replicas (skipped for `minimal`).
3. Creates `voice-id`, applies the two detectors, Qwen and Gemma (`standard`/`full`;
   downloads start), then XTTS and Fish (`full`) when GPUs are allocatable.
4. Installs the game with your values, admin password and the profile's opponent flags;
   installs Clone Studio (`full`).
5. Imports the seed pack and checks the clip count.
6. Waits for vLLM workloads (accepting `GatewayPreconditionNotMet` on the gateway),
   warms Fish (`full`), runs the smoke test.
7. Hands over URLs and the remaining manual checks.

Expected duration about 35 minutes for `full` on a fresh cluster; `minimal` is a few
minutes (no GPU nodes or model downloads).

## Handover checks for the owner

The agent cannot prove what happens on the booth device. Before visitors arrive:
play one solo and one battle game on the booth laptop over HTTPS, check `/tv` on the
monitor, log in to `/admin` with your password and reset scores, and confirm a seed
pack export and full backup exist in `~/voice-id-audio/`. See
[booth operations](customization/booth-operations.md).

## Common follow-up tasks

```text
/voice-id-operations Check booth readiness; report gaps without changing anything.
/voice-id-operations Explain the scoring of a 4/5 battle against Gary 2.0.
/voice-id-operations Import Anton's new Czech sentence recordings with XTTS and Fish pairs (dry run first).
/voice-id-operations Take a seed pack export and a full backup.
/voice-id-operations Disable the Iris opponent for today.
```

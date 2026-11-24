#!/usr/bin/env bash
# Voice ID: install the booth stack on an OpenShift AI 3.x cluster on AWS.
#
#   ./install.sh --profile minimal|standard|full [--namespace voice-id]
#                [--kubeconfig PATH] [--admin-token-file PATH] [--yes] [--dry-run]
#
# Profiles (see docs/INSTALLATION.md#install-profiles):
#   minimal   game + both Wav2Vec2 detectors (Gary, Gary 2.0)        0 GPUs, CPU only
#   standard  minimal + Qwen2-Audio (Teddy) + Gemma (Iris)            2x NVIDIA L4 (g6.2xlarge)
#   full      standard + XTTS v2 + Fish Speech + Voice Clone Studio   4x NVIDIA L4
#
# Rules this script follows:
#   - It never runs `oc login`. It uses the current KUBECONFIG (or --kubeconfig) only,
#     prints the target server and asks for confirmation unless --yes.
#   - It never reads, copies or uploads audio. Clips come later from a seed pack
#     (docs/SEEDING.md).
#   - Re-runs are idempotent: apply / helm upgrade --install / scale.
#   - --dry-run prints every mutating command instead of running it. Read-only
#     discovery (oc whoami, oc get) still runs so the plan matches the cluster.
#
# STATUS: validated with bash -n and a mocked `oc`/`helm` dry run only.
# NOT yet tested against a live cluster.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE=""
NAMESPACE="voice-id"
STUDIO_NAMESPACE="voice-clone-studio"
KUBECONFIG_ARG=""
ADMIN_TOKEN_FILE=""
VALUES="${ROOT}/apps/voice-id/helm/voice-id/values.yaml"
STUDIO_VALUES="${ROOT}/apps/voice-clone-studio/helm/voice-clone-studio/values.yaml"
ASSUME_YES=0
DRY_RUN=0
GPU_INSTANCE_TYPE="g6.2xlarge"
GPU_TIMEOUT_SEC=1800
MODEL_TIMEOUT_SEC=2400

usage() {
  sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --profile P            minimal | standard | full (required)
  --namespace NS         namespace for the game, opponents and TTS (default voice-id)
  --studio-namespace NS  namespace for Voice Clone Studio (default voice-clone-studio)
  --kubeconfig PATH      kubeconfig file to use (default: $KUBECONFIG)
  --admin-token-file F   file holding the /admin password (passed with --set-file adminToken=F).
                         Omit it and the public image default password is used.
  --values F             voice-id Helm values file (default apps/voice-id/helm/voice-id/values.yaml)
  --yes                  do not ask for confirmation
  --dry-run              print mutating commands instead of running them
  -h, --help             this help
EOF
}

die()  { echo "ERROR: $*" >&2; exit 1; }
log()  { printf '\n==> %s\n' "$*"; }
warn() { echo "WARNING: $*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="${2:-}"; shift 2 ;;
    --namespace) NAMESPACE="${2:-}"; shift 2 ;;
    --studio-namespace) STUDIO_NAMESPACE="${2:-}"; shift 2 ;;
    --kubeconfig) KUBECONFIG_ARG="${2:-}"; shift 2 ;;
    --admin-token-file) ADMIN_TOKEN_FILE="${2:-}"; shift 2 ;;
    --values) VALUES="${2:-}"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

case "$PROFILE" in
  minimal) GPUS=0 ;;
  standard) GPUS=2 ;;
  full) GPUS=4 ;;
  "") usage; die "--profile is required" ;;
  *) die "--profile must be minimal, standard or full" ;;
esac
[[ "$NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die "invalid namespace: $NAMESPACE"
[[ "$STUDIO_NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die "invalid namespace: $STUDIO_NAMESPACE"

if [[ -n "$KUBECONFIG_ARG" ]]; then
  [[ -f "$KUBECONFIG_ARG" ]] || die "kubeconfig not found: $KUBECONFIG_ARG"
  export KUBECONFIG="$KUBECONFIG_ARG"
fi

for cmd in oc helm python3; do
  command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' not found on PATH"
done
helm version --short 2>/dev/null | grep -q '^v3\.' || die "Helm 3 is required (helm version must report v3)"
[[ -f "$VALUES" ]] || die "values file not found: $VALUES"
if [[ -n "$ADMIN_TOKEN_FILE" ]]; then
  [[ -f "$ADMIN_TOKEN_FILE" ]] || die "admin token file not found: $ADMIN_TOKEN_FILE"
fi

# Our own images must be pinned in the values files.
for f in "$VALUES" "$STUDIO_VALUES"; do
  tag="$(grep -E '^[[:space:]]+tag:' "$f" | head -1 | sed -E 's/.*tag:[[:space:]]*"?([^"]*)"?.*/\1/')"
  [[ -n "$tag" && "$tag" != "latest" ]] || die "image tag in $f must be pinned (found '${tag:-none}')"
done

# --- command wrappers ---------------------------------------------------------

# Mutating command: print in dry-run, run otherwise.
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '[dry-run]'; printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}

# Apply an infra manifest, rewriting the namespace if it is not the default.
apply_manifest() {
  local file="${ROOT}/$1"
  [[ -f "$file" ]] || die "manifest not found: $file"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] oc apply -f $1   (namespace ${NAMESPACE})"
    return
  fi
  sed -e "s/namespace: voice-id\$/namespace: ${NAMESPACE}/" "$file" | oc apply -f -
}

# Wait until a command succeeds, or time out.
wait_for() {
  local desc="$1" timeout="$2"; shift 2
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "[dry-run] wait (max ${timeout}s): ${desc}"
    return 0
  fi
  local start; start=$(date +%s)
  printf 'Waiting for %s ' "$desc"
  until "$@" >/dev/null 2>&1; do
    if (( $(date +%s) - start > timeout )); then
      echo; die "timed out after ${timeout}s waiting for ${desc}"
    fi
    printf '.'; sleep 15
  done
  echo " ok ($(( $(date +%s) - start ))s)"
}

gpu_allocatable() {
  oc get nodes -l nvidia.com/gpu.present=true \
    -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' 2>/dev/null \
    | awk '{s+=$1} END {print s+0}'
}
enough_gpus() { [[ "$(gpu_allocatable)" -ge "$GPUS" ]]; }

model_ready() {  # $1 = LLMInferenceService name, $2 = expected served model id
  oc exec -n "$NAMESPACE" deploy/voice-id -- python3 -c "
import requests, sys
r = requests.get('https://$1-kserve-workload-svc.${NAMESPACE}.svc.cluster.local:8000/v1/models', verify=False, timeout=10)
sys.exit(0 if '$2' in r.text else 1)"
}

# --- 0. target and confirmation -----------------------------------------------

log "Target cluster"
oc whoami >/dev/null 2>&1 || die "not authenticated. This script never runs 'oc login'. Log in yourself into a DEDICATED kubeconfig:
  export KUBECONFIG=~/.kube/voice-id-<event>.kubeconfig
  oc login --server=https://api.<cluster-domain>:6443 -u <admin-user>
then re-run with --kubeconfig \"\$KUBECONFIG\"."
SERVER="$(oc whoami --show-server)"
USER_NAME="$(oc whoami)"
echo "  kubeconfig: ${KUBECONFIG:-~/.kube/config (default)}"
echo "  server:     ${SERVER}"
echo "  user:       ${USER_NAME}"
echo "  profile:    ${PROFILE} (${GPUS} GPU node(s), ${GPU_INSTANCE_TYPE})"
echo "  namespace:  ${NAMESPACE}$([[ $PROFILE == full ]] && echo ", Clone Studio in ${STUDIO_NAMESPACE}")"
[[ $DRY_RUN -eq 1 ]] && echo "  mode:       DRY RUN (no changes)"
[[ -z "${KUBECONFIG:-}" ]] && warn "KUBECONFIG is not set; using the shared ~/.kube/config. Prefer a dedicated file per cluster."
[[ -z "$ADMIN_TOKEN_FILE" ]] && warn "no --admin-token-file: /admin keeps the PUBLIC image default password. Re-runs without it also reset a custom password."

if [[ $ASSUME_YES -eq 0 && $DRY_RUN -eq 0 ]]; then
  read -r -p "Install profile '${PROFILE}' on ${SERVER}? [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]] || die "aborted"
fi

oc auth can-i '*' '*' --all-namespaces >/dev/null 2>&1 || warn "current user may not be cluster-admin"
if [[ $GPUS -gt 0 ]]; then
  oc get crd llminferenceservices.serving.kserve.io >/dev/null 2>&1 \
    || die "LLMInferenceService CRD missing: OpenShift AI 3.x with KServe is required for profile ${PROFILE}"
  oc get clusterpolicy >/dev/null 2>&1 || warn "no NVIDIA ClusterPolicy found; is the GPU Operator installed?"
fi

# --- 1. GPU MachineSet (cloned from the cluster's own worker MachineSet) ------

if [[ $GPUS -gt 0 ]]; then
  log "1. GPU MachineSet: ${GPUS} x ${GPU_INSTANCE_TYPE}"
  GPU_MS="$(oc get machineset -n openshift-machine-api -l voice-id/gpu-machineset=true -o name | head -1 || true)"
  if [[ -n "$GPU_MS" ]]; then
    CUR="$(oc get "$GPU_MS" -n openshift-machine-api -o jsonpath='{.spec.replicas}')"
    if [[ "${CUR:-0}" -lt "$GPUS" ]]; then
      echo "Found ${GPU_MS} with ${CUR} replica(s); scaling up to ${GPUS}"
      run oc scale "$GPU_MS" -n openshift-machine-api --replicas="$GPUS"
    elif [[ "${CUR:-0}" -gt "$GPUS" ]]; then
      warn "${GPU_MS} has ${CUR} replicas, profile ${PROFILE} needs ${GPUS}. Not scaling down automatically
  (a random machine would be removed). Scale down the extra GPU workloads first, then:
    oc scale ${GPU_MS} -n openshift-machine-api --replicas=${GPUS}
  See docs/INSTALLATION.md#switch-profiles."
    else
      echo "Found ${GPU_MS} with ${CUR} replica(s): ok"
    fi
  else
    SRC="$(oc get machineset -n openshift-machine-api -o name | grep -- '-worker-' | head -1 || true)"
    [[ -n "$SRC" ]] || die "no worker MachineSet to clone (is this an installer-provisioned AWS cluster?)"
    echo "Cloning ${SRC}"
    MS_JSON="$(mktemp)"
    trap 'rm -f "$MS_JSON"' EXIT
    oc get "$SRC" -n openshift-machine-api -o json | python3 -c '
import json, sys
replicas, itype = int(sys.argv[1]), sys.argv[2]
ms = json.load(sys.stdin)
name = ms["metadata"]["name"].replace("-worker-", "-gpu-")
for k in ("uid", "resourceVersion", "creationTimestamp", "generation", "managedFields", "annotations", "ownerReferences"):
    ms["metadata"].pop(k, None)
ms.pop("status", None)
ms["metadata"]["name"] = name
ms["metadata"].setdefault("labels", {})["voice-id/gpu-machineset"] = "true"
ms["metadata"]["annotations"] = {"machine.openshift.io/GPU": "1", "machine.openshift.io/vCPU": "8",
                                 "machine.openshift.io/memoryMb": "32768"}
ms["spec"]["replicas"] = replicas
ms["spec"]["selector"]["matchLabels"]["machine.openshift.io/cluster-api-machineset"] = name
t = ms["spec"]["template"]
t["metadata"]["labels"]["machine.openshift.io/cluster-api-machineset"] = name
t["spec"].setdefault("metadata", {}).setdefault("labels", {})["node-role.kubernetes.io/gpu-worker"] = ""
t["spec"]["taints"] = [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}]
p = t["spec"]["providerSpec"]["value"]
p["instanceType"] = itype
p["blockDevices"][0]["ebs"]["volumeSize"] = 200
json.dump(ms, sys.stdout, indent=1)
' "$GPUS" "$GPU_INSTANCE_TYPE" > "$MS_JSON"
    if [[ $DRY_RUN -eq 1 ]]; then
      python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); t=m["spec"]["template"]["spec"]; p=t["providerSpec"]["value"]; print("[dry-run] MachineSet", m["metadata"]["name"], "replicas", m["spec"]["replicas"], p["instanceType"], "taints", t["taints"], "root", p["blockDevices"][0]["ebs"]["volumeSize"], "GiB")' "$MS_JSON"
    fi
    run oc apply -f "$MS_JSON"
  fi
else
  log "1. GPU MachineSet: skipped (profile minimal needs no GPU)"
fi

if [[ $PROFILE != full ]] && oc get deploy -n "$NAMESPACE" -l 'app in (xtts-v2,fish-native)' -o jsonpath='{.items[*].spec.replicas}' 2>/dev/null | grep -q '[1-9]'; then
  warn "XTTS/Fish are still running in ${NAMESPACE} but profile ${PROFILE} does not include them.
  To release their GPUs: docs/INSTALLATION.md#switch-profiles (this script does not remove them)."
fi

# --- 2. namespace, CPU components, model downloads ----------------------------

log "2. Namespace ${NAMESPACE}, detectors (CPU)$([[ $GPUS -gt 0 ]] && echo ', LLM opponents (downloads start now)')"
if ! oc get namespace "$NAMESPACE" >/dev/null 2>&1; then
  run oc create namespace "$NAMESPACE"
fi
apply_manifest infra/deepfake-detector-base/manifests.yaml
apply_manifest infra/deepfake-detector/manifests.yaml
if [[ $GPUS -ge 2 ]]; then
  apply_manifest infra/qwen2-audio/manifests.yaml
  apply_manifest infra/gemma/manifests.yaml
fi

# --- 3. the game ---------------------------------------------------------------

log "3. Voice ID game (Helm)"
HELM_ARGS=(upgrade --install voice-id "${ROOT}/apps/voice-id/helm/voice-id" -n "$NAMESPACE" -f "$VALUES"
  --set "detector.url=http://deepfake-detector-fine-tuned:8080"
  --set "detectorBase.url=http://deepfake-detector-base:8080")
if [[ $GPUS -ge 2 ]]; then
  HELM_ARGS+=(--set aiJudge.enabled=true --set aiJudge2.enabled=true
    --set "aiJudge.url=https://gemma-kserve-workload-svc.${NAMESPACE}.svc.cluster.local:8000/v1"
    --set "aiJudge2.url=https://qwen2-audio-kserve-workload-svc.${NAMESPACE}.svc.cluster.local:8000/v1")
else
  HELM_ARGS+=(--set aiJudge.enabled=false --set aiJudge2.enabled=false)
fi
[[ -n "$ADMIN_TOKEN_FILE" ]] && HELM_ARGS+=(--set-file "adminToken=${ADMIN_TOKEN_FILE}")
run helm "${HELM_ARGS[@]}"
run oc rollout status deploy/voice-id -n "$NAMESPACE" --timeout=300s
run oc rollout status deploy/deepfake-detector-base -n "$NAMESPACE" --timeout=600s
run oc rollout status deploy/deepfake-detector-fine-tuned -n "$NAMESPACE" --timeout=600s

# --- 4. GPUs, TTS engines, Clone Studio ------------------------------------------

if [[ $GPUS -gt 0 ]]; then
  log "4. Waiting for ${GPUS} allocatable GPU(s) (node provisioning + driver, ~5-10 min)"
  wait_for "${GPUS} allocatable nvidia.com/gpu" "$GPU_TIMEOUT_SEC" enough_gpus
fi

if [[ $PROFILE == full ]]; then
  log "4b. TTS engines and Voice Clone Studio"
  apply_manifest infra/xtts/manifests.yaml
  apply_manifest infra/fish-speech/manifests.yaml
  run helm upgrade --install voice-clone-studio "${ROOT}/apps/voice-clone-studio/helm/voice-clone-studio" \
    -n "$STUDIO_NAMESPACE" --create-namespace -f "$STUDIO_VALUES" \
    --set "providers.xtts.url=http://xtts-v2.${NAMESPACE}.svc.cluster.local" \
    --set "providers.fish.url=http://fish-native.${NAMESPACE}.svc.cluster.local:8080"
  run oc rollout status deploy/xtts-v2 -n "$NAMESPACE" --timeout=1200s
  # Fish downloads ~9 GB in an init container on first start.
  run oc rollout status deploy/fish-native -n "$NAMESPACE" --timeout=2400s
fi

# --- 5. LLM opponents ready ----------------------------------------------------------

if [[ $GPUS -ge 2 ]]; then
  log "5. LLM opponents (downloads + vLLM start; GatewayPreconditionNotMet on the gateway is expected)"
  wait_for "qwen2-audio /v1/models" "$MODEL_TIMEOUT_SEC" model_ready qwen2-audio Qwen2-Audio-7B-Instruct
  wait_for "gemma /v1/models" "$MODEL_TIMEOUT_SEC" model_ready gemma gemma-4-12b
fi

if [[ $PROFILE == full ]]; then
  log "5b. Fish Speech warm-up (first request triggers a ~4 min torch.compile)"
  run oc exec -n "$NAMESPACE" deploy/voice-id -- python3 -c "
import requests, time; t = time.time()
r = requests.post('http://fish-native:8080/v1/tts', json={'text': 'Dobrý den, toto je zkouška.', 'format': 'wav'}, timeout=900)
print('fish warm-up', r.status_code, round(time.time() - t), 's')" || warn "Fish warm-up failed; retry later (docs/INSTALLATION.md#fish-speech-warm-up)"
  # Clone Studio re-registers its voices with XTTS/Fish on startup.
  run oc rollout restart deploy/voice-clone-studio -n "$STUDIO_NAMESPACE"
fi

# --- 6. verify and hand over -------------------------------------------------------------

log "6. Verify"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "[dry-run] would query /api/battle/opponents and /api/admin/clips on the game route"
  ROUTE="<voice-id route>"
else
  ROUTE="$(oc get route voice-id -n "$NAMESPACE" -o jsonpath='{.spec.host}')"
  OPPS="$(curl -sk "https://${ROUTE}/api/battle/opponents" | python3 -c 'import json,sys; print(" ".join(o["id"] for o in json.load(sys.stdin)))' || echo '?')"
  if [[ -n "$ADMIN_TOKEN_FILE" ]]; then ADMIN_TOKEN_VALUE="$(tr -d '\r\n' < "$ADMIN_TOKEN_FILE")"; else ADMIN_TOKEN_VALUE="${VOICEID_ADMIN_TOKEN:-v01c3@r3dh4t!}"; fi
  CLIPS="$(curl -sk -H "Authorization: Bearer ${ADMIN_TOKEN_VALUE}" "https://${ROUTE}/api/admin/clips" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d) if isinstance(d, list) else "? (" + str(d.get("error", "unauthorized")) + ")")' 2>/dev/null || echo '?')"
  echo "  opponents: ${OPPS:-none}"
  echo "  clips:     ${CLIPS}"
fi

log "Done: profile ${PROFILE}"
echo "  Game:   https://${ROUTE}/"
echo "  TV:     https://${ROUTE}/tv"
echo "  Admin:  https://${ROUTE}/admin"
if [[ $PROFILE == full ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then STUDIO_ROUTE="<voice-clone-studio route>"
  else STUDIO_ROUTE="$(oc get route voice-clone-studio -n "$STUDIO_NAMESPACE" -o jsonpath='{.spec.host}' 2>/dev/null || echo '?')"; fi
  echo "  Studio: https://${STUDIO_ROUTE}/   (unauthenticated: do not publish)"
fi
cat <<EOF

Next steps:
  1. Import the private seed pack (the game has no clips until you do):
       export VOICEID_ADMIN_TOKEN=...   # read -s, not in shell history
       python3 apps/voice-id/tools/seedpack.py import --game https://${ROUTE} \\
         --file ~/voice-id-audio/seedpacks/<pack>.seedpack.zip --mode skip
     or /admin -> Seed pack & backup -> Import. See docs/SEEDING.md.
  2. Run the smoke test in docs/INSTALLATION.md (section 6) and play a game in the booth browser.
EOF

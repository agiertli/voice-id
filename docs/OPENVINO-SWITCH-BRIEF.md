# Decision brief: switch Gary / Gary 2.0 to OpenVINO Model Server

**Recommendation: technically ready, switch after one battle on the OpenVINO path.** Parity is exact on all 129 live clips. OVMS is about 2.8x more
efficient per CPU, and the cold-cache "Preparing the game" for Gary 2.0 drops from about 18 s to about 6 s. It runs on a supported RHOAI runtime, and rollback is one
Helm value. The costs are about 2x memory requests and one small custom adapter. The only gate not executed is the browser battle on staging (blocked by permissions).
Either run the staging commands in section 9, or switch live and immediately play one Gary and one Gary 2.0 battle, keeping the legacy Deployments
running for instant rollback.

Background and payload contract: [OPENVINO-DETECTOR.md](OPENVINO-DETECTOR.md). Everything below was measured on
2026-11-24 on the live cluster, against the live game's 129 clips (seed pack `voice-id-seedpack-20261124-2233`).

## 1. What is proven

| Gate | Evidence | Result |
|------|----------|--------|
| Payload contract | Client preprocessing is bit-identical to serve.py + `Wav2Vec2FeatureExtractor` (max \|Δinput\| = 0.0) for both length policies. IR logits = PyTorch logits within 1.7e-5 | PASS |
| Parity, all 129 live clips, legacy `/predict` vs adapter `/predict`, in-cluster | Gary 2.0: **0 flips**, 0 failures, max \|Δp_fake\| 0.0001 (= serve.py's 4-decimal rounding), mean 2e-6. Gary base: **0 flips**, 0 failures, max 0.0001, mean 2e-6 | PASS |
| Includes the clips added since the first test | 20 Martin clips (fish + xtts) and the newer XTTS stock voices are all in the 129 | PASS |
| Game needs zero code change | The adapter serves the same `/predict` `/health` `/ready` and the same JSON fields (`verdict`, `confidence`, `prob_fake`, `duration`) | PASS |
| Staging game, end-to-end battle in a browser | Not executed: namespace/Helm install blocked by the session's permission policy (commands in section 9) | NOT RUN |
| Performance not worse | See section 2 | PASS |

A pitfall found on the way: the two legacy images were built from **different serve.py revisions**. Gary 2.0 scores the first
10 s, padded or truncated to 160000 samples. Gary base scores the whole clip at native length. The adapter reproduces each one
(`INPUT_LENGTH=160000` / `native`), so game balance is unchanged.

## 2. Benchmark (inside the cluster, identical audio, same node)

All pods run on the same 64-vCPU node (g6.16xlarge). The load generator is a pod in `voice-id-ovms`, so there is no port-forward overhead.
CPU limits are identical: legacy detector **4 CPU**, OVMS predictor **4 CPU** (+ adapter, limit 1 CPU, measured ~0.1–0.3 CPU).

| Model | Test | Legacy Flask/PyTorch | OVMS + adapter | Speed-up |
|-------|------|----------------------|----------------|----------|
| Gary 2.0 | (a) single request, all 129 clips, p50 / p95 | 2709 / 2902 ms | **1216 / 1263 ms** | 2.2x |
| Gary 2.0 | (b) 5 parallel (pre-battle), wall p50 / p95, 20 batches | 17.9 / 18.1 s | **6.1 / 6.2 s** | 2.9x |
| Gary 2.0 | (c) 10 clients x 60 s: throughput, latency p50 / p95, errors | 0.28 req/s, 34.8 / 38.2 s, 0 | **0.81 req/s**, 11.1 / 17.3 s, 0 | 2.9x |
| Gary 2.0 | throughput per CPU (limit 4) | 0.070 req/s/CPU | **0.203 req/s/CPU** | 2.9x |
| Gary base | (a) single request, p50 / p95 | 1193 / 1995 ms | **434 / 832 ms** | 2.7x |
| Gary base | (b) 5 parallel, wall p50 / p95 | 8.2 / 9.4 s | **2.5 / 2.8 s** | 3.3x |
| Gary base | (c) 10 clients x 60 s | 0.45 req/s, 17.2 / 39.7 s, 0 | **1.23 req/s**, 4.8 / 17.9 s, 0 | 2.7x |
| Gary base | throughput per CPU (limit 4) | 0.112 req/s/CPU | **0.307 req/s/CPU** | 2.7x |

(d) CPU and memory during (c), from `oc adm top` every 15 s:

| Pod | CPU avg / max | Memory max |
|-----|---------------|------------|
| legacy `deepfake-detector-fine-tuned` | 3.79 / 4.00 cores | 794 Mi |
| legacy `deepfake-detector-base` | 3.86 / 4.00 cores | 975 Mi |
| `gary-finetuned-predictor` (OVMS) + adapter | 3.66 / 3.94 + 0.01 cores | 1486 Mi + 146 Mi |
| `gary-base-predictor` (OVMS) + adapter | 3.71 / 3.94 + 0.01 cores | 2615 Mi + 118 Mi |

Both backends saturate the same 4-CPU limit, so no reduced-limit rerun was needed. OVMS does about 2.7–2.9x more work per CPU.
For the game this means "Preparing the game" for Gary 2.0 (5 clips) drops from about 18 s to about 6 s when the answer cache is cold.

The first load run found a robustness bug, which is now fixed. Under 10 concurrent clients, OVMS's 4 default REST workers were all busy, so the adapter's
`/ready` check (which called OVMS) timed out and Kubernetes removed the adapter from its Service (15,843 fast errors in 60 s).
Fixes: the adapter answers `/ready` from recent successful calls or in-flight work (image v0.1.1), and OVMS runs `--rest_workers=16`.
The rerun had 0 errors. The 5-parallel pattern the game actually uses had 0 errors in both runs.

## 3. Resource footprint (per model)

| Per model | Legacy | OVMS + adapter |
|-----------|--------|----------------|
| CPU request / limit | 2 / 4 | 2.2 / 5.2 (predictor 2/4, kube-rbac-proxy 0.1/0.2, adapter 0.1/1) |
| Memory request / limit | 2Gi / 4Gi | 4.2Gi / 4.6Gi (Gary 2.0), 4.2Gi / 6.6Gi (Gary base) |
| Memory used (anon) | ~0.7–1.0 GiB, plus the 1.26 GB weights mmapped from the image (page cache, not charged to the pod) | Gary 2.0 ~1.5 GiB + 1.26 GB mmapped `model.bin`; Gary base up to ~2.7 GiB (per-shape buffers for native-length input); adapter ~0.1 GiB |
| CPU at saturation | ~3.8 cores | ~3.7 cores (+0.01 adapter) |
| Idle CPU | ~0 | ~0 |
| Image | custom `python:3.11-slim` + torch (~2.5 GB, weights baked in) | Red Hat OVMS image + 442 MB UBI adapter (no weights) |
| Storage | none (weights in image) | 5Gi gp3 PVC (1.26 GB used) |

Net effect: OVMS needs **about 2x the memory requests** (8.4Gi vs 4Gi for both models) and about 0.8–1.7 GiB more real memory per model.
In return it delivers **about 2.8x the throughput per CPU**. CPU requests are about the same.

## 4. Supportability

- **Supported runtime.** OVMS is the OpenShift AI built-in `kserve-ovms` ServingRuntime (Red Hat image `registry.redhat.io/rhoai/odh-openvino-model-server-rhel9`),
  deployed as a standard KServe `InferenceService`. The legacy detectors are a custom Flask/PyTorch image (`python:3.11-slim`, unsupported base) maintained by us.
- **Visibility.** Both models appear in the OpenShift AI dashboard (project `voice-id-ovms`, label `opendatahub.io/dashboard`),
  with KServe status/conditions and Prometheus metrics on :8888 (`ovms_requests_success`, `ovms_inference_time_us`, …).
- **Lifecycle.** The model is versioned (`<model>/1/`). The runtime upgrades with RHOAI, and the model can move to a Model Registry / OCI modelcar later.
- **Remaining custom code.** The adapter is about 100 lines on UBI9-minimal (numpy/scipy/soundfile). It holds no weights, carries the preprocessing
  contract, and is covered by the parity test (`apps/deepfake-detector/ovms_bench.py`).

## 5. Switch procedure (voice-id release, about 2 min, no data migration)

Prerequisite: the chart with the `detectorBackend` switch (`apps/voice-id/helm/voice-id/templates/deployment.yaml`) is committed.
The live release uses chart defaults only (no user-supplied values), so upgrade from the **same chart revision that is deployed**.

```bash
export KUBECONFIG=~/.kube/voice-id-cluster.kubeconfig
oc get isvc -n voice-id-ovms                     # gary-base, gary-finetuned READY=True
oc get deploy -n voice-id-ovms adapter-gary-base adapter-gary-finetuned   # 1/1
helm upgrade voice-id apps/voice-id/helm/voice-id -n voice-id --reuse-values --set detectorBackend=openvino
oc set env deploy/voice-id -n voice-id --list | grep DETECTOR
#   DETECTOR_URL=http://adapter-gary-finetuned.voice-id-ovms.svc.cluster.local:8080
#   DETECTOR_BASE_URL=http://adapter-gary-base.voice-id-ovms.svc.cluster.local:8080
#   DETECTOR_BACKEND=openvino
```
Verify:
1. `oc logs deploy/voice-id -n voice-id | grep DETECTOR` shows verdict lines, and `oc logs -n voice-id-ovms deploy/adapter-gary-finetuned` shows `predict verdict=`.
2. Optional: clear the opponent answer cache so every answer is recomputed via OVMS: `POST /api/admin/opponent-cache/clear`, then `.../warm`.
   This isn't required, because parity means cached answers are identical.
3. Play one battle against Gary and one against Gary 2.0.
4. Watch `ovms_requests_fail` (expected 0) and adapter 5xx in the logs during the first session.

## 6. Rollback (seconds)

```bash
helm upgrade voice-id apps/voice-id/helm/voice-id -n voice-id --reuse-values --set detectorBackend=legacy
```
Keep the legacy `deepfake-detector-*` Deployments running during the soak, so rollback is just this one command.

## 7. Decommission (after a soak period: at least one event / a few days with 0 adapter errors)

1. Scale the legacy detectors to 0, keeping them for a fast restore: `oc scale deploy/deepfake-detector-base deploy/deepfake-detector-fine-tuned -n voice-id --replicas=0`.
2. After another event, delete the Deployments, Services and Routes (`deepfake-detector-*` in `voice-id`, `infra/deepfake-detector*/`), and update
   `install.sh` / profiles / docs (Rule Two). The legacy detector Routes are currently public; removing them is a security win.
3. Move the adapter image to quay (`quay.io/agiertli/voice-id-ovms-adapter:v0.1.0`) and add `infra/openvino-detector/` to the installer profiles.

## 8. Risks and open points

1. **The staging browser battle was not executed by me.** The staging namespace + Helm install was blocked by this session's permission
   policy (see gate summary). The adapter's HTTP contract is proven by parity on all 129 clips through the same `/predict` the game calls, but nobody
   has clicked through a battle on the OpenVINO path yet. The commands to do it are in the gate summary.
2. **Gary base length contract.** The adapter keeps native length (identical to today). If the base image were ever rebuilt from today's
   serve.py, it would switch to 160000 and 30/110 verdicts would change. This is now pinned in `adapter.yaml` (`INPUT_LENGTH`) and documented.
3. **Memory.** Gary base on OVMS grows a few MB per new clip length (CPU plugin per-shape buffers), so it has a 6Gi limit. With many more clips,
   watch `memory.current`. A fallback is `INPUT_LENGTH` padding to buckets, but that would change results, so it's not recommended.
4. **Runtime version.** The template advertises OVMS v2026.1.0, but the image is 2026.2.0. An RHOAI upgrade can change the runtime image, so re-run
   `ovms_bench.py --phases parity` after every RHOAI upgrade. It takes about 15 min and must give 0 flips.
5. **Headless predictor Service.** In-cluster calls must use `http://<isvc>-predictor.voice-id-ovms.svc.cluster.local:8888`. The ISVC status URL (port 80) does not work.
6. **Adapter image location.** It is in the cluster's internal registry, built by the `voice-id-ovms-adapter` BuildConfig. The quay push failed (stale credentials).
   Push `quay.io/agiertli/voice-id-ovms-adapter:v0.1.1` before relying on it across clusters.
7. **No auth / NetworkPolicy** on OVMS and adapters. This is the same posture as the legacy detectors, which even have public Routes. Optionally add a NetworkPolicy:
   ingress to `adapter-*` only from namespace `voice-id`, and to predictors only from the adapters.
8. **Single replica** each, same as legacy. OVMS restart and model load take about 10 s.
9. **Chart state.** Commit `d5c0e04` already contains the `detectorBackend` values, but **not** the template logic (`templates/deployment.yaml`, uncommitted).
   Commit the template before the switch. Until then `detectorBackend` has no effect.

## 9. Gate summary

| Gate | Result |
|------|--------|
| 0 verdict flips on all live clips (129, incl. the 20 Martin + newer XTTS clips), legacy vs adapter, in-cluster | **PASS**: Gary 2.0 0/129, Gary base 0/129, max \|Δp\| 0.0001 (v0.1.0 run and again with the deployed v0.1.1: identical) |
| Staging game: full battle vs Gary and Gary 2.0 in a browser | **NOT RUN (blocked)**: creating `voice-id-staging` + `helm install` was denied by the session's permission policy |
| Benchmark not worse | **PASS**: 2.2–3.3x faster on every metric, 0 errors after the v0.1.1 fix, same CPU limit |
| Chart switch renders both ways (`helm template`) | **PASS**: `legacy` gives the legacy URLs, `openvino` gives the adapter URLs, an invalid value fails, and stored values without `openvino.*` still render |

Staging commands for the owner (or an agent with permission):
```bash
export KUBECONFIG=~/.kube/voice-id-cluster.kubeconfig
oc create namespace voice-id-staging
helm upgrade --install voice-id apps/voice-id/helm/voice-id -n voice-id-staging \
  --set conference.name="OpenVINO staging" --set detectorBackend=openvino
oc rollout status deploy/voice-id -n voice-id-staging
STAGING=https://$(oc get route voice-id -n voice-id-staging -o jsonpath='{.spec.host}')
python3 apps/voice-id/tools/seedpack.py import --game "$STAGING" --file ~/voice-id-audio/seedpacks/voice-id-seedpack-20261124-2233.seedpack.zip --mode skip
# token: $VOICEID_ADMIN_TOKEN (image default); then play a battle vs Gary and Gary 2.0 at $STAGING
```

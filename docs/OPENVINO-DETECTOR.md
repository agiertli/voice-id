# Deepfake detectors on OpenVINO Model Server (OpenShift AI)

The two Wav2Vec2 detectors ("Gary" = `wav2vec2-base`, "Gary 2.0" = `wav2vec2-v4a-no-weights`) served by
the **built-in `kserve-ovms` runtime** of OpenShift AI 3.5.4 (template annotation says runtime-version v2026.1.0; the image actually reports **OVMS 2026.2.0**). It runs on CPU with an FP32 IR.

**Status: this is the live serving path since 2026-11-25** (Helm `detectorBackend: openvino`, the default). Everything runs in the
game's namespace `voice-id`: ServingRuntime `kserve-ovms`, InferenceServices `gary-base` / `gary-finetuned`, model PVCs, and the
adapters `adapter-gary-base` / `adapter-gary-finetuned` that the game calls. The legacy Flask/PyTorch Deployments were decommissioned
the same day; their manifests (`infra/deepfake-detector*/`) remain for an emergency rollback only. Install: `install.sh` (all profiles)
or [INSTALLATION.md](INSTALLATION.md) step 2. Evidence: [OPENVINO-SWITCH-BRIEF.md](OPENVINO-SWITCH-BRIEF.md).

> Sections 4 and 5 below are the historical record of the evaluation (run in a separate namespace `voice-id-ovms`, since deleted).

| File | Purpose |
|------|---------|
| `apps/deepfake-detector/export_openvino.py` | HF checkpoint -> ONNX (opset 17, dynamic batch/sequence) -> FP32 OpenVINO IR, plus a PyTorch vs IR logits check |
| `apps/deepfake-detector/ovms_adapter/ovms_client.py` | Reference client: serve.py preprocessing + KServe v2 call + serve.py post-processing (`MODEL_LENGTH` per model) |
| `apps/deepfake-detector/ovms_adapter/` (`app.py`, `Dockerfile`) | serve.py-compatible adapter (`/predict`, `/health`, `/ready`) used by the game when `detectorBackend=openvino` |
| `apps/deepfake-detector/ovms_bench.py` | In-cluster parity + benchmark, legacy `/predict` vs adapter `/predict` |
| `apps/deepfake-detector/ovms_parity.py` | Runs every clip through the current Flask detector and through OVMS, then reports flips, p_fake diffs and latency |
| `infra/openvino-detector/manifests.yaml` | PVCs, ServingRuntime (from template `kserve-ovms`), 2 InferenceServices (namespace `voice-id`; no Namespace object) |
| `infra/openvino-detector/model-loader.yaml` | Temporary pod used to `oc cp` the IR onto the PVCs |
| `infra/openvino-detector/adapter.yaml` | `adapter-gary-base` / `adapter-gary-finetuned` Deployments + Services, image `quay.io/agiertli/voice-id-ovms-adapter:v0.1.1` |
| `infra/openvino-detector/adapter-build.yaml` | Optional: ImageStream + BuildConfig to build the adapter image in-cluster instead |

Model weights stay out of git because of their size (2 x 1.26 GB). They live in `~/voice-id-audio/ovms/repo/` and on the PVCs.
Weights contain no recordings. The **clips** used for the parity test are Rule Zero data: they stayed local and were deleted afterwards.

---

## 1. Payload contract (derived from `serve.py`, verified bit-exact)

The whole contract comes down to one point: **OVMS only runs the neural network. All audio work happens client-side
and must reproduce serve.py exactly.** The model sees a normalized float waveform. It does not see a file.

> **The two running detectors do NOT share one contract.** The images were built from different `serve.py` revisions:
>
> | Opponent | Running image | serve.py revision | Input length |
> |----------|---------------|-------------------|--------------|
> | Gary 2.0 (`gary-finetuned`) | `quay.io/agiertli/deepfake-detector:v2.0.0` | current (`apps/deepfake-detector/serve.py`) | **fixed 160000** (truncate/pad, step 6) |
> | Gary (`gary-base`) | `quay.io/agiertli/deepfake-detector-base:v1.0.3` | older (no `preprocess_audio`, `padding=True`) | **native clip length**, whole clip, no 10 s cap (step 6 skipped) |
>
> The model weights in both images are byte-identical to `models/wav2vec2-base` and `models/wav2vec2-v4a-no-weights`
> (sha256 checked in the pods). The first parity run applied the 160000 contract to Gary base and got
> 30/110 verdict flips (max |Δp_fake| 0.82). Switching to native length restored parity. `ovms_client.MODEL_LENGTH`
> encodes this per model. If the base image is ever rebuilt from today's `serve.py`, its contract (and its verdicts)
> change to the fixed-length one.

### Client-side preprocessing (order matters)

| # | Step | serve.py source | Notes |
|---|------|-----------------|-------|
| 0 | (game) `ffmpeg -i clip -ac 1 -ar 22050 -sample_fmt s16 -f wav` | `app.py convert_to_wav_pcm` | The game always sends 22.05 kHz mono s16 WAV to the detector |
| 1 | Decode to float32: `int16 / 32768` | `sf.read(dtype="float32")` | `wave` module + numpy gives bit-identical values |
| 2 | Mono: mean over channels | `audio.mean(axis=1)` | no-op after step 0 |
| 3 | Resample to **16 kHz** with `scipy.signal.resample_poly(audio, 16000/g, sr/g)`, cast to float32 | same | 22050 -> 16000 = up 320 / down 441. **Use scipy. librosa or ffmpeg resampling gives different numbers** |
| 4 | **RMS normalization** to 0.05: `audio *= 0.05 / rms` if `rms > 1e-6`, then `clip(-1, 1)` | `normalize_audio` | Applied to the *full* clip, before truncation. Not in the HF preprocessor config, so it is easy to miss |
| 5 | `duration = len(audio)/16000` | | reported only |
| 6 | **Gary 2.0: truncate or right-pad with zeros to exactly 160000 samples** (10 s). **Gary base: skip** (send the full clip) | `preprocess_audio` (current serve.py only) | Gary 2.0 scores only the first 10 s. There is no chunking |
| 7 | **Zero-mean / unit-variance** over every sample sent, *including the zero padding*: `(x - x.mean()) / sqrt(x.var() + 1e-7)` (float32) | `Wav2Vec2FeatureExtractor(do_normalize=True)` | Because step 6 pads first, the extractor's attention mask is all ones, so its statistics include the padding |
| 8 | Attention mask | `return_attention_mask=True` | **Always all ones, so it is dropped.** The exported graph has no mask input. Feeding the true speech mask would *change* results versus serve.py |

### Model tensors (both models identical)

| | Name | Datatype | Shape | Values |
|---|------|----------|-------|--------|
| Input | `input_values` | `FP32` | `[batch, sequence]`, both dynamic (`[-1,-1]`). **Gary 2.0: `[1, 160000]`. Gary base: `[1, n_samples_16k]`** | zero-mean, unit-variance floats. Peaks reach roughly ±10 to ±25 on game clips (not ±1) |
| Output | `logits` | `FP32` | `[batch, 2]` | raw logits, index 0 = `real`, **index 1 = `fake`** (`config.json` id2label) |

Batch > 1 works with Gary 2.0 (fixed length), but the game sends one clip per call. Gary base with batching would need padding *and* a mask, which changes results, so keep batch = 1.

### Post-processing (serve.py)

```
p_fake = softmax(logits[0])[1]
p_fake < 0.3  -> REAL       confidence = 1 - p_fake
p_fake > 0.7  -> SYNTHETIC  confidence = p_fake
otherwise     -> UNCERTAIN  confidence = 1 - |p_fake - 0.5| * 2
```
Game mapping (`_detector_analyze`): `SYNTHETIC` -> guess "ai", everything else -> "human".

### KServe v2 REST example

Endpoint inside the cluster: `http://<isvc>-predictor.voice-id.svc.cluster.local:8888/v2/models/<isvc>/infer`
(`<isvc>` = `gary-base` | `gary-finetuned`; OVMS's model name equals the ISVC name; the predictor Service also maps port 80 to 8888).

JSON request (Gary 2.0: 160000 numbers, about 3.5 MB of JSON text):
```json
{
  "inputs": [
    {"name": "input_values", "shape": [1, 160000], "datatype": "FP32",
     "data": [0.0123, -0.0441, 0.1087, "... 160000 values total ..."]}
  ]
}
```
Response:
```json
{
  "model_name": "gary-finetuned",
  "model_version": "1",
  "outputs": [
    {"name": "logits", "shape": [1, 2], "datatype": "FP32", "data": [-1.9994410276412964, 2.0453832149505615]}
  ]
}
```
-> p_fake = softmax([-1.9994, 2.0454])[1] = 0.9828 -> `SYNTHETIC` (a real response from `gary-finetuned` on a Fish S2 Pro clip).
The binary and JSON variants returned bit-identical logits.

Binary variant (what `ovms_client.py` uses by default, 640 KB payload for 160000 samples): `Content-Type: application/octet-stream`,
`Inference-Header-Content-Length: <len(json header)>`, body = JSON header with
`"parameters": {"binary_data_size": 640000}` on the input, followed by 640000 bytes of little-endian float32.

Other endpoints: `GET /v2/models/<isvc>/ready`, `GET /v2/models/<isvc>` (metadata: shows `input_values [-1,-1]`, `logits [-1,2]`).

### Runtime pitfalls handled

- **bf16 on CPU**: OpenVINO's CPU plugin silently switches to bf16 inference on CPUs with AVX512_BF16/AMX
  (Sapphire Rapids and newer). That would break parity on some nodes but not others. The ISVCs pin
  `--plugin_config={"INFERENCE_PRECISION_HINT":"f32","PERFORMANCE_HINT":"LATENCY"}`.
- **FP16 weight compression**: `ov.save_model(..., compress_to_fp16=False)`. `ovc` compresses to FP16 by default.
  Both are noted because "FP32 IR" does not happen by default.
- **IR/runtime versions**: the IR was produced with `openvino==2026.1.0`, the version the template advertises. The image is actually OVMS 2026.2.0, which loads it fine because older IR on a newer runtime is supported. Don't export with a *newer* OpenVINO than the server.
- **Export**: legacy TorchScript ONNX exporter (`dynamo=False`) with eager attention, and a wrapper that returns only `logits`.

---

## 2. Export and local verification

```bash
python3.12 -m venv /tmp/ovms-venv
/tmp/ovms-venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
/tmp/ovms-venv/bin/pip install "transformers<5" onnx onnxscript "openvino==2026.1.*" soundfile scipy numpy
cd apps/deepfake-detector
/tmp/ovms-venv/bin/python export_openvino.py --model-dir models/wav2vec2-base           --out ~/voice-id-audio/ovms/repo/gary-base
/tmp/ovms-venv/bin/python export_openvino.py --model-dir models/wav2vec2-v4a-no-weights --out ~/voice-id-audio/ovms/repo/gary-finetuned
```
Tested with torch 2.15.0 CPU, transformers 4.57.6, onnx 1.23.2, openvino 2026.1.0.

Local PyTorch vs IR (CPU, f32) on 8 inputs per model: random noise, zero-padded speech-like signals, batch=2, seq=48000:

| Model | worst max\|Δlogit\| | p_fake identical to 4-5 dp |
|-------|--------------------|----------------------------|
| gary-base | 1.5e-05 | yes |
| gary-finetuned | 1.7e-05 | yes |

Client preprocessing (`ovms_client.preprocess`) vs serve.py + `AutoFeatureExtractor` on 19 real game clips (every 6th clip):
- fixed 160000 (current serve.py, Gary 2.0): **max |input_values diff| = 0.0 (bit-exact)**, attention mask all ones in every case;
- native length (older serve.py in the Gary base image, `padding=True`): **max |input_values diff| = 0.0 (bit-exact)**.

---

## 3. Deployment

`install.sh` does all of this for every profile (`--ovms-model-dir`, default `~/voice-id-audio/ovms/repo`). By hand:

```bash
export KUBECONFIG=<cluster kubeconfig>
oc label namespace voice-id opendatahub.io/dashboard=true --overwrite
# 2 PVCs (gp3-csi, 5Gi each), ServingRuntime kserve-ovms; ISVCs last
python3 -c "d=open('infra/openvino-detector/manifests.yaml').read().split('\n---\n');print('\n---\n'.join(x for x in d if 'kind: InferenceService' not in x))" | oc apply -f -
oc apply -f infra/openvino-detector/model-loader.yaml
oc wait --for=condition=Ready pod/ovms-model-loader -n voice-id
oc cp ~/voice-id-audio/ovms/repo/gary-base      voice-id/ovms-model-loader:/models/gary-base-models/gary-base
oc cp ~/voice-id-audio/ovms/repo/gary-finetuned voice-id/ovms-model-loader:/models/gary-finetuned-models/gary-finetuned
oc exec -n voice-id ovms-model-loader -- sha256sum /models/*/*/1/model.bin   # compare with local shasum
oc delete pod ovms-model-loader -n voice-id            # RWO: free the volumes
oc apply -f infra/openvino-detector/manifests.yaml      # creates the 2 InferenceServices
oc wait --for=condition=Ready isvc gary-base gary-finetuned -n voice-id --timeout=400s
# adapters: public image quay.io/agiertli/voice-id-ovms-adapter:v0.1.1 (optional in-cluster build: adapter-build.yaml)
oc apply -f infra/openvino-detector/adapter.yaml
oc rollout status deploy/adapter-gary-base deploy/adapter-gary-finetuned -n voice-id
```

Loading a new IR into a running system: stop the predictors first (RWO volumes), e.g. `oc delete isvc gary-base gary-finetuned -n voice-id`,
load as above, re-apply `manifests.yaml`, then clear that opponent's answer cache (`POST /api/admin/opponent-cache/clear` with
`{"opponent": "gary-finetuned"}`) and warm it again.

What gets created:
- `ServingRuntime/kserve-ovms`: verbatim objects from template `redhat-ods-applications/kserve-ovms`, plus the template annotations the dashboard adds.
- `InferenceService/gary-base` and `gary-finetuned`: `serving.kserve.io/deploymentMode: Standard` (raw Deployment, same as the
  dashboard-created llama example), `modelFormat openvino_ir`, `storageUri: pvc://<pvc>/<model>`, CPU 2 request / 4 limit, memory 4Gi, no GPU,
  `security.opendatahub.io/enable-auth: "false"`, no Route (internal only), and the `--plugin_config` pin described above.
- Storage layout on each PVC: `<model>/1/model.xml` + `model.bin` (1.26 GB). The predictor sees it as `/mnt/models/1/`.

Tests from a workstation: port-forward **the Deployment, not the Service**. `svc/<isvc>-predictor` is headless (port 80 -> 8888),
and `oc port-forward svc/...` then tries pod port 80 and fails:
```bash
oc port-forward -n voice-id deploy/gary-finetuned-predictor 18888:8888 &
curl -s localhost:18888/v2/models/gary-finetuned      # metadata
python3 apps/deepfake-detector/ovms_adapter/ovms_client.py --url http://localhost:18888 --model gary-finetuned clip.wav
```

---

## 4. Parity on the game clips

Method: all **110 clips** of seed pack `voice-id-seedpack-20261124-2139` (79 wav, 20 m4a, 10 flac, 1 mp3, durations 2-60 s;
the 12 XTTS clips imported later are not in that pack). The pack was unpacked locally and deleted afterwards. Each clip was converted
with the game's ffmpeg call, then the same WAV bytes went to (a) the running `svc/deepfake-detector-*` `/predict` in `voice-id`
(read-only, via port-forward) and (b) `ovms_client.predict` -> OVMS in the evaluation namespace `voice-id-ovms` (via port-forward).
Command: `apps/deepfake-detector/ovms_parity.py`.

| Model | n | Failures | Verdict flips | max \|Δp_fake\| | mean \|Δp_fake\| |
|-------|---|----------|---------------|----------------|-----------------|
| gary-finetuned (fixed 160000) | 110 | 0 | **0** | 0.000126 | 0.000027 |
| gary-base (native length) | 110 | 0 | **0** | 0.000071 | 0.000023 |
| *gary-base with the 160000 contract (wrong)* | *110* | *0* | ***30*** | *0.825* | *0.190* |

serve.py rounds `prob_fake` to 4 decimals, so differences below about 5e-5 are rounding. The IR itself matches PyTorch to about 1e-5 in logits.

Latency per clip (sequential calls from a workstation through `oc port-forward`; the tunnel overhead is the same on both sides;
the OVMS figure includes client-side decode and preprocessing):

| Model | Flask/torch p50 / p95 | OVMS + client p50 / p95 | OVMS server-side inference (metrics) |
|-------|----------------------|-------------------------|--------------------------------------|
| gary-finetuned | 3227 / 4407 ms | **1932 / 3543 ms** | mean 1.22 s, 114/119 requests under 1.27 s |
| gary-base | 1624 / 3569 ms | **968 / 2489 ms** | mostly 0.4-1.3 s (depends on clip length) |

Both run on the same 4-CPU limit. OVMS is about 1.6x faster at p50, mostly because of OpenVINO's CPU kernels.
In-cluster calls from the game would drop the tunnel part.

Memory (cgroup `memory.current`, after every clip had been sent):
- gary-finetuned is flat at about 2.6 GiB.
- gary-base grows a few MB per *new* input length, because the CPU plugin keeps buffers per shape. It measured 2.6 GiB after 135 requests on a fresh pod,
  and `oc adm top` showed 3.8 GiB after 220 mixed-shape requests. Its limit is therefore 6 GiB, versus 4 GiB for Gary 2.0.

Side effect observed during the test: the **existing** `deepfake-detector-base` pod in `voice-id` was **OOMKilled once**
(2 Gi limit, exit 137, 22:42 local time) during the first sequential pass, then restarted automatically (about 1 min without Gary base).
It never reached HIGH-MEM during the later passes (peak about 730 Mi). It is a pre-existing headroom issue of that Deployment
(whole-clip inference, 2 Gi limit), not caused by OVMS, but sequential load is enough to trigger it.

---

## 5. Integration (option B implemented and live)

Option B below is implemented and has been the live path since 2026-11-25: see `apps/deepfake-detector/ovms_adapter/`,
`infra/openvino-detector/adapter.yaml`, the Helm switch `detectorBackend`, and [OPENVINO-SWITCH-BRIEF.md](OPENVINO-SWITCH-BRIEF.md).
The text below is the original decision record; namespace names in it refer to the evaluation setup.

### Where should preprocessing live?

The preprocessing is about 30 lines of numpy/scipy (decode, resample, RMS, pad, normalize). OVMS cannot do it:
the built-in runtime only runs the IR, and OVMS's Python/MediaPipe nodes are not exposed by the `kserve-ovms` template.

| Option | Pros | Cons |
|--------|------|------|
| **A. In the game backend** (`app.py` imports `ovms_client.preprocess/infer/postprocess`) | No extra service; one fewer hop; the game already ships ffmpeg, numpy and scipy; the detector becomes a pure, supported RHOAI model (dashboard, metrics, Model Registry); a 640 KB binary tensor per call instead of a WAV upload | Couples the game image to the model's preprocessing contract, so a retrained model with a different contract needs a game rebuild; numpy/scipy versions in the game image become parity-relevant |
| **B. Thin adapter** (keep a tiny `/predict` service: same API as serve.py, but it calls OVMS instead of torch) | Zero game change: switch by changing `DETECTOR_URL`/`DETECTOR_BASE_URL`; preprocessing stays versioned next to the model; adapter image is about 150 MB (no torch) instead of about 2.5 GB | One more Deployment to run and secure; an extra hop (in-cluster, about 1-2 ms); still custom code outside the RHOAI-supported path |
| C. KServe transformer (`spec.transformer` on the ISVC) | KServe-native pre/post pipeline, one ISVC object | A custom transformer image and the KServe Python SDK, so it is the most moving parts; support for transformers in RawDeployment/Standard mode on RHOAI should be checked first |

**Recommendation: B first, then A.** The adapter keeps the `/predict` contract the game already uses, so the switch is
a pure configuration change with an instant rollback, and the parity test above stays valid for it unchanged (it is the same code).
Once the adapter has run a conference without issues, folding `ovms_client` into the game (A) removes the extra service.
Do A only if the model contract is considered frozen. V4-A is the declared final detector, so it effectively is.

### Switch plan (option B)

0. Decide the Gary base contract first. Either keep native length (exactly today's behaviour), or move it to the fixed
   160000 contract of the current serve.py, which changes 30 of 110 verdicts in the parity set. That second option is a *game balance* change, not an infra one.
1. Build `deepfake-detector-ovms-adapter` (python:3.11-slim or UBI9 python + numpy + scipy + flask/gunicorn + `ovms_client.py`),
   one Deployment per model or one with `?model=` routing. Keep the response JSON identical to serve.py and keep
   `ovms_client.MODEL_LENGTH` (Gary 2.0 = 160000, Gary base = native).
2. Deploy it **in `voice-id-ovms`** and point it at `http://gary-<x>-predictor.voice-id-ovms.svc.cluster.local:8888`.
   Re-run `ovms_parity.py` against the adapter (flask_url = old service, ovms side = adapter `/predict`).
3. Switch: set Helm values `detector.url` / `detectorBase.url` (env `DETECTOR_URL` / `DETECTOR_BASE_URL`) to the adapter services and `helm upgrade`.
   Add a NetworkPolicy in `voice-id-ovms` that allows ingress from namespace `voice-id` only.
4. Watch `DETECTOR verdict=` log lines and OVMS metrics (`ovms_requests_success`, `ovms_request_time_us`) during one game session.
5. Keep the old `deepfake-detector-*` Deployments running (or scaled to 0, not deleted) for at least one event.

### Footprint (per model, unchanged in production)

| Item | Requests / limits | Measured |
|------|-------------------|----------|
| gary-finetuned predictor | 2 CPU / 4 CPU, 4Gi / 4Gi (+ kube-rbac-proxy 100m/64Mi) | ~2.6 GiB RSS, about 0 CPU when idle |
| gary-base predictor | 2 CPU / 4 CPU, 4Gi / 6Gi (+ kube-rbac-proxy) | ~2.6 GiB RSS, about 0 CPU when idle |
| PVCs | 2 x 5Gi gp3 | 1.26 GB used each |

The cluster reserves 4 CPU and 8 GiB in requests while idle. Both pods landed on the 64-vCPU control-plane/worker node.
Scale to zero without deleting anything (this disables Gary and Gary 2.0 in the game): `oc patch isvc gary-base gary-finetuned -n voice-id --type merge -p '{"metadata":{"annotations":{"serving.kserve.io/stop":"true"}}}'`.

### Rollback (emergency)

The legacy Deployments are decommissioned. Re-apply `infra/deepfake-detector-base/manifests.yaml` and `infra/deepfake-detector/manifests.yaml`
in `voice-id`, wait for both rollouts (about 60 s model load), then `helm upgrade voice-id apps/voice-id/helm/voice-id -n voice-id --reuse-values --set detectorBackend=legacy`
(or `./install.sh --profile <p> --legacy-detectors`). No data migration; the game DB is untouched. See
[INSTALLATION.md](INSTALLATION.md#detector-rollback-legacy).

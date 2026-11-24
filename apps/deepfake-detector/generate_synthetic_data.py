"""
Generate diverse synthetic training data from live XTTS and Fish Speech endpoints.

Produces balanced SK/CS/EN clips across both generators with diverse texts.
Saves raw WAV files + metadata manifest for training pipeline.

Usage:
    python generate_synthetic_data.py \
        --xtts-url https://<xtts-route> \
        --fish-url https://<fish-route> \
        --output-dir generated_data \
        --clips-per-combo 3
"""

import argparse
import base64
import io
import json
import os
import time
import uuid
import warnings

import requests

warnings.filterwarnings("ignore")

REFERENCE_CLIPS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "voice-id", "samples"
)

TEXTS = {
    "cs": [
        "Kontejnerizace aplikací přináší flexibilitu a škálovatelnost.",
        "OpenShift je podniková platforma pro správu kontejnerů.",
        "Automatizace je klíčem k úspěšnému nasazení v cloudu.",
        "Bezpečnost je naší nejvyšší prioritou při vývoji software.",
        "Umělá inteligence transformuje způsob, jakým pracujeme.",
        "Hybridní cloud umožňuje kombinovat veřejné a privátní prostředí.",
        "DevOps praktiky zrychlují dodávku software zákazníkům.",
        "Kubernetes orchestruje kontejnery v produkčním prostředí.",
        "Strojové učení pomáhá analyzovat velké objemy dat.",
        "Red Hat Enterprise Linux je stabilní základ pro podnikové systémy.",
        "Otevřený zdrojový kód je hnacím motorem inovací.",
        "Mikroslužby umožňují nezávislý vývoj a nasazení komponent.",
        "Správa konfigurace je důležitá pro konzistentní prostředí.",
        "Monitorování systémů pomáhá předcházet výpadkům.",
        "Infrastruktura jako kód zajišťuje reprodukovatelnost nasazení.",
    ],
    "sk": [
        "Kontajnerizácia aplikácií prináša flexibilitu a škálovateľnosť.",
        "OpenShift je podniková platforma na správu kontajnerov.",
        "Automatizácia je kľúčom k úspešnému nasadeniu v cloude.",
        "Bezpečnosť je našou najvyššou prioritou pri vývoji softvéru.",
        "Umelá inteligencia transformuje spôsob, akým pracujeme.",
        "Hybridný cloud umožňuje kombinovať verejné a privátne prostredie.",
        "DevOps praktiky zrýchľujú dodávku softvéru zákazníkom.",
        "Kubernetes orchestruje kontajnery v produkčnom prostredí.",
        "Strojové učenie pomáha analyzovať veľké objemy dát.",
        "Red Hat Enterprise Linux je stabilný základ pre podnikové systémy.",
        "Otvorený zdrojový kód je hnacím motorom inovácií.",
        "Mikroslužby umožňujú nezávislý vývoj a nasadenie komponentov.",
        "Správa konfigurácie je dôležitá pre konzistentné prostredie.",
        "Monitorovanie systémov pomáha predchádzať výpadkom.",
        "Infraštruktúra ako kód zabezpečuje reprodukovateľnosť nasadenia.",
    ],
    "en": [
        "Container technology brings flexibility and scalability to modern applications.",
        "OpenShift is an enterprise platform for managing containerized workloads.",
        "Automation is the key to successful cloud deployment strategies.",
        "Security remains our highest priority in software development.",
        "Artificial intelligence is transforming the way we work every day.",
        "Hybrid cloud enables combining public and private environments seamlessly.",
        "DevOps practices accelerate software delivery to end customers.",
        "Kubernetes orchestrates containers in production environments at scale.",
        "Machine learning helps analyze large volumes of data efficiently.",
        "Red Hat Enterprise Linux provides a stable foundation for enterprise systems.",
        "Open source code drives innovation across the technology industry.",
        "Microservices enable independent development and deployment of components.",
        "Configuration management ensures consistent and reliable environments.",
        "System monitoring helps prevent outages and performance degradation.",
        "Infrastructure as code ensures reproducible and auditable deployments.",
    ],
}

# XTTS language codes differ from ISO
XTTS_LANG_MAP = {"cs": "cs", "sk": "cs", "en": "en"}


def find_reference_speakers():
    """Find available reference WAV files for voice cloning."""
    speakers = {}
    natural_dir = os.path.join(REFERENCE_CLIPS_DIR, "natural")

    for lang_dir in ["cs", "en", "sk"]:
        lang_path = os.path.join(natural_dir, lang_dir)
        if not os.path.isdir(lang_path):
            continue
        for f in sorted(os.listdir(lang_path)):
            if not f.endswith((".wav", ".m4a", ".mp3")):
                continue
            speaker = f.split("-")[0]
            if speaker not in speakers:
                speakers[speaker] = {}
            if lang_dir not in speakers[speaker]:
                speakers[speaker][lang_dir] = []
            speakers[speaker][lang_dir].append(os.path.join(lang_path, f))

    return speakers


def convert_to_wav(path):
    """Convert audio file to WAV bytes using ffmpeg."""
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-ar", "16000", "-ac", "1", tmp_path],
            capture_output=True, check=True,
        )
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


class XTTSGenerator:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.voice_cache = {}

    def register_voice(self, wav_bytes, speaker_name):
        cache_key = speaker_name
        if cache_key in self.voice_cache:
            return self.voice_cache[cache_key]
        resp = requests.post(
            f"{self.url}/clone_speaker",
            files={"wav_file": ("reference.wav", io.BytesIO(wav_bytes), "audio/wav")},
            timeout=60, verify=False,
        )
        resp.raise_for_status()
        data = resp.json()
        ref = {
            "speaker_embedding": data["speaker_embedding"],
            "gpt_cond_latent": data["gpt_cond_latent"],
        }
        self.voice_cache[cache_key] = ref
        return ref

    def generate(self, text, voice_ref, language):
        xtts_lang = XTTS_LANG_MAP.get(language, language)
        payload = {
            "text": text,
            "language": xtts_lang,
            "speaker_embedding": voice_ref["speaker_embedding"],
            "gpt_cond_latent": voice_ref["gpt_cond_latent"],
        }
        resp = requests.post(
            f"{self.url}/tts", json=payload, timeout=120, verify=False,
        )
        resp.raise_for_status()
        raw = resp.text.strip().strip('"')
        return base64.b64decode(raw)


class FishGenerator:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.registered = set()

    def register_voice(self, wav_bytes, speaker_name):
        if speaker_name in self.registered:
            return {"reference_id": speaker_name}
        resp = requests.post(
            f"{self.url}/v1/references/add",
            files={"audio": ("reference.wav", io.BytesIO(wav_bytes), "audio/wav")},
            data={"id": speaker_name, "text": "Toto je ukázka mého hlasu pro klonování."},
            timeout=60, verify=False,
        )
        if resp.status_code in (200, 409):
            self.registered.add(speaker_name)
            return {"reference_id": speaker_name}
        raise RuntimeError(f"Fish register failed: {resp.status_code} {resp.text[:200]}")

    def generate(self, text, voice_ref, language):
        payload = {
            "text": text,
            "reference_id": voice_ref["reference_id"],
            "format": "wav",
        }
        resp = requests.post(
            f"{self.url}/v1/tts", json=payload, timeout=300, verify=False,
        )
        resp.raise_for_status()
        return resp.content


def generate_dataset(args):
    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)

    speakers = find_reference_speakers()
    print(f"Found {len(speakers)} reference speakers: {list(speakers.keys())}")
    for sp, langs in speakers.items():
        print(f"  {sp}: {list(langs.keys())}")

    xtts = XTTSGenerator(args.xtts_url)
    fish = FishGenerator(args.fish_url)

    manifest = []
    errors = []
    clip_id = 0

    generators = [("xtts", xtts), ("fish", fish)]
    languages = ["cs", "sk", "en"]

    # Determine how many texts per combo to use
    clips_per_combo = args.clips_per_combo

    for gen_name, gen in generators:
        for lang in languages:
            texts = TEXTS[lang][:clips_per_combo]
            for speaker, speaker_langs in speakers.items():
                # Find a reference clip for this speaker
                # Prefer same language, fall back to any
                ref_path = None
                if lang in speaker_langs:
                    ref_path = speaker_langs[lang][0]
                else:
                    for available_lang in speaker_langs:
                        ref_path = speaker_langs[available_lang][0]
                        break

                if not ref_path:
                    continue

                print(f"\n  Registering {speaker} for {gen_name} ({lang})...")
                try:
                    wav_bytes = convert_to_wav(ref_path)
                    voice_ref = gen.register_voice(wav_bytes, f"{speaker}-{lang}")
                except Exception as e:
                    print(f"    ERROR registering: {e}")
                    errors.append({"speaker": speaker, "gen": gen_name, "error": str(e)})
                    continue

                for text_idx, text in enumerate(texts):
                    clip_id += 1
                    filename = f"{gen_name}-{speaker}-{lang}-{text_idx:02d}.wav"
                    filepath = os.path.join(output_dir, filename)

                    print(f"    [{clip_id}] {filename}...", end=" ", flush=True)
                    t0 = time.time()

                    try:
                        wav_data = gen.generate(text, voice_ref, lang)
                        with open(filepath, "wb") as f:
                            f.write(wav_data)

                        elapsed = time.time() - t0
                        print(f"OK ({elapsed:.1f}s, {len(wav_data)} bytes)")

                        manifest.append({
                            "id": clip_id,
                            "filename": filename,
                            "path": os.path.abspath(filepath),
                            "generator": gen_name,
                            "generator_full": "Coqui XTTS v2" if gen_name == "xtts" else "Fish Audio S2 Pro",
                            "speaker": speaker,
                            "language": lang,
                            "text": text,
                            "text_index": text_idx,
                            "duration_bytes": len(wav_data),
                            "generation_time_s": round(elapsed, 1),
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "label": 1,
                            "label_name": "fake",
                        })

                    except Exception as e:
                        elapsed = time.time() - t0
                        print(f"FAILED ({elapsed:.1f}s): {e}")
                        errors.append({
                            "clip_id": clip_id, "filename": filename,
                            "error": str(e),
                        })

    # Save manifest
    manifest_path = os.path.join(output_dir, "generation_manifest.json")
    with open(manifest_path, "w") as f:
        json.dump({
            "total_clips": len(manifest),
            "errors": len(errors),
            "generators": ["xtts", "fish"],
            "languages": languages,
            "speakers": list(speakers.keys()),
            "clips_per_combo": clips_per_combo,
            "clips": manifest,
            "error_log": errors,
        }, f, indent=2, ensure_ascii=False)

    print(f"\n{'=' * 60}")
    print(f"Generation complete: {len(manifest)} clips, {len(errors)} errors")
    print(f"Manifest: {manifest_path}")

    # Summary by generator x language
    from collections import Counter
    counts = Counter()
    for c in manifest:
        counts[(c["generator"], c["language"])] += 1
    print(f"\n  {'':12s} {'cs':>6s} {'sk':>6s} {'en':>6s}")
    for gen in ["xtts", "fish"]:
        row = f"  {gen:12s}"
        for lang in ["cs", "sk", "en"]:
            row += f" {counts.get((gen, lang), 0):6d}"
        print(row)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--xtts-url", required=True)
    parser.add_argument("--fish-url", required=True)
    parser.add_argument("--output-dir", default="generated_data")
    parser.add_argument("--clips-per-combo", type=int, default=5,
                        help="Number of texts per speaker/generator/language combo")
    args = parser.parse_args()
    generate_dataset(args)

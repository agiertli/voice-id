#!/usr/bin/env python3
"""Import Clone Studio sentence recordings (and optional AI pairs) into the Voice ID game.

Talks only to the two HTTP APIs - never touches either SQLite DB directly.

  Clone Studio:  GET /api/recordings, GET /api/recordings/<id>/audio,
                 GET /api/providers, GET /api/clips?voice_id=, GET /api/clips/<id>/audio,
                 POST /api/generate {voice_id, text, provider_id, language}
  Voice ID game: GET /api/admin/clips, POST /api/admin/upload (multipart)

Dry-run is the default: nothing is uploaded to the game and nothing is generated in
Clone Studio. Pass --apply to do it. --out-dir exports every WAV plus manifest.json
locally (works in dry-run too; doubles as training-data export for the deepfake detector).

Usage:
  python3 tools/import_from_clone_studio.py \\
      --studio https://voice-clone-studio.apps.example.com \\
      --game   https://voice-id.apps.example.com \\
      [--speaker anton --speaker miska] [--language cs] \\
      [--with-ai --providers xtts,fish] [--out-dir ./export] [--apply]

Stdlib only. TLS verification is off by default (demo clusters use self-signed routes);
pass --verify-tls to enable it.
"""
import argparse
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from pathlib import Path

# Must match the source strings already used in the game DB.
PROVIDER_SOURCES = {
    "xtts": "Coqui XTTS v2",
    "fish": "Fish Audio S2 Pro",
}

LIST_TIMEOUT = 30
AUDIO_TIMEOUT = 120
GENERATE_TIMEOUT = 600  # Fish S2 Pro: 30-120 s per request, first call after restart can take ~5 min


class HttpError(Exception):
    pass


class Http:
    def __init__(self, verify_tls=False, token=""):
        self.ctx = None if verify_tls else ssl._create_unverified_context()
        self.token = token

    def request(self, method, url, data=None, headers=None, timeout=LIST_TIMEOUT):
        headers = dict(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf-8", "replace")
            try:
                body = json.loads(body).get("error", body)
            except Exception:
                pass
            raise HttpError(f"{method} {url} -> HTTP {e.code}: {body}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise HttpError(f"{method} {url} -> {reason}") from None

    def get_json(self, url, timeout=LIST_TIMEOUT):
        return json.loads(self.request("GET", url, timeout=timeout))

    def post_json(self, url, payload, timeout=LIST_TIMEOUT):
        body = json.dumps(payload).encode()
        return json.loads(self.request("POST", url, body, {"Content-Type": "application/json"}, timeout))

    def get_bytes(self, url, timeout=AUDIO_TIMEOUT):
        return self.request("GET", url, timeout=timeout)

    def post_multipart(self, url, fields, files, timeout=AUDIO_TIMEOUT):
        boundary = "----vid" + uuid.uuid4().hex
        parts = []
        for k, v in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
                         + str(v).encode() + b"\r\n")
        for k, (fname, data, ctype) in files.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                         f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return json.loads(self.request("POST", url, b"".join(parts), headers, timeout))


def speaker_key(name):
    return (name or "").strip().lower()


def file_slug(key):
    """Speaker key -> safe filename component (keeps unicode letters, e.g. 'miška')."""
    return re.sub(r"[^\w.-]+", "_", key).strip("_") or "unknown"


def human_filename(lang, key, idx):
    return f"natural/{lang}/{file_slug(key)}-s{idx:02d}.wav"


def ai_filename(lang, provider, key, idx):
    return f"ai/{lang}/{provider}-{file_slug(key)}-s{idx:02d}.wav"


def find_existing_clip(studio_clips, provider, text, language):
    """Pick an already generated Clone Studio clip for the same voice+text+provider.
    studio_clips is /api/clips?voice_id=... (newest first). Language must match when set."""
    want = " ".join(text.split())
    for c in studio_clips:
        if c.get("provider_id") != provider:
            continue
        if " ".join((c.get("text") or "").split()) != want:
            continue
        if c.get("language") and language and c["language"] != language:
            continue
        return c
    return None


def build_plan(recordings, existing_filenames, with_ai, providers, speakers=None, language=None):
    """Pure planning step (unit-testable). Returns list of item dicts."""
    want_speakers = {speaker_key(s) for s in speakers} if speakers else None
    items = []
    for r in sorted(recordings, key=lambda r: (speaker_key(r["speaker"]), r["language"], r["sentence_idx"])):
        key = speaker_key(r["speaker"])
        if not key:
            continue
        if want_speakers and key not in want_speakers:
            continue
        if language and r["language"] != language:
            continue
        base = {"speaker": key, "language": r["language"], "sentence_idx": r["sentence_idx"],
                "text": r["text"], "recording_id": r["recording_id"], "voice_id": r.get("voice_id"),
                "duration_s": r.get("duration_s")}
        fn = human_filename(r["language"], key, r["sentence_idx"])
        items.append(dict(base, kind="human", provider="", source="", filename=fn,
                          exists=fn in existing_filenames))
        if not with_ai:
            continue
        for p in providers:
            fn = ai_filename(r["language"], p, key, r["sentence_idx"])
            item = dict(base, kind="ai", provider=p, source=PROVIDER_SOURCES[p], filename=fn,
                        exists=fn in existing_filenames)
            if not r.get("voice_id"):
                item["no_voice"] = True
            items.append(item)
    return items


class Importer:
    def __init__(self, http, studio, game, apply, out_dir):
        self.http = http
        self.studio = studio.rstrip("/")
        self.game = game.rstrip("/")
        self.apply = apply
        self.out_dir = Path(out_dir) if out_dir else None
        self.studio_providers = None
        self._clip_cache = {}
        self.manifest = []

    # --- Clone Studio helpers ---
    def studio_clips(self, voice_id):
        if voice_id not in self._clip_cache:
            q = urllib.parse.urlencode({"voice_id": voice_id})
            self._clip_cache[voice_id] = self.http.get_json(f"{self.studio}/api/clips?{q}")
        return self._clip_cache[voice_id]

    def ensure_ai_clip(self, item):
        """Return (studio_clip_id, how) - reuse existing clip or generate one (apply mode only)."""
        existing = find_existing_clip(self.studio_clips(item["voice_id"]), item["provider"],
                                      item["text"], item["language"])
        if existing:
            return existing["clip_id"], "reused"
        if not self.apply:
            if item["provider"] not in self.studio_providers:
                return None, "would-generate, but provider NOT configured in Clone Studio"
            return None, "would-generate"
        if item["provider"] not in self.studio_providers:
            configured = ", ".join(sorted(self.studio_providers)) or "none"
            raise HttpError(f"provider '{item['provider']}' is not configured in Clone Studio "
                            f"(configured: {configured}; set XTTS_URL / FISH_URL on the studio deployment)")
        t0 = time.time()
        print(f"      generating with {item['provider']} (can take 30-120 s for fish) ...", flush=True)
        res = self.http.post_json(f"{self.studio}/api/generate", {
            "voice_id": item["voice_id"], "text": item["text"],
            "provider_id": item["provider"], "language": item["language"],
        }, timeout=GENERATE_TIMEOUT)
        print(f"      generated clip {res['clip_id']} in {time.time() - t0:.0f}s", flush=True)
        self._clip_cache.setdefault(item["voice_id"], []).insert(0, res)
        return res["clip_id"], "generated"

    # --- main per-item step ---
    def process(self, item):
        """Returns status: exists | planned | uploaded | failed | no-voice."""
        if item["kind"] == "ai" and item.get("no_voice"):
            item["status"] = "no-voice"
            return item["status"]
        need_upload = not item["exists"]
        need_audio = self.out_dir is not None or (need_upload and self.apply)
        wav = None
        try:
            if item["kind"] == "human":
                if need_audio:
                    wav = self.http.get_bytes(f"{self.studio}/api/recordings/{item['recording_id']}/audio")
            else:
                if need_upload or self.out_dir:
                    if item["exists"]:
                        # already in game: only export if a studio clip exists, never generate for it
                        c = find_existing_clip(self.studio_clips(item["voice_id"]), item["provider"],
                                               item["text"], item["language"])
                        clip_id, how = (c["clip_id"], "reused") if c else (None, "not-in-studio")
                    else:
                        clip_id, how = self.ensure_ai_clip(item)
                    item["studio_clip_id"], item["ai_clip"] = clip_id, how
                    if clip_id and need_audio:
                        wav = self.http.get_bytes(f"{self.studio}/api/clips/{clip_id}/audio")

            if wav is not None and self.out_dir:
                self.export(item, wav)

            if not need_upload:
                item["status"] = "exists"
            elif not self.apply:
                item["status"] = "planned"
            else:
                res = self.http.post_multipart(f"{self.game}/api/admin/upload", {
                    "is_ai": "1" if item["kind"] == "ai" else "0",
                    "language": item["language"],
                    "source": item["source"],
                    "speaker": item["speaker"],
                    "filename": item["filename"],
                }, {"audio": (Path(item["filename"]).name, wav, "audio/wav")})
                item["game_clip_id"] = res.get("id")
                item["status"] = "uploaded"
        except (HttpError, KeyError, ValueError) as e:
            item["status"] = "failed"
            item["error"] = str(e)
        return item["status"]

    def export(self, item, wav):
        path = self.out_dir / item["filename"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(wav)
        self.manifest.append({
            "file": item["filename"],
            "label": "ai" if item["kind"] == "ai" else "human",
            "is_ai": item["kind"] == "ai",
            "language": item["language"],
            "speaker": item["speaker"],
            "provider": item["provider"],
            "source": item["source"],
            "text": item["text"],
            "sentence_idx": item["sentence_idx"],
            "recording_id": item["recording_id"],
            "voice_id": item.get("voice_id"),
            "studio_clip_id": item.get("studio_clip_id"),
            "duration_s": item["duration_s"] if item["kind"] == "human" else None,
            "bytes": len(wav),
        })

    def write_manifest(self):
        if not self.out_dir:
            return None
        path = self.out_dir / "manifest.json"
        merged = {}
        if path.exists():
            try:
                for e in json.loads(path.read_text()).get("clips", []):
                    merged[e["file"]] = e
            except Exception:
                print(f"WARNING: could not parse existing {path}, rewriting it", file=sys.stderr)
        for e in self.manifest:
            merged[e["file"]] = e
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "studio": self.studio,
            "clips": sorted(merged.values(), key=lambda e: e["file"]),
        }, indent=2, ensure_ascii=False))
        return path, len(merged)


def print_summary(items, apply):
    rows = defaultdict(lambda: defaultdict(int))
    for it in items:
        k = (it["speaker"], it["language"], "AI " + it["provider"] if it["kind"] == "ai" else "human")
        rows[k]["total"] += 1
        rows[k][it["status"]] += 1
    cols = ["total", "planned", "uploaded", "exists", "failed", "no-voice"]
    head = f"{'speaker':<16}{'lang':<6}{'kind':<10}" + "".join(f"{c:>10}" for c in cols)
    print("\n" + "=" * len(head))
    print(f"SUMMARY ({'APPLY' if apply else 'DRY-RUN - nothing uploaded or generated'})")
    print("planned = would be uploaded with --apply; exists = already in game (skipped);")
    print("no-voice = recording has no linked cloned voice, AI pair impossible")
    print("-" * len(head))
    print(head)
    totals = defaultdict(int)
    for k in sorted(rows):
        r = rows[k]
        print(f"{k[0]:<16}{k[1]:<6}{k[2]:<10}" + "".join(f"{r[c]:>10}" for c in cols))
        for c in cols:
            totals[c] += r[c]
    print("-" * len(head))
    print(f"{'TOTAL':<32}" + "".join(f"{totals[c]:>10}" for c in cols))
    fails = [it for it in items if it["status"] == "failed"]
    if fails:
        print("\nFailures:")
        for it in fails:
            print(f"  {it['filename']}: {it['error']}")
    return totals


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--studio", required=True, help="Clone Studio base URL")
    ap.add_argument("--game", required=True, help="Voice ID game base URL")
    ap.add_argument("--speaker", action="append", help="only this speaker (repeatable, case-insensitive)")
    ap.add_argument("--language", choices=["cs", "sk", "en"], help="only this language")
    ap.add_argument("--with-ai", action="store_true", help="also create AI pairs (same sentence, cloned voice)")
    ap.add_argument("--providers", default="xtts,fish", help="AI providers for --with-ai (default: xtts,fish)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false", default=False,
                      help="(default) only show what would happen")
    mode.add_argument("--apply", dest="apply", action="store_true", help="really generate and upload")
    ap.add_argument("--out-dir", help="also save every WAV + manifest.json here (training-data export)")
    ap.add_argument("--verify-tls", action="store_true", help="verify TLS certificates (off by default)")
    ap.add_argument("--game-token", default="", help="optional Bearer token for the game admin API")
    args = ap.parse_args(argv)

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    bad = [p for p in providers if p not in PROVIDER_SOURCES]
    if bad:
        ap.error(f"unknown provider(s) {bad}; choose from {sorted(PROVIDER_SOURCES)}")

    http = Http(verify_tls=args.verify_tls, token=args.game_token)
    imp = Importer(http, args.studio, args.game, args.apply, args.out_dir)

    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN (use --apply to upload)'}", flush=True)
    try:
        recordings = http.get_json(f"{imp.studio}/api/recordings")
        game_clips = http.get_json(f"{imp.game}/api/admin/clips")
        if args.with_ai:
            imp.studio_providers = {p["id"] for p in http.get_json(f"{imp.studio}/api/providers")}
    except (HttpError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    existing = {c["filename"] for c in game_clips}
    print(f"Clone Studio: {len(recordings)} recordings | Game: {len(game_clips)} clips")

    if args.with_ai:
        missing = [p for p in providers if p not in imp.studio_providers]
        if missing:
            print(f"WARNING: Clone Studio has no provider configured for {missing} "
                  f"(configured: {sorted(imp.studio_providers) or 'none'}). Only already generated "
                  f"clips can be reused for those; new generation will fail.")

    items = build_plan(recordings, existing, args.with_ai, providers, args.speaker, args.language)
    if not items:
        print("Nothing matches the filters.")
        return 0

    for n, it in enumerate(items, 1):
        label = it["kind"] if it["kind"] == "human" else f"ai/{it['provider']}"
        print(f"[{n}/{len(items)}] {label:<9} {it['filename']}", flush=True)
        status = imp.process(it)
        extra = f" ({it['ai_clip']})" if it.get("ai_clip") else ""
        if status == "failed":
            extra += f": {it['error']}"
        print(f"      -> {status}{extra}", flush=True)

    totals = print_summary(items, args.apply)
    res = imp.write_manifest()
    if res:
        print(f"\nExported to {res[0]} ({res[1]} clips in manifest)")
    if not args.apply and totals["planned"]:
        print("\nDry-run only. Re-run with --apply to upload.")
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())

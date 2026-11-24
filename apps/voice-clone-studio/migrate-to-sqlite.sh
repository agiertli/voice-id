#!/bin/bash
#
# Migration script: filesystem voices/clips -> SQLite via API
#
# Run ON the pod:
#   oc exec deploy/voice-clone-studio -n voice-clone-studio -- bash /opt/app-root/src/migrate-to-sqlite.sh
#
# Or from your machine (copies the script first):
#   oc cp apps/voice-clone-studio/migrate-to-sqlite.sh voice-clone-studio/<pod>:/tmp/migrate.sh -n voice-clone-studio
#   oc exec <pod> -n voice-clone-studio -- bash /tmp/migrate.sh
#
# The script reads old flat-file data from DATA_DIR and POSTs it to the local API.
# It does NOT delete old files — do that manually after verifying.

set -euo pipefail

API="http://localhost:8080"
DATA_DIR="${DATA_DIR:-/tmp/voice-data}"
VOICES_DIR="${DATA_DIR}/voices"
CLIPS_DIR="${DATA_DIR}/clips"

echo "=== Voice Clone Studio: filesystem -> SQLite migration ==="
echo "DATA_DIR: ${DATA_DIR}"
echo "API: ${API}"
echo ""

# Check API is up
if ! curl -sf "${API}/api/health" > /dev/null 2>&1; then
    echo "ERROR: API not responding at ${API}/api/health"
    exit 1
fi

# Check if there's anything to migrate
if [ ! -d "${VOICES_DIR}" ]; then
    echo "No voices directory found at ${VOICES_DIR} — nothing to migrate."
    exit 0
fi

VOICE_COUNT=0
CLIP_COUNT=0
VOICE_SKIP=0
CLIP_SKIP=0

# Migrate voices
for meta_file in "${VOICES_DIR}"/*/meta.json; do
    [ -f "${meta_file}" ] || continue

    voice_dir=$(dirname "${meta_file}")
    voice_id=$(basename "${voice_dir}")
    ref_wav="${voice_dir}/reference.wav"

    if [ ! -f "${ref_wav}" ]; then
        echo "SKIP voice ${voice_id}: no reference.wav"
        VOICE_SKIP=$((VOICE_SKIP + 1))
        continue
    fi

    name=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['name'])" "${meta_file}" 2>/dev/null || echo "unknown")

    # Check if voice already exists in SQLite
    existing=$(curl -sf "${API}/api/voices" | python3 -c "
import json, sys
voices = json.load(sys.stdin)
for v in voices:
    if v['voice_id'] == '${voice_id}':
        print('exists')
        break
" 2>/dev/null || true)

    if [ "${existing}" = "exists" ]; then
        echo "SKIP voice ${voice_id} (${name}): already in database"
        VOICE_SKIP=$((VOICE_SKIP + 1))
    else
        echo "Migrating voice ${voice_id} (${name})..."
        resp=$(curl -sf "${API}/api/voices" \
            -F "audio=@${ref_wav}" \
            -F "name=${name}" 2>&1) || {
            echo "  FAILED: ${resp}"
            continue
        }
        new_voice_id=$(echo "${resp}" | python3 -c "import json,sys; print(json.load(sys.stdin)['voice_id'])")
        echo "  OK -> new voice_id: ${new_voice_id} (was: ${voice_id})"
        VOICE_COUNT=$((VOICE_COUNT + 1))

        # Map old voice_id to new for clip migration
        eval "VOICE_MAP_${voice_id}=${new_voice_id}"
    fi
done

# Migrate clips
for clip_json in "${CLIPS_DIR}"/*.json; do
    [ -f "${clip_json}" ] || continue

    clip_id=$(basename "${clip_json}" .json)
    clip_wav="${CLIPS_DIR}/${clip_id}.wav"

    if [ ! -f "${clip_wav}" ]; then
        echo "SKIP clip ${clip_id}: no WAV file"
        CLIP_SKIP=$((CLIP_SKIP + 1))
        continue
    fi

    # Parse clip metadata
    eval "$(python3 -c "
import json, sys
c = json.load(open(sys.argv[1]))
print(f'clip_voice_id={c[\"voice_id\"]}')
print(f'clip_provider_id={c.get(\"provider_id\", \"unknown\")}')
print(f'clip_provider_name=\"{c.get(\"provider_name\", \"Unknown\")}\"')
print(f'clip_text=\"{c.get(\"text\", \"\")}\"')
print(f'clip_language={c.get(\"language\", \"cs\")}')
print(f'clip_created_at={c.get(\"created_at\", 0)}')
" "${clip_json}" 2>/dev/null)" || {
        echo "SKIP clip ${clip_id}: failed to parse metadata"
        CLIP_SKIP=$((CLIP_SKIP + 1))
        continue
    }

    # Resolve voice_id (use mapped ID if voice was migrated, otherwise use original)
    resolved_voice_id=$(eval "echo \${VOICE_MAP_${clip_voice_id}:-${clip_voice_id}}" 2>/dev/null || echo "${clip_voice_id}")

    echo "Importing clip ${clip_id} (voice: ${resolved_voice_id}, provider: ${clip_provider_id})..."
    resp=$(curl -sf "${API}/api/clips/import" \
        -F "audio=@${clip_wav}" \
        -F "voice_id=${resolved_voice_id}" \
        -F "provider_id=${clip_provider_id}" \
        -F "provider_name=${clip_provider_name}" \
        -F "text=${clip_text}" \
        -F "language=${clip_language}" \
        -F "created_at=${clip_created_at}" 2>&1) || {
        echo "  FAILED: ${resp}"
        CLIP_SKIP=$((CLIP_SKIP + 1))
        continue
    }
    new_clip_id=$(echo "${resp}" | python3 -c "import json,sys; print(json.load(sys.stdin)['clip_id'])")
    echo "  OK -> new clip_id: ${new_clip_id}"
    CLIP_COUNT=$((CLIP_COUNT + 1))
done

echo ""
echo "=== Migration complete ==="
echo "Voices migrated: ${VOICE_COUNT} (skipped: ${VOICE_SKIP})"
echo "Clips imported:  ${CLIP_COUNT} (skipped: ${CLIP_SKIP})"
echo ""
echo "Verify the data, then manually remove old files:"
echo "  rm -rf ${VOICES_DIR} ${CLIPS_DIR}"

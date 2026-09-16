#!/usr/bin/env bash
set -euo pipefail

# Voice ID: AI or Alive? — deployment script
# Deploys the game to any OpenShift cluster. No GPU required.
#
# Usage:
#   ./deploy.sh                                    # defaults
#   CONFERENCE="DevConf 2025" LANG=cs ./deploy.sh  # customize
#   VERSION=v1.11.1 ./deploy.sh                    # pin image tag
#   BUILD=1 ./deploy.sh                            # build + push + deploy

NAMESPACE="${NAMESPACE:-voice-id}"
CONFERENCE="${CONFERENCE:-Red Hat Summit}"
LANG="${LANG:-en}"
VERSION="${VERSION:-v1.11.1}"
IMAGE_REGISTRY="${IMAGE_REGISTRY:-quay.io/agiertli}"
IMAGE_NAME="${IMAGE_REGISTRY}/voice-id"
BUILD="${BUILD:-0}"

echo "=== Voice ID: AI or Alive? ==="
echo "  Namespace:  ${NAMESPACE}"
echo "  Conference: ${CONFERENCE}"
echo "  Language:   ${LANG}"
echo "  Image:      ${IMAGE_NAME}:${VERSION}"
echo ""

# --- Prerequisites ---

if ! command -v oc &>/dev/null; then
    echo "ERROR: 'oc' CLI not found. Install it from https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/"
    exit 1
fi

if ! command -v helm &>/dev/null; then
    echo "ERROR: 'helm' CLI not found. Install it from https://helm.sh/docs/intro/install/"
    exit 1
fi

if ! oc whoami &>/dev/null; then
    echo "ERROR: Not logged into an OpenShift cluster. Run 'oc login' first."
    exit 1
fi

echo "Cluster: $(oc whoami --show-server)"
echo "User:    $(oc whoami)"
echo ""

# --- Build (optional) ---

if [ "${BUILD}" = "1" ]; then
    echo "Building ${IMAGE_NAME}:${VERSION} ..."
    podman build --platform linux/amd64 -t "${IMAGE_NAME}:${VERSION}" .
    echo "Pushing ..."
    podman push "${IMAGE_NAME}:${VERSION}"
    echo ""
fi

# --- Deploy ---

echo "Deploying to namespace '${NAMESPACE}' ..."

helm upgrade --install voice-id helm/voice-id \
    --namespace "${NAMESPACE}" --create-namespace \
    --set image.repository="${IMAGE_REGISTRY}/voice-id" \
    --set image.tag="${VERSION}" \
    --set conference.name="${CONFERENCE}" \
    --set game.defaultLang="${LANG}"

echo ""
echo "Waiting for rollout ..."
oc rollout status deployment/voice-id -n "${NAMESPACE}" --timeout=120s

ROUTE=$(oc get route voice-id -n "${NAMESPACE}" -o jsonpath='{.spec.host}' 2>/dev/null || true)
echo ""
echo "=== Deployed ==="
if [ -n "${ROUTE}" ]; then
    echo "  Game:        https://${ROUTE}"
    echo "  TV:          https://${ROUTE}/tv"
    echo "  Admin clips: https://${ROUTE}/api/admin/clips"
fi
echo ""
echo "Next: upload audio clips via the API (see README.md for format)."

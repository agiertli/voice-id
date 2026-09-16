#!/usr/bin/env bash
set -euo pipefail

# Voice ID: AI or Alive? — deployment script
# All configuration lives in helm/voice-id/values.yaml.
# Edit that file, then run this script.
#
# Usage:
#   ./deploy.sh                   # deploy using values.yaml
#   ./deploy.sh --build           # build + push image, then deploy
#   ./deploy.sh -n my-namespace   # override namespace (default: voice-id)

NAMESPACE="voice-id"
BUILD=0
VALUES_FILE="helm/voice-id/values.yaml"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --build)  BUILD=1; shift ;;
        -n|--namespace) NAMESPACE="$2"; shift 2 ;;
        -f|--values) VALUES_FILE="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: ./deploy.sh [--build] [-n NAMESPACE] [-f VALUES_FILE]"
            echo ""
            echo "All game configuration lives in helm/voice-id/values.yaml."
            echo ""
            echo "Options:"
            echo "  --build          Build and push the container image before deploying"
            echo "  -n, --namespace  Target namespace (default: voice-id)"
            echo "  -f, --values     Path to values file (default: helm/voice-id/values.yaml)"
            exit 0 ;;
        *) echo "Unknown option: $1. Use --help for usage."; exit 1 ;;
    esac
done

# --- Prerequisites ---

for cmd in oc helm; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: '$cmd' not found."
        exit 1
    fi
done

if ! oc whoami &>/dev/null; then
    echo "ERROR: Not logged into an OpenShift cluster. Run 'oc login' first."
    exit 1
fi

echo "=== Voice ID: AI or Alive? ==="
echo "  Cluster:   $(oc whoami --show-server)"
echo "  User:      $(oc whoami)"
echo "  Namespace: ${NAMESPACE}"
echo "  Values:    ${VALUES_FILE}"
echo ""

# --- Build (optional) ---

if [ "${BUILD}" = "1" ]; then
    IMAGE=$(grep 'repository:' "${VALUES_FILE}" | awk '{print $2}')
    TAG=$(grep 'tag:' "${VALUES_FILE}" | head -1 | awk '{print $2}' | tr -d '"')
    echo "Building ${IMAGE}:${TAG} ..."
    podman build --platform linux/amd64 -t "${IMAGE}:${TAG}" .
    echo "Pushing ..."
    podman push "${IMAGE}:${TAG}"
    echo ""
fi

# --- Deploy ---

echo "Deploying ..."

helm upgrade --install voice-id helm/voice-id \
    --namespace "${NAMESPACE}" --create-namespace \
    -f "${VALUES_FILE}"

echo ""
echo "Waiting for rollout ..."
oc rollout status deployment/voice-id -n "${NAMESPACE}" --timeout=120s

ROUTE=$(oc get route voice-id -n "${NAMESPACE}" -o jsonpath='{.spec.host}' 2>/dev/null || true)
echo ""
echo "=== Deployed ==="
if [ -n "${ROUTE}" ]; then
    echo "  Game: https://${ROUTE}"
    echo "  TV:   https://${ROUTE}/tv"
fi
echo ""
echo "Next: upload audio clips via the API (see README.md)."

#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DOCKERFILE="${ROOT_DIR}/Dockerfile.ghcr"

HARBOR_REGISTRY="${HARBOR_REGISTRY:-127.0.0.1:81}"
HARBOR_URL="${HARBOR_URL:-http://127.0.0.1:81}"
HARBOR_USERNAME="${HARBOR_USERNAME:-admin}"
HARBOR_PASSWORD="${HARBOR_PASSWORD:-}"
HARBOR_PROJECT="${HARBOR_PROJECT:-ai}"
HARBOR_BASE_REPO="${HARBOR_BASE_REPO:-hermes-base}"
HARBOR_BASE_TAG="${HARBOR_BASE_TAG:-base-20260425-v1}"

BASE_IMAGE="${HARBOR_REGISTRY}/${HARBOR_PROJECT}/${HARBOR_BASE_REPO}:${HARBOR_BASE_TAG}"

if [[ ! -f "${SOURCE_DOCKERFILE}" ]]; then
  echo "Dockerfile not found: ${SOURCE_DOCKERFILE}" >&2
  exit 1
fi

if [[ -z "${HARBOR_PASSWORD}" ]]; then
  read -r -s -p "Harbor password for ${HARBOR_USERNAME}: " HARBOR_PASSWORD
  echo
fi

TMP_DOCKERFILE="$(mktemp "${TMPDIR:-/tmp}/Dockerfile.base.XXXXXX")"
trap 'rm -f "${TMP_DOCKERFILE}"' EXIT

awk '
  /^COPY \. \/opt\/hermes$/ { exit }
  { print }
' "${SOURCE_DOCKERFILE}" > "${TMP_DOCKERFILE}"

if [[ ! -s "${TMP_DOCKERFILE}" ]]; then
  echo "Failed to generate base Dockerfile from ${SOURCE_DOCKERFILE}" >&2
  exit 1
fi

echo "${HARBOR_PASSWORD}" | docker login "${HARBOR_URL}" \
  --username "${HARBOR_USERNAME}" \
  --password-stdin

BUILD_ARGS=(
  --build-arg "APT_MIRROR=${APT_MIRROR:-}"
  --build-arg "APT_UPDATES_MIRROR=${APT_UPDATES_MIRROR:-}"
  --build-arg "APT_SECURITY_MIRROR=${APT_SECURITY_MIRROR:-}"
  --build-arg "NPM_REGISTRY=${NPM_REGISTRY:-https://registry.npmjs.org}"
  --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL:-}"
  --build-arg "UV_INDEX_URL=${UV_INDEX_URL:-}"
)

echo "Building base image: ${BASE_IMAGE}"
docker build \
  -f "${TMP_DOCKERFILE}" \
  -t "${BASE_IMAGE}" \
  "${BUILD_ARGS[@]}" \
  "${ROOT_DIR}"

echo "Pushing base image: ${BASE_IMAGE}"
docker push "${BASE_IMAGE}"

echo "Base image ready: ${BASE_IMAGE}"

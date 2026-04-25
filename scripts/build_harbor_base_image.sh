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
PLAYWRIGHT_ONLY_SHELL="${PLAYWRIGHT_ONLY_SHELL:-1}"
PLAYWRIGHT_BROWSERS_PATH_ARG="${PLAYWRIGHT_BROWSERS_PATH_ARG:-/opt/hermes/.playwright}"
PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST:-}"

BASE_IMAGE="${HARBOR_REGISTRY}/${HARBOR_PROJECT}/${HARBOR_BASE_REPO}:${HARBOR_BASE_TAG}"
LOCAL_RAW_BASE_IMAGE="local/hermes-base-raw:${HARBOR_BASE_TAG}"

if [[ ! -f "${SOURCE_DOCKERFILE}" ]]; then
  echo "Dockerfile not found: ${SOURCE_DOCKERFILE}" >&2
  exit 1
fi

if [[ -z "${HARBOR_PASSWORD}" ]]; then
  read -r -s -p "Harbor password for ${HARBOR_USERNAME}: " HARBOR_PASSWORD
  echo
fi

TMP_RAW_DOCKERFILE="$(mktemp "${TMPDIR:-/tmp}/Dockerfile.base.raw.XXXXXX")"
TMP_PLAYWRIGHT_DOCKERFILE="$(mktemp "${TMPDIR:-/tmp}/Dockerfile.base.playwright.XXXXXX")"
trap 'rm -f "${TMP_RAW_DOCKERFILE}" "${TMP_PLAYWRIGHT_DOCKERFILE}"' EXIT

awk '
  /^COPY \. \/opt\/hermes$/ { exit }
  { print }
' "${SOURCE_DOCKERFILE}" > "${TMP_RAW_DOCKERFILE}"

if [[ ! -s "${TMP_RAW_DOCKERFILE}" ]]; then
  echo "Failed to generate base Dockerfile from ${SOURCE_DOCKERFILE}" >&2
  exit 1
fi

cat > "${TMP_PLAYWRIGHT_DOCKERFILE}" <<EOF
ARG BASE_IMAGE=${LOCAL_RAW_BASE_IMAGE}
FROM \${BASE_IMAGE}

ARG PLAYWRIGHT_BROWSERS_PATH_ARG=${PLAYWRIGHT_BROWSERS_PATH_ARG}
ARG PLAYWRIGHT_ONLY_SHELL=${PLAYWRIGHT_ONLY_SHELL}
ARG PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST}

ENV PLAYWRIGHT_BROWSERS_PATH=\${PLAYWRIGHT_BROWSERS_PATH_ARG}

WORKDIR /opt/hermes

RUN --mount=type=cache,target=/root/.cache/ms-playwright \\
    if [ -n "\$PLAYWRIGHT_DOWNLOAD_HOST" ]; then export PLAYWRIGHT_DOWNLOAD_HOST="\$PLAYWRIGHT_DOWNLOAD_HOST"; fi; \\
    export PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright; \\
    if [ "\$PLAYWRIGHT_ONLY_SHELL" = "1" ]; then \\
        npx playwright install --with-deps chromium --only-shell; \\
    else \\
        npx playwright install --with-deps chromium; \\
    fi; \\
    mkdir -p /opt/hermes/.playwright && \\
    cp -a /root/.cache/ms-playwright/. /opt/hermes/.playwright/
EOF

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

echo "Building raw base image: ${LOCAL_RAW_BASE_IMAGE}"
docker build \
  -f "${TMP_RAW_DOCKERFILE}" \
  -t "${LOCAL_RAW_BASE_IMAGE}" \
  "${BUILD_ARGS[@]}" \
  "${ROOT_DIR}"

echo "Adding Playwright deps/browser to base image: ${BASE_IMAGE}"
docker build \
  -f "${TMP_PLAYWRIGHT_DOCKERFILE}" \
  -t "${BASE_IMAGE}" \
  --build-arg "BASE_IMAGE=${LOCAL_RAW_BASE_IMAGE}" \
  --build-arg "PLAYWRIGHT_BROWSERS_PATH_ARG=${PLAYWRIGHT_BROWSERS_PATH_ARG}" \
  --build-arg "PLAYWRIGHT_ONLY_SHELL=${PLAYWRIGHT_ONLY_SHELL}" \
  --build-arg "PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST}" \
  "${ROOT_DIR}"

echo "Pushing base image: ${BASE_IMAGE}"
docker push "${BASE_IMAGE}"

docker image rm -f "${LOCAL_RAW_BASE_IMAGE}" >/dev/null 2>&1 || true

echo "Base image ready: ${BASE_IMAGE}"

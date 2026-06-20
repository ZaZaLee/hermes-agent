#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DOCKERFILE="${ROOT_DIR}/Dockerfile.ghcr"

HARBOR_REGISTRY="${HARBOR_REGISTRY:-sig-harbor.vancygame.com}"
HARBOR_URL="${HARBOR_URL:-https://sig-harbor.vancygame.com}"
HARBOR_USERNAME="${HARBOR_USERNAME:-admin}"
HARBOR_PASSWORD="${HARBOR_PASSWORD:-tG8dS1mP6yA0tB9x}"
HARBOR_PROJECT="${HARBOR_PROJECT:-ai}"
HARBOR_BASE_REPO="${HARBOR_BASE_REPO:-hermes-base}"
HARBOR_RAW_BASE_REPO="${HARBOR_RAW_BASE_REPO:-${HARBOR_BASE_REPO}-raw}"
HARBOR_BASE_TAG="${HARBOR_BASE_TAG:-base-20260425-v1}"
PLAYWRIGHT_ONLY_SHELL="${PLAYWRIGHT_ONLY_SHELL:-0}"
PLAYWRIGHT_BROWSERS_PATH_ARG="${PLAYWRIGHT_BROWSERS_PATH_ARG:-/opt/hermes/.playwright}"
PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST:-}"
CONTAINERD_SOCK="${CONTAINERD_SOCK:-unix:///run/k3s/containerd/containerd.sock}"
CONTAINERD_NAMESPACE="${CONTAINERD_NAMESPACE:-ai}"

BASE_IMAGE="${HARBOR_REGISTRY}/${HARBOR_PROJECT}/${HARBOR_BASE_REPO}:${HARBOR_BASE_TAG}"
RAW_BASE_IMAGE="${HARBOR_REGISTRY}/${HARBOR_PROJECT}/${HARBOR_RAW_BASE_REPO}:${HARBOR_BASE_TAG}"

detect_container_tool() {
  if command -v nerdctl >/dev/null 2>&1; then
    CONTAINER_CMD=(nerdctl --address "${CONTAINERD_SOCK}" --namespace "${CONTAINERD_NAMESPACE}" --insecure-registry)
    CONTAINER_TYPE="nerdctl"
    echo "Using container tool: nerdctl (${CONTAINERD_NAMESPACE}, ${CONTAINERD_SOCK})"
    return
  fi

  if command -v docker >/dev/null 2>&1; then
    CONTAINER_CMD=(docker)
    CONTAINER_TYPE="docker"
    echo "Using container tool: docker"
    return
  fi

  echo "Neither nerdctl nor docker is available" >&2
  exit 1
}

if [[ ! -f "${SOURCE_DOCKERFILE}" ]]; then
  echo "Dockerfile not found: ${SOURCE_DOCKERFILE}" >&2
  exit 1
fi

detect_container_tool

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
ARG BASE_IMAGE=${RAW_BASE_IMAGE}
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
    cp -a /root/.cache/ms-playwright/. /opt/hermes/.playwright/ && \\
    if [ "\$PLAYWRIGHT_ONLY_SHELL" != "1" ]; then \\
        chrome_path="\$(find /opt/hermes/.playwright -path '*/chrome-linux/chrome' -type f | head -n 1)"; \\
        if [ -n "\$chrome_path" ]; then \\
            ln -sf "\$chrome_path" /usr/local/bin/google-chrome; \\
            ln -sf "\$chrome_path" /usr/local/bin/chromium; \\
        fi; \\
    fi
EOF

echo "${HARBOR_PASSWORD}" | "${CONTAINER_CMD[@]}" login "${HARBOR_URL}" \
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

echo "Building raw base image: ${RAW_BASE_IMAGE}"
"${CONTAINER_CMD[@]}" build \
  -f "${TMP_RAW_DOCKERFILE}" \
  -t "${RAW_BASE_IMAGE}" \
  "${BUILD_ARGS[@]}" \
  "${ROOT_DIR}"

echo "Pushing raw base image: ${RAW_BASE_IMAGE}"
"${CONTAINER_CMD[@]}" push "${RAW_BASE_IMAGE}"

if [[ "${PLAYWRIGHT_ONLY_SHELL}" == "1" ]]; then
  echo "Adding Playwright Chromium headless shell to base image: ${BASE_IMAGE}"
else
  echo "Adding full Playwright Chromium to base image: ${BASE_IMAGE}"
fi
"${CONTAINER_CMD[@]}" build \
  -f "${TMP_PLAYWRIGHT_DOCKERFILE}" \
  -t "${BASE_IMAGE}" \
  --build-arg "BASE_IMAGE=${RAW_BASE_IMAGE}" \
  --build-arg "PLAYWRIGHT_BROWSERS_PATH_ARG=${PLAYWRIGHT_BROWSERS_PATH_ARG}" \
  --build-arg "PLAYWRIGHT_ONLY_SHELL=${PLAYWRIGHT_ONLY_SHELL}" \
  --build-arg "PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST}" \
  "${ROOT_DIR}"

echo "Pushing base image: ${BASE_IMAGE}"
"${CONTAINER_CMD[@]}" push "${BASE_IMAGE}"

"${CONTAINER_CMD[@]}" image rm -f "${RAW_BASE_IMAGE}" >/dev/null 2>&1 || true

cat <<EOF
Base image ready:
  ${BASE_IMAGE}

Raw base image kept in Harbor for reproducible follow-up builds:
  ${RAW_BASE_IMAGE}

Use it for app builds with:
  APP_FROM_HARBOR_BASE=1 HARBOR_BASE_IMAGE=${BASE_IMAGE} scripts/update-build-image-hermes-agent.sh
EOF

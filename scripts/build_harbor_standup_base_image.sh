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
CONTAINER_TOOL="${CONTAINER_TOOL:-auto}"

BASE_IMAGE="${HARBOR_REGISTRY}/${HARBOR_PROJECT}/${HARBOR_BASE_REPO}:${HARBOR_BASE_TAG}"
RAW_BASE_IMAGE="${HARBOR_REGISTRY}/${HARBOR_PROJECT}/${HARBOR_RAW_BASE_REPO}:${HARBOR_BASE_TAG}"

container_tool_available() {
  command -v "$1" >/dev/null 2>&1
}

use_nerdctl() {
  CONTAINER_CMD=(nerdctl --address "${CONTAINERD_SOCK}" --namespace "${CONTAINERD_NAMESPACE}" --insecure-registry)
  CONTAINER_TYPE="nerdctl"
}

use_docker() {
  CONTAINER_CMD=(docker)
  CONTAINER_TYPE="docker"
}

verify_container_tool() {
  "${CONTAINER_CMD[@]}" version >/dev/null 2>&1
}

require_registry_image() {
  local image="$1"
  local image_path="${image%%@*}"
  local last_component="${image_path##*/}"

  if [[ "${last_component}" == *:* ]]; then
    image_path="${image_path%:*}"
  fi

  local registry="${image_path%%/*}"

  if [[ "${image_path}" != */* || ("${registry}" != *.* && "${registry}" != *:* && "${registry}" != "localhost") ]]; then
    echo "Image must include a real registry host, got: ${image}" >&2
    echo "Set HARBOR_REGISTRY=sig-harbor.vancygame.com or another registry host." >&2
    exit 1
  fi
}

detect_container_tool() {
  case "${CONTAINER_TOOL}" in
    nerdctl)
      container_tool_available nerdctl || {
        echo "CONTAINER_TOOL=nerdctl but nerdctl is not available" >&2
        exit 1
      }
      use_nerdctl
      verify_container_tool || {
        echo "nerdctl is installed but cannot connect to ${CONTAINERD_SOCK} in namespace ${CONTAINERD_NAMESPACE}" >&2
        exit 1
      }
      echo "Using container tool: nerdctl (${CONTAINERD_NAMESPACE}, ${CONTAINERD_SOCK})"
      return
      ;;
    docker)
      container_tool_available docker || {
        echo "CONTAINER_TOOL=docker but docker is not available" >&2
        exit 1
      }
      use_docker
      verify_container_tool || {
        echo "docker is installed but not available" >&2
        exit 1
      }
      echo "Using container tool: docker"
      return
      ;;
    auto) ;;
    *)
      echo "Unsupported CONTAINER_TOOL=${CONTAINER_TOOL}; use auto, nerdctl, or docker" >&2
      exit 1
      ;;
  esac

  if container_tool_available nerdctl; then
    use_nerdctl
    if verify_container_tool; then
      echo "Using container tool: nerdctl (${CONTAINERD_NAMESPACE}, ${CONTAINERD_SOCK})"
      return
    fi
    echo "Skipping nerdctl: cannot connect to ${CONTAINERD_SOCK} in namespace ${CONTAINERD_NAMESPACE}" >&2
  fi

  if container_tool_available docker; then
    use_docker
    if verify_container_tool; then
      echo "Using container tool: docker"
      return
    fi
    echo "Skipping docker: docker is installed but not available" >&2
  fi

  echo "Neither nerdctl nor docker is available or usable" >&2
  exit 1
}

if [[ ! -f "${SOURCE_DOCKERFILE}" ]]; then
  echo "Dockerfile not found: ${SOURCE_DOCKERFILE}" >&2
  exit 1
fi

detect_container_tool
require_registry_image "${RAW_BASE_IMAGE}"
require_registry_image "${BASE_IMAGE}"

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

echo "Pulling raw base image for Playwright layer: ${RAW_BASE_IMAGE}"
"${CONTAINER_CMD[@]}" pull "${RAW_BASE_IMAGE}"

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
  BASE_IMAGE=${BASE_IMAGE} scripts/build_harbor_standup_hermes_image.sh
EOF

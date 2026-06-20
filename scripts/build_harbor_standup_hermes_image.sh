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
HARBOR_BASE_TAG="${HARBOR_BASE_TAG:-base-20260425-v1}"
HARBOR_APP_REPO="${HARBOR_APP_REPO:-hermes-agent}"
HARBOR_APP_TAG="${HARBOR_APP_TAG:-ai-sig}"
LOCAL_APP_IMAGE="${LOCAL_APP_IMAGE:-hermes-agent:local}"
PUSH_IMAGE="${PUSH_IMAGE:-1}"
UPDATE_SOURCE="${UPDATE_SOURCE:-1}"
FORCE_SYNC="${FORCE_SYNC:-0}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
CONTAINERD_SOCK="${CONTAINERD_SOCK:-unix:///run/k3s/containerd/containerd.sock}"
CONTAINERD_NAMESPACE="${CONTAINERD_NAMESPACE:-ai}"
CONTAINER_TOOL="${CONTAINER_TOOL:-auto}"
HARBOR_LOGIN_TARGET="${HARBOR_LOGIN_TARGET:-${HARBOR_URL}}"

BASE_IMAGE="${BASE_IMAGE:-${HARBOR_REGISTRY}/${HARBOR_PROJECT}/${HARBOR_BASE_REPO}:${HARBOR_BASE_TAG}}"
APP_IMAGE="${APP_IMAGE:-${HARBOR_REGISTRY}/${HARBOR_PROJECT}/${HARBOR_APP_REPO}:${HARBOR_APP_TAG}}"

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

update_source_tree() {
  if [[ "${UPDATE_SOURCE}" != "1" ]]; then
    echo "Skipping source update: UPDATE_SOURCE=${UPDATE_SOURCE}"
    return
  fi

  echo "Updating source tree in ${ROOT_DIR}"
  (
    cd "${ROOT_DIR}"
    git fetch "${REMOTE}" "${BRANCH}:refs/remotes/${REMOTE}/${BRANCH}"
    if [[ "${FORCE_SYNC}" == "1" ]]; then
      git checkout -B "${BRANCH}" "${REMOTE}/${BRANCH}"
      git reset --hard "${REMOTE}/${BRANCH}"
    else
      git diff --quiet && git diff --cached --quiet || {
        echo "Tracked files have local changes; commit/stash them first, set UPDATE_SOURCE=0, or set FORCE_SYNC=1" >&2
        exit 1
      }
      if git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
        git checkout "${BRANCH}"
      else
        git checkout -b "${BRANCH}" "${REMOTE}/${BRANCH}"
      fi
      git pull --ff-only "${REMOTE}" "${BRANCH}"
    fi
  )
}

detect_container_tool
update_source_tree
require_registry_image "${BASE_IMAGE}"
require_registry_image "${APP_IMAGE}"

if [[ ! -f "${SOURCE_DOCKERFILE}" ]]; then
  echo "Dockerfile not found: ${SOURCE_DOCKERFILE}" >&2
  exit 1
fi

if [[ -z "${HARBOR_PASSWORD}" ]]; then
  read -r -s -p "Harbor password for ${HARBOR_USERNAME}: " HARBOR_PASSWORD
  echo
fi

TMP_DOCKERFILE="$(mktemp "${TMPDIR:-/tmp}/Dockerfile.app.XXXXXX")"
trap 'rm -f "${TMP_DOCKERFILE}"' EXIT

cat > "${TMP_DOCKERFILE}" <<EOF
ARG BASE_IMAGE=${BASE_IMAGE}
FROM \${BASE_IMAGE}

ARG INSTALL_BROWSER=1
ARG PREINSTALLED_PLAYWRIGHT=1
ARG PLAYWRIGHT_BROWSERS_PATH_ARG=/opt/hermes/.playwright
ARG PLAYWRIGHT_ONLY_SHELL=0
ARG INSTALL_WHATSAPP_BRIDGE=0
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG PIP_INDEX_URL=
ARG UV_INDEX_URL=
ARG PLAYWRIGHT_DOWNLOAD_HOST=

ENV PLAYWRIGHT_BROWSERS_PATH=\${PLAYWRIGHT_BROWSERS_PATH_ARG}
EOF

awk '
  found { print }
  /^COPY \. \/opt\/hermes$/ { found = 1; print }
' "${SOURCE_DOCKERFILE}" >> "${TMP_DOCKERFILE}"

if ! grep -q '^COPY \. /opt/hermes$' "${TMP_DOCKERFILE}"; then
  echo "Failed to generate app Dockerfile from ${SOURCE_DOCKERFILE}" >&2
  exit 1
fi

echo "${HARBOR_PASSWORD}" | "${CONTAINER_CMD[@]}" login "${HARBOR_LOGIN_TARGET}" \
  --username "${HARBOR_USERNAME}" \
  --password-stdin

echo "Pulling base image: ${BASE_IMAGE}"
"${CONTAINER_CMD[@]}" pull "${BASE_IMAGE}"

BUILD_ARGS=(
  --build-arg "BASE_IMAGE=${BASE_IMAGE}"
  --build-arg "INSTALL_BROWSER=${INSTALL_BROWSER:-1}"
  --build-arg "PREINSTALLED_PLAYWRIGHT=${PREINSTALLED_PLAYWRIGHT:-1}"
  --build-arg "PLAYWRIGHT_BROWSERS_PATH_ARG=${PLAYWRIGHT_BROWSERS_PATH_ARG:-/opt/hermes/.playwright}"
  --build-arg "PLAYWRIGHT_ONLY_SHELL=${PLAYWRIGHT_ONLY_SHELL:-0}"
  --build-arg "INSTALL_WHATSAPP_BRIDGE=${INSTALL_WHATSAPP_BRIDGE:-0}"
  --build-arg "NPM_REGISTRY=${NPM_REGISTRY:-https://registry.npmjs.org}"
  --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL:-}"
  --build-arg "UV_INDEX_URL=${UV_INDEX_URL:-}"
  --build-arg "PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST:-}"
)

echo "Building Hermes image: ${APP_IMAGE}"
if [[ "${PREINSTALLED_PLAYWRIGHT:-1}" == "1" ]]; then
  echo "App build will reuse Playwright from base image: ${BASE_IMAGE}"
else
  echo "App build will install Playwright during the app layer build"
fi
BUILD_TAG_ARGS=(
  -t "${APP_IMAGE}"
)

if [[ -n "${LOCAL_APP_IMAGE}" && "${LOCAL_APP_IMAGE}" != "${APP_IMAGE}" ]]; then
  BUILD_TAG_ARGS+=(-t "${LOCAL_APP_IMAGE}")
fi

"${CONTAINER_CMD[@]}" build \
  -f "${TMP_DOCKERFILE}" \
  "${BUILD_TAG_ARGS[@]}" \
  "${BUILD_ARGS[@]}" \
  "${ROOT_DIR}"

if [[ "${PUSH_IMAGE}" == "1" ]]; then
  echo "Pushing Hermes image: ${APP_IMAGE}"
  "${CONTAINER_CMD[@]}" push "${APP_IMAGE}"
fi

echo "Hermes image ready: ${APP_IMAGE}"
if [[ -n "${LOCAL_APP_IMAGE}" ]]; then
  echo "Local compose image ready: ${LOCAL_APP_IMAGE}"
fi

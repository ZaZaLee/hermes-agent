#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DOCKERFILE="${ROOT_DIR}/Dockerfile.ghcr"

echo "Updating source tree in ${ROOT_DIR}"
git -C "${ROOT_DIR}" fetch origin
git -C "${ROOT_DIR}" reset --hard origin/main

HARBOR_REGISTRY="${HARBOR_REGISTRY:-127.0.0.1:81}"
HARBOR_URL="${HARBOR_URL:-http://127.0.0.1:81}"
HARBOR_USERNAME="${HARBOR_USERNAME:-admin}"
HARBOR_PASSWORD="${HARBOR_PASSWORD:-fygame#10088}"
HARBOR_PROJECT="${HARBOR_PROJECT:-ai}"
HARBOR_BASE_REPO="${HARBOR_BASE_REPO:-hermes-base}"
HARBOR_BASE_TAG="${HARBOR_BASE_TAG:-base-20260425-v1}"
HARBOR_APP_REPO="${HARBOR_APP_REPO:-hermes-agent}"
HARBOR_APP_TAG="${HARBOR_APP_TAG:-app-20260425-v1}"
PUSH_IMAGE="${PUSH_IMAGE:-0}"

BASE_IMAGE="${BASE_IMAGE:-${HARBOR_REGISTRY}/${HARBOR_PROJECT}/${HARBOR_BASE_REPO}:${HARBOR_BASE_TAG}}"
APP_IMAGE="${APP_IMAGE:-${HARBOR_REGISTRY}/${HARBOR_PROJECT}/${HARBOR_APP_REPO}:${HARBOR_APP_TAG}}"

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
ARG PLAYWRIGHT_ONLY_SHELL=1
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

echo "${HARBOR_PASSWORD}" | docker login "${HARBOR_URL}" \
  --username "${HARBOR_USERNAME}" \
  --password-stdin

echo "Pulling base image: ${BASE_IMAGE}"
docker pull "${BASE_IMAGE}"

BUILD_ARGS=(
  --build-arg "BASE_IMAGE=${BASE_IMAGE}"
  --build-arg "INSTALL_BROWSER=${INSTALL_BROWSER:-1}"
  --build-arg "PREINSTALLED_PLAYWRIGHT=${PREINSTALLED_PLAYWRIGHT:-1}"
  --build-arg "PLAYWRIGHT_BROWSERS_PATH_ARG=${PLAYWRIGHT_BROWSERS_PATH_ARG:-/opt/hermes/.playwright}"
  --build-arg "PLAYWRIGHT_ONLY_SHELL=${PLAYWRIGHT_ONLY_SHELL:-1}"
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
docker build \
  -f "${TMP_DOCKERFILE}" \
  -t "${APP_IMAGE}" \
  "${BUILD_ARGS[@]}" \
  "${ROOT_DIR}"

if [[ "${PUSH_IMAGE}" == "1" ]]; then
  echo "Pushing Hermes image: ${APP_IMAGE}"
  docker push "${APP_IMAGE}"
fi

echo "Hermes image ready: ${APP_IMAGE}"

#!/usr/bin/env bash
#
# Update hermes-agent from Git, build a container image on a k3s host, push it
# to Harbor, and optionally roll the Kubernetes deployment.

set -Eeuo pipefail

if [ ! -x "$0" ]; then
  chmod +x "$0"
  exec "$0" "$@"
fi

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPO_URL="${REPO_URL:-git@github.com:ZaZaLee/hermes-agent.git}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-}"

HARBOR_REGISTRY="${HARBOR_REGISTRY:-sig-harbor.vancygame.com}"
HARBOR_URL="${HARBOR_URL:-https://sig-harbor.vancygame.com}"
HARBOR_USERNAME="${HARBOR_USERNAME:-admin}"
HARBOR_PASSWORD="${HARBOR_PASSWORD:-tG8dS1mP6yA0tB9x}"
HARBOR_PROJECT="${HARBOR_PROJECT:-ai}"
IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-hermes-agent}"
TAG="${TAG:-ai-sig}"

CONTAINERD_SOCK="${CONTAINERD_SOCK:-unix:///run/k3s/containerd/containerd.sock}"
CONTAINERD_NAMESPACE="${CONTAINERD_NAMESPACE:-k8s.io}"

DOCKERFILE="${DOCKERFILE:-Dockerfile.ghcr}"
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"
FORCE="${FORCE:-0}"
CLEAN_UNTRACKED="${CLEAN_UNTRACKED:-0}"
PULL_BASE_IMAGES="${PULL_BASE_IMAGES:-0}"
PUSH_IMAGE="${PUSH_IMAGE:-1}"
UPDATE_K8S="${UPDATE_K8S:-1}"
KUBE_NAMESPACE="${KUBE_NAMESPACE:-ai}"
KUBE_DEPLOYMENT="${KUBE_DEPLOYMENT:-hermes-agent}"
KUBE_CONTAINER="${KUBE_CONTAINER:-hermes-agent}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-180s}"
PRUNE_OLD_IMAGES="${PRUNE_OLD_IMAGES:-0}"

# Build args used by Dockerfile.ghcr. They are harmlessly omitted when another
# Dockerfile is selected.
INSTALL_BROWSER="${INSTALL_BROWSER:-1}"
PREINSTALLED_PLAYWRIGHT="${PREINSTALLED_PLAYWRIGHT:-0}"
PLAYWRIGHT_BROWSERS_PATH_ARG="${PLAYWRIGHT_BROWSERS_PATH_ARG:-/opt/hermes/.playwright}"
PLAYWRIGHT_ONLY_SHELL="${PLAYWRIGHT_ONLY_SHELL:-1}"
INSTALL_WHATSAPP_BRIDGE="${INSTALL_WHATSAPP_BRIDGE:-0}"
APT_MIRROR="${APT_MIRROR:-}"
APT_UPDATES_MIRROR="${APT_UPDATES_MIRROR:-}"
APT_SECURITY_MIRROR="${APT_SECURITY_MIRROR:-}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmjs.org}"
PIP_INDEX_URL="${PIP_INDEX_URL:-}"
UV_INDEX_URL="${UV_INDEX_URL:-}"
PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST:-}"
BASE_IMAGE="${BASE_IMAGE:-}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
  printf '[%s] ERROR: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

detect_container_tool() {
  if command -v nerdctl >/dev/null 2>&1; then
    CONTAINER_CMD=(nerdctl --address "${CONTAINERD_SOCK}" --namespace "${CONTAINERD_NAMESPACE}" --insecure-registry)
    CONTAINER_TYPE="nerdctl"
    log "using container tool: nerdctl (${CONTAINERD_NAMESPACE}, ${CONTAINERD_SOCK})"
    return
  fi

  if command -v docker >/dev/null 2>&1; then
    CONTAINER_CMD=(docker)
    CONTAINER_TYPE="docker"
    log "using container tool: docker"
    return
  fi

  fail "neither nerdctl nor docker is available"
}

prepare_repo() {
  cd "$REPO_DIR"
  [ -d .git ] || fail "not a git repository: $REPO_DIR"
  [ -f "$DOCKERFILE" ] || fail "Dockerfile not found: $REPO_DIR/$DOCKERFILE"

  git remote set-url "$REMOTE" "$REPO_URL"

  if [ -z "$BRANCH" ]; then
    BRANCH="$(git rev-parse --abbrev-ref HEAD)"
  fi

  if [ "$BRANCH" = "HEAD" ]; then
    fail "repository is in detached HEAD; set BRANCH=<branch-name>"
  fi

  log "repository: $REPO_DIR"
  log "updating from $REMOTE/$BRANCH"
  git fetch "$REMOTE" --tags --prune

  if [ "$FORCE" = "1" ]; then
    log "FORCE=1: resetting tracked files to $REMOTE/$BRANCH"
    git checkout -B "$BRANCH" "$REMOTE/$BRANCH"
    git reset --hard "$REMOTE/$BRANCH"
    if [ "$CLEAN_UNTRACKED" = "1" ]; then
      log "CLEAN_UNTRACKED=1: removing untracked source files while preserving runtime files"
      git clean -fd \
        -e .env \
        -e cli-config.yaml \
        -e data/ \
        -e logs/ \
        -e home/ \
        -e .idea/ \
        -e .vscode/
    fi
    return
  fi

  if [ "$ALLOW_DIRTY" != "1" ] &&
    (! git diff --quiet || ! git diff --cached --quiet); then
    fail "tracked files have local changes; commit/stash them first, or set ALLOW_DIRTY=1"
  fi

  git checkout "$BRANCH"
  git pull --ff-only "$REMOTE" "$BRANCH"
}

login_harbor() {
  if [ "$PUSH_IMAGE" != "1" ]; then
    return
  fi

  [ -n "$HARBOR_USERNAME" ] || fail "HARBOR_USERNAME is empty"
  [ -n "$HARBOR_PASSWORD" ] || fail "HARBOR_PASSWORD is empty"

  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
  log "logging in to Harbor: $HARBOR_URL"
  "${CONTAINER_CMD[@]}" login -u "$HARBOR_USERNAME" -p "$HARBOR_PASSWORD" "$HARBOR_REGISTRY"
}

build_image() {
  cd "$REPO_DIR"

  COMMIT="$(git rev-parse --short HEAD)"
  FULL_IMAGE_NAME="${HARBOR_REGISTRY}/${HARBOR_PROJECT}/${IMAGE_REPOSITORY}:${TAG}"

  log "building container image"
  log "image: $FULL_IMAGE_NAME"
  log "dockerfile: $DOCKERFILE"
  log "commit: $COMMIT"

  build_flags=(-t "$FULL_IMAGE_NAME" -f "$DOCKERFILE")
  if [ "$PULL_BASE_IMAGES" = "1" ]; then
    build_flags+=(--pull)
  fi

  if [ "$DOCKERFILE" = "Dockerfile.ghcr" ]; then
    if [ -n "$BASE_IMAGE" ]; then
      build_flags+=(--build-arg "BASE_IMAGE=$BASE_IMAGE")
    fi
    build_flags+=(
      --build-arg "INSTALL_BROWSER=$INSTALL_BROWSER"
      --build-arg "PREINSTALLED_PLAYWRIGHT=$PREINSTALLED_PLAYWRIGHT"
      --build-arg "PLAYWRIGHT_BROWSERS_PATH_ARG=$PLAYWRIGHT_BROWSERS_PATH_ARG"
      --build-arg "PLAYWRIGHT_ONLY_SHELL=$PLAYWRIGHT_ONLY_SHELL"
      --build-arg "INSTALL_WHATSAPP_BRIDGE=$INSTALL_WHATSAPP_BRIDGE"
      --build-arg "APT_MIRROR=$APT_MIRROR"
      --build-arg "APT_UPDATES_MIRROR=$APT_UPDATES_MIRROR"
      --build-arg "APT_SECURITY_MIRROR=$APT_SECURITY_MIRROR"
      --build-arg "NPM_REGISTRY=$NPM_REGISTRY"
      --build-arg "PIP_INDEX_URL=$PIP_INDEX_URL"
      --build-arg "UV_INDEX_URL=$UV_INDEX_URL"
      --build-arg "PLAYWRIGHT_DOWNLOAD_HOST=$PLAYWRIGHT_DOWNLOAD_HOST"
    )
  fi

  "${CONTAINER_CMD[@]}" build "${build_flags[@]}" .
}

push_image() {
  if [ "$PUSH_IMAGE" != "1" ]; then
    log "PUSH_IMAGE=0: skipped Harbor push"
    return
  fi

  log "pushing image: $FULL_IMAGE_NAME"
  if ! "${CONTAINER_CMD[@]}" push "$FULL_IMAGE_NAME"; then
    log "push failed; retrying after Harbor login"
    "${CONTAINER_CMD[@]}" login -u "$HARBOR_USERNAME" -p "$HARBOR_PASSWORD" "$HARBOR_REGISTRY"
    "${CONTAINER_CMD[@]}" push "$FULL_IMAGE_NAME"
  fi

  "${CONTAINER_CMD[@]}" logout "$HARBOR_REGISTRY" >/dev/null 2>&1 || true
}

update_k8s() {
  if [ "$UPDATE_K8S" != "1" ]; then
    log "UPDATE_K8S=0: skipped Kubernetes rollout"
    return
  fi

  require_cmd kubectl
  log "updating Kubernetes deployment $KUBE_NAMESPACE/$KUBE_DEPLOYMENT"
  kubectl -n "$KUBE_NAMESPACE" set image \
    "deployment/$KUBE_DEPLOYMENT" \
    "$KUBE_CONTAINER=$FULL_IMAGE_NAME"
  kubectl -n "$KUBE_NAMESPACE" rollout restart "deployment/$KUBE_DEPLOYMENT"
  kubectl -n "$KUBE_NAMESPACE" rollout status \
    "deployment/$KUBE_DEPLOYMENT" \
    --timeout="$ROLLOUT_TIMEOUT"
}

prune_images() {
  if [ "$PRUNE_OLD_IMAGES" != "1" ]; then
    return
  fi

  log "removing dangling container images"
  "${CONTAINER_CMD[@]}" image prune -f
}

main() {
  require_cmd git
  detect_container_tool
  "${CONTAINER_CMD[@]}" version >/dev/null 2>&1 || fail "$CONTAINER_TYPE is not available"

  prepare_repo
  login_harbor
  build_image
  push_image
  update_k8s
  prune_images

  log "done: $FULL_IMAGE_NAME"
}

main "$@"

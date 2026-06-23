#!/usr/bin/env bash
set -euo pipefail

image_tag="${FCT_NIX_CLOSURE_IMAGE_TAG:-fct-nixos-closure-manifest:local}"
dockerfile="${FCT_DOCKERFILE:-Dockerfile.nixos}"
platform="${FCT_DOCKER_PLATFORM:-}"

build_args=(
  "--file" "$dockerfile"
  "--target" "fct-nix-profile"
  "--build-arg" "FCT_NIX_CLOSURE_MODE=generate"
  "--tag" "$image_tag"
)

if [ -n "$platform" ]; then
  build_args=("--platform" "$platform" "${build_args[@]}")
fi

docker build "${build_args[@]}" .

architecture="$(docker image inspect --format '{{.Architecture}}' "$image_tag")"
case "$architecture" in
  amd64) nix_system="x86_64-linux" ;;
  arm64) nix_system="aarch64-linux" ;;
  *) echo "unsupported docker image architecture: $architecture" >&2; exit 1 ;;
esac

container_id="$(docker create "$image_tag")"
tmp_manifest="$(mktemp)"
trap 'docker rm -f "$container_id" >/dev/null 2>&1 || true; rm -f "$tmp_manifest"' EXIT

docker cp "$container_id:/etc/fct-workspace/nix-closure.txt" "$tmp_manifest"
mkdir -p nix
cp "$tmp_manifest" "nix/fct-workspace-closure.${nix_system}.txt"

echo "Wrote nix/fct-workspace-closure.${nix_system}.txt from $image_tag"

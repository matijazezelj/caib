#!/usr/bin/env bash
# Inventory every container image this repo references, and fail on the drift
# dependabot will not catch for us.
#
# Dependabot covers the Dockerfiles (`docker`) and the compose files
# (`docker-compose`) — see .github/dependabot.yml. What it does NOT cover:
#
#   * service-container images in .github/workflows/*.yml. The
#     `github-actions` ecosystem only updates `uses:` refs, so the
#     timescaledb image the test job runs against is nobody's job to bump.
#   * agreement between copies of the same pin. timescale/timescaledb is
#     named in docker-compose.yml, backup/Dockerfile and the CI service
#     container; dependabot opens one PR per file and is happy to leave them
#     on different tags. CI asserts the TSL license against whatever the
#     compose file says, so a stale CI pin means testing the wrong build.
#
# Run: bash scripts/check_image_pins.sh

set -euo pipefail

cd "$(dirname "$0")/.."

# Images deliberately left floating. The renderer is a throwaway sidecar for
# regenerating README screenshots, never part of a running stack.
UNPINNED_OK="grafana/grafana-image-renderer"

# Every copy of these repositories must carry the same tag.
MUST_AGREE="timescale/timescaledb"

# --- collect ------------------------------------------------------------
# One "<file>\t<repository>\t<tag>" row per reference, tag "-" when absent.

compose_and_workflow_files() {
  git ls-files 'docker-compose.yml' 'compose.*.yml' '.github/workflows/*.yml' \
               '.github/workflows/*.yaml'
}

dockerfiles() {
  git ls-files '*Dockerfile' '*Dockerfile.*'
}

emit_row() {
  # $1 file, $2 image reference (repo[:tag][@digest])
  local file=$1 ref=$2 repo tag
  ref=${ref%%@*}
  case $ref in
    *:*) repo=${ref%:*}; tag=${ref##*:} ;;
    *)   repo=$ref;      tag="-" ;;
  esac
  printf '%s\t%s\t%s\n' "$file" "$repo" "$tag"
}

collect() {
  local file ref stages

  for file in $(compose_and_workflow_files); do
    # `image: foo/bar:tag` in compose services and workflow service containers.
    while read -r ref; do
      [ -n "$ref" ] && emit_row "$file" "$ref"
    done < <(sed -n 's/^[[:space:]]*image:[[:space:]]*\([^[:space:]#]\{1,\}\).*/\1/p' "$file")
  done

  for file in $(dockerfiles); do
    # Build stage names are not images; collect them so FROM can skip them.
    stages=$(sed -n 's/.*[[:space:]][Aa][Ss][[:space:]]\{1,\}\([A-Za-z0-9_.-]\{1,\}\).*/\1/p' "$file")
    while read -r ref; do
      [ -z "$ref" ] && continue
      grep -qxF "$ref" <<<"$stages" && continue
      emit_row "$file" "$ref"
    done < <(sed -n 's/^[[:space:]]*[Ff][Rr][Oo][Mm][[:space:]]\{1,\}\([^[:space:]]\{1,\}\).*/\1/p' "$file")

    # `COPY --from=<image>` pulls a binary out of another image.
    while read -r ref; do
      [ -z "$ref" ] && continue
      grep -qxF "$ref" <<<"$stages" && continue
      emit_row "$file" "$ref"
    done < <(sed -n 's/^[[:space:]]*[Cc][Oo][Pp][Yy][[:space:]].*--from=\([^[:space:]]\{1,\}\).*/\1/p' "$file")
  done
}

rows=$(collect | sort -u)

if [ -z "$rows" ]; then
  echo "check_image_pins: found no image references at all — the globs are wrong." >&2
  exit 1
fi

printf '%-28s %-42s %s\n' FILE IMAGE TAG
while IFS=$'\t' read -r file repo tag; do
  printf '%-28s %-42s %s\n' "$file" "$repo" "$tag"
done <<<"$rows"
echo

# --- check --------------------------------------------------------------

fail=0

while IFS=$'\t' read -r file repo tag; do
  case $repo in
    caib-*) continue ;;  # built here by compose, no upstream to pin to
  esac
  if grep -qxF "$repo" <<<"$UNPINNED_OK"; then
    continue
  fi
  if [ "$tag" = "-" ] || [ "$tag" = "latest" ]; then
    echo "unpinned: $file pins $repo to '${tag/-/no tag}'" >&2
    fail=1
  fi
done <<<"$rows"

while read -r repo; do
  [ -n "$repo" ] || continue
  tags=$(awk -F'\t' -v r="$repo" '$2 == r { print $3 }' <<<"$rows" | sort -u)
  if [ "$(wc -l <<<"$tags")" -gt 1 ]; then
    echo "disagreement: $repo is pinned to more than one tag:" >&2
    awk -F'\t' -v r="$repo" '$2 == r { print "  " $1 " -> " $3 }' <<<"$rows" >&2
    fail=1
  fi
done <<<"$MUST_AGREE"

if [ "$fail" -ne 0 ]; then
  echo >&2
  echo "check_image_pins: FAILED" >&2
  exit 1
fi

echo "check_image_pins: OK"

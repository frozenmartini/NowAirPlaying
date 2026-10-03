#!/bin/sh
# Build the release assets:
#   dist/nowairplaying-<ver>-trixie-arm64.tar.gz and its .sha256, the release
#     that install/bootstrap.sh fetches
#   dist/nowairplaying-bootstrap-<ver>.sh and its .sha256, the tag's
#     install/bootstrap.sh, so Home Assistant pins it from the release like
#     the tarball (docs/INSTALL-STATE.md)
#
# The tarball is the tagged tree (git archive of v<ver>) plus the built trixie
# packages in build/out/trixie/debs, under one top directory
# nowairplaying-<ver>/. Run it as your normal user after build.sh, from a
# clean tree whose HEAD is tagged v<VERSION>. Upload all four files to the
# GitHub release of that tag.
set -eu

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"
VER=$(cat VERSION)
TAG=v$VER

[ -z "$(git status --porcelain)" ] || die "the tree has uncommitted changes"
[ "$(git rev-parse HEAD)" = "$(git rev-parse -q --verify "$TAG^{commit}" || true)" ] \
    || die "HEAD is not tagged $TAG"

# shellcheck source=versions.env
. build/versions.env
DEBS="build/out/trixie/debs/nowairplaying-nqptp_${NQPTP_VERSION}-0nap${NAP_REVISION}+deb13_arm64.deb
build/out/trixie/debs/nowairplaying-shairport-sync_${SHAIRPORT_VERSION}-0nap${NAP_REVISION}+deb13_arm64.deb"
for f in $DEBS; do [ -f "$f" ] || die "missing $f: run build/build.sh for trixie first"; done

NAME=nowairplaying-$VER-trixie-arm64
TOP=nowairplaying-$VER
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

git archive --prefix="$TOP/" "$TAG" | tar -x -C "$STAGE"
mkdir -p "$STAGE/$TOP/build/out/trixie/debs"
# shellcheck disable=SC2086
cp $DEBS "$STAGE/$TOP/build/out/trixie/debs/"

mkdir -p dist
# fixed order, owner and times, so the same tag builds the same bytes
tar --sort=name --owner=0 --group=0 --numeric-owner \
    --mtime="@$(git log -1 --format=%ct "$TAG")" \
    -C "$STAGE" -cf - "$TOP" | gzip -n -9 > "dist/$NAME.tar.gz"
(cd dist && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256")

BOOT=nowairplaying-bootstrap-$VER.sh
git show "$TAG:install/bootstrap.sh" > "dist/$BOOT"
(cd dist && sha256sum "$BOOT" > "$BOOT.sha256")

cat "dist/$NAME.tar.gz.sha256" "dist/$BOOT.sha256"
echo "Upload the four files in dist/ for $VER to the $TAG release."

#!/usr/bin/env bash
# Build the fake Hermes remote used by dev-sandbox install/update tests.
#
# A shallow source checkout cannot safely serve its HEAD as an update target:
# the commit names a parent object that the checkout does not have. Publish a
# synthetic commit with the same tree and the installed release as its parent,
# so the fake remote contains the complete update range the real updater needs.

set -euo pipefail

if [ "$#" -ne 7 ]; then
  echo "usage: $0 SOURCE_ROOT SOURCE_COMMIT UPSTREAM_REPO UPSTREAM_COMMIT INSTALL_REF FAKE_REPO PROMOTE_FILE" >&2
  exit 2
fi

SOURCE_ROOT="$1"
SOURCE_COMMIT_INPUT="$2"
UPSTREAM_REPO="$3"
UPSTREAM_COMMIT_INPUT="$4"
INSTALL_REF="$5"
FAKE_REPO="$6"
PROMOTE_FILE="$7"

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

SOURCE_COMMIT="$(git -C "$SOURCE_ROOT" rev-parse --verify "$SOURCE_COMMIT_INPUT^{commit}" 2>/dev/null)" \
  || fail "could not verify source commit $SOURCE_COMMIT_INPUT"
SOURCE_TREE="$(git -C "$SOURCE_ROOT" rev-parse --verify "$SOURCE_COMMIT^{tree}" 2>/dev/null)" \
  || fail "could not verify source tree for $SOURCE_COMMIT"

[ "$(git --git-dir="$FAKE_REPO" rev-parse --is-bare-repository 2>/dev/null)" = "true" ] \
  || fail "fake remote is not a bare Git repository: $FAKE_REPO"

UPSTREAM_COMMIT=""
if [ -n "$INSTALL_REF" ]; then
  UPSTREAM_COMMIT="$(git -C "$UPSTREAM_REPO" rev-parse --verify "$UPSTREAM_COMMIT_INPUT^{commit}" 2>/dev/null)" \
    || fail "could not verify install commit $UPSTREAM_COMMIT_INPUT"
  git --git-dir="$FAKE_REPO" fetch -q --update-shallow --force "$UPSTREAM_REPO" \
    "+$UPSTREAM_COMMIT:refs/heads/main"
  PUBLISHED_BASE="$(git --git-dir="$FAKE_REPO" rev-parse --verify refs/heads/main 2>/dev/null)" \
    || fail "install commit fetch did not publish fake main"
  [ "$PUBLISHED_BASE" = "$UPSTREAM_COMMIT" ] \
    || fail "fake main is $PUBLISHED_BASE, expected install commit $UPSTREAM_COMMIT"
fi

SOURCE_IS_SHALLOW="$(git -C "$SOURCE_ROOT" rev-parse --is-shallow-repository 2>/dev/null)" \
  || fail "could not inspect source repository depth"
SOURCE_IS_DIRTY=false
[ -n "$(git -C "$SOURCE_ROOT" status --porcelain)" ] && SOURCE_IS_DIRTY=true

SNAPSHOT_REASON=""
if [ "$SOURCE_IS_DIRTY" = true ]; then
  SNAPSHOT_REASON="dirty worktree"
elif [ "$SOURCE_IS_SHALLOW" = "true" ]; then
  SNAPSHOT_REASON="shallow source checkout"
elif [ -n "$INSTALL_REF" ] \
  && ! git -C "$SOURCE_ROOT" merge-base --is-ancestor "$UPSTREAM_COMMIT" "$SOURCE_COMMIT" 2>/dev/null; then
  SNAPSHOT_REASON="install commit is not an ancestor of the source"
fi

SOURCE_REPO="$SOURCE_ROOT"
SOURCE_REF="$SOURCE_COMMIT"
SNAPSHOT_REPO=""
SNAPSHOT_TREE="$SOURCE_TREE"
if [ -n "$SNAPSHOT_REASON" ]; then
  echo "[sandbox] publishing synthetic update target: $SNAPSHOT_REASON" >&2
  SNAPSHOT_REPO="$(mktemp -d -t hermes-sandbox-snapshot.XXXXXX)"
  trap 'rm -rf -- "$SNAPSHOT_REPO" 2>/dev/null || true' EXIT
  git -C "$SNAPSHOT_REPO" init -q
  git -C "$SNAPSHOT_REPO" fetch -q --update-shallow "$SOURCE_ROOT" "$SOURCE_COMMIT"
  git -C "$SNAPSHOT_REPO" config user.name 'Hermes sandbox'
  git -C "$SNAPSHOT_REPO" config user.email 'sandbox@invalid'
  GIT_DIR="$SNAPSHOT_REPO/.git" GIT_WORK_TREE="$SOURCE_ROOT" git read-tree "$SOURCE_COMMIT"
  GIT_DIR="$SNAPSHOT_REPO/.git" GIT_WORK_TREE="$SOURCE_ROOT" git add -A -- .
  SNAPSHOT_TREE="$(GIT_DIR="$SNAPSHOT_REPO/.git" git write-tree)"

  PARENT_ARGS=()
  if [ -n "$INSTALL_REF" ]; then
    git -C "$SNAPSHOT_REPO" fetch -q --update-shallow "$FAKE_REPO" "$UPSTREAM_COMMIT"
    git -C "$SNAPSHOT_REPO" cat-file -e "$UPSTREAM_COMMIT^{commit}" \
      || fail "synthetic target parent is unavailable: $UPSTREAM_COMMIT"
    PARENT_ARGS=(-p "$UPSTREAM_COMMIT")
  elif [ "$SOURCE_IS_SHALLOW" != "true" ]; then
    PARENT_ARGS=(-p "$SOURCE_COMMIT")
  fi

  SOURCE_REF="$(GIT_DIR="$SNAPSHOT_REPO/.git" git commit-tree "$SNAPSHOT_TREE" \
    "${PARENT_ARGS[@]}" -m "sandbox snapshot: $SNAPSHOT_REASON")"
  SOURCE_REPO="$SNAPSHOT_REPO"
fi

if [ -n "$INSTALL_REF" ]; then
  TARGET_REF="refs/hermes-sandbox/next"
else
  TARGET_REF="refs/heads/main"
fi

git --git-dir="$FAKE_REPO" fetch -q --update-shallow --force "$SOURCE_REPO" \
  "+$SOURCE_REF:$TARGET_REF"
PUBLISHED_TARGET="$(git --git-dir="$FAKE_REPO" rev-parse --verify "$TARGET_REF" 2>/dev/null)" \
  || fail "target fetch did not publish $TARGET_REF"
[ "$PUBLISHED_TARGET" = "$SOURCE_REF" ] \
  || fail "$TARGET_REF is $PUBLISHED_TARGET, expected $SOURCE_REF"

PUBLISHED_TREE="$(git --git-dir="$FAKE_REPO" rev-parse --verify "$SOURCE_REF^{tree}" 2>/dev/null)" \
  || fail "published target tree is unavailable"
[ "$PUBLISHED_TREE" = "$SNAPSHOT_TREE" ] \
  || fail "published target tree $PUBLISHED_TREE does not match source tree $SNAPSHOT_TREE"

if [ -n "$INSTALL_REF" ]; then
  if [ -n "$SNAPSHOT_REASON" ]; then
    PUBLISHED_PARENT="$(git --git-dir="$FAKE_REPO" rev-parse --verify "$SOURCE_REF^" 2>/dev/null)" \
      || fail "synthetic target parent is unavailable after publication"
    [ "$PUBLISHED_PARENT" = "$UPSTREAM_COMMIT" ] \
      || fail "synthetic target parent is $PUBLISHED_PARENT, expected $UPSTREAM_COMMIT"
  fi
  git --git-dir="$FAKE_REPO" rev-list --objects "$UPSTREAM_COMMIT..$SOURCE_REF" >/dev/null \
    || fail "published update range is not object-complete"
  printf '%s\n' "$SOURCE_REF" > "$PROMOTE_FILE"
else
  rm -f -- "$PROMOTE_FILE"
fi

printf '%s\n' "$SOURCE_REF"

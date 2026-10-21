#!/bin/sh
# Points the `assets` branch at one new parentless commit holding the README's
# media, as built into .github/assets/ by build_all.py:
#
#   python .github/scripts/build_all.py
#   .github/scripts/publish-assets.sh
#   git push --force-with-lease origin assets
#
# The branch never has more than that one commit, so a clone fetches only the
# current media, and `uv tool install` (which reads the default branch) none of
# it. The commit is made from git objects alone (hash-object, a temporary
# index, write-tree, commit-tree, update-ref): no checkout, no change to any
# working tree or index. Nothing is pushed.
set -eu

root=$(git rev-parse --show-toplevel)
dir="$root/.github/assets"
files="banner.svg demo.webp demo.png feature-adding-up.svg feature-why.svg feature-fan-out.svg feature-checks.svg feature-trust.svg feature-private.svg"

GIT_INDEX_FILE=$(mktemp -u "${TMPDIR:-/tmp}/capcom-assets-index.XXXXXX")
export GIT_INDEX_FILE
trap 'rm -f "$GIT_INDEX_FILE"' EXIT

for file in $files; do
  [ -f "$dir/$file" ] || { echo "missing $dir/$file" >&2; exit 1; }
  blob=$(git hash-object -w "$dir/$file")
  git update-index --add --cacheinfo "100644,$blob,$file"
done

source=$(git rev-parse --short HEAD)
commit=$(git commit-tree "$(git write-tree)" -m "chore(assets): README media" -m "Generated from $source by .github/scripts/build_all.py, from synthetic logs. This branch always holds this one commit: each update replaces it.")
git update-ref -m "publish README media" refs/heads/assets "$commit"
echo "assets -> $(git rev-parse --short "$commit"): $files"

#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
site_root=$(cd -- "$script_dir/.." && pwd)
repo_root=$(cd -- "$site_root/.." && pwd)
public_origin=${FACTLANE_SITE_URL:-https://factlane.pages.dev}
output_root=${FACTLANE_PUBLICATION_CI_OUTPUT:-$site_root/.publication-ci}

if [[ ${FACTLANE_PUBLIC_BUILD:-1} != 1 ]]; then
  echo 'HOLD: publication CI must run with FACTLANE_PUBLIC_BUILD=1' >&2
  exit 1
fi

if [[ -n $(git -C "$repo_root" status --porcelain) ]]; then
  echo 'HOLD: publication CI requires an exact clean committed source state' >&2
  git -C "$repo_root" status --short >&2
  exit 1
fi

source_commit=$(git -C "$repo_root" rev-parse HEAD)
source_tree=$(git -C "$repo_root" rev-parse 'HEAD^{tree}')
short_commit=${source_commit:0:8}
tmp_root=$(mktemp -d "${TMPDIR:-/tmp}/factlane-publication-ci.${short_commit}.XXXXXX")
clone_a="$tmp_root/source-a"
clone_b="$tmp_root/source-b"
build_a="$tmp_root/build-a"
build_b="$tmp_root/build-b"
composed_a="$tmp_root/composed-a"
composed_b="$tmp_root/composed-b"
receipt_a="$tmp_root/OVERLAY_COMPOSITION_A.json"
receipt_b="$tmp_root/OVERLAY_COMPOSITION_B.json"
routes_a="$tmp_root/ROUTES_A.txt"
routes_b="$tmp_root/ROUTES_B.txt"
eligibility="$tmp_root/PUBLICATION_ELIGIBILITY.json"
readiness="$tmp_root/READINESS_RESULT.json"
verified_root="$tmp_root/verified-root"
negative_build="$tmp_root/negative-release-bound-build"
negative_collision_base="$tmp_root/negative-overlay-collision-base"
package_dir="$output_root/package"
freeze_result="$output_root/FREEZE_RESULT.json"
consumer_readiness="$output_root/CONSUMER_READINESS_RESULT.json"
server_pid=''

cleanup() {
  if [[ -n "$server_pid" ]]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  rm -rf "$tmp_root"
}
trap cleanup EXIT

rm -rf "$output_root"
mkdir -p "$output_root"

echo "PUBLICATION_CI_SOURCE_COMMIT=$source_commit"
echo "PUBLICATION_CI_SOURCE_TREE=$source_tree"
echo "PUBLICATION_CI_ORIGIN=$public_origin"

git clone --quiet --local --no-hardlinks "$repo_root" "$clone_a"
git clone --quiet --local --no-hardlinks "$repo_root" "$clone_b"
git -C "$clone_a" checkout --quiet --detach "$source_commit"
git -C "$clone_b" checkout --quiet --detach "$source_commit"

build_one() {
  local clone=$1
  local out=$2
  (
    cd "$clone/site"
    npm ci --prefer-offline --no-audit --no-fund
    npm run clear
    FACTLANE_SITE_URL="$public_origin" FACTLANE_PUBLIC_BUILD=1 npm run build -- --out-dir "$out"
  )
}

(
  cd "$clone_a/site"
  npm ci --prefer-offline --no-audit --no-fund
  npm run typecheck
  npm run clear
  FACTLANE_SITE_URL="$public_origin" FACTLANE_PUBLIC_BUILD=1 npm run build -- --out-dir "$build_a"
)
build_one "$clone_b" "$build_b"

base_manifest_a="$tmp_root/base-a.sha256"
base_manifest_b="$tmp_root/base-b.sha256"
(cd "$build_a" && find . -type f -print0 | sort -z | xargs -0 sha256sum) > "$base_manifest_a"
(cd "$build_b" && find . -type f -print0 | sort -z | xargs -0 sha256sum) > "$base_manifest_b"
cmp -s "$base_manifest_a" "$base_manifest_b" || {
  echo 'HOLD: isolated public build manifests differ' >&2
  diff -u "$base_manifest_a" "$base_manifest_b" >&2 || true
  exit 1
}

(
  cd "$clone_a/site"
  FACTLANE_BUILD_DIR="$build_a" FACTLANE_SEO_MODE=public FACTLANE_SITE_URL="$public_origin" node scripts/check-doc-seo.mjs
  FACTLANE_BUILD_DIR="$build_a" FACTLANE_SEO_MODE=public FACTLANE_SITE_URL="$public_origin" node scripts/check-answer-authority.mjs
  node scripts/check-publication-eligibility.mjs --build "$build_a" --receipt "$eligibility"
  node scripts/compose-publication-artifact.mjs \
    --base "$build_a" \
    --output "$composed_a" \
    --origin "$public_origin" \
    --receipt "$receipt_a" \
    --routes "$routes_a"
)

cp -a "$build_a" "$negative_build"
python - "$negative_build/index.html" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text()
marker = '<p>FACTLANE_RELEASE_BOUND_NEGATIVE_SENTINEL</p>'
pattern = r'(<section\b[^>]*id="engineering-rigor"[^>]*>[\s\S]*?)(</section>)'
updated, count = re.subn(pattern, rf'\1{marker}\2', text, count=1)
if count != 1:
    raise SystemExit('negative control could not find engineering-rigor section')
path.write_text(updated)
PY
negative_eligibility="$tmp_root/NEGATIVE_PUBLICATION_ELIGIBILITY.json"
(
  cd "$clone_a/site"
  node scripts/check-publication-eligibility.mjs --build "$negative_build" --receipt "$negative_eligibility" >/dev/null
)
[[ $(node -p "require('$negative_eligibility').publicationClass") == RELEASE_BOUND ]] || {
  echo 'HOLD: release-bound negative control was not classified RELEASE_BOUND' >&2
  exit 1
}
[[ $(node -p "require('$negative_eligibility').publicationEligibility") == HOLD_UNRELEASED_CONTRACT ]] || {
  echo 'HOLD: release-bound negative control did not fail closed for publication' >&2
  exit 1
}

cp -a "$build_a" "$negative_collision_base"
cp "$clone_a/site/publication/overlays/discovery/1d226189096d15c62deace5c0bb3ade7.txt" "$negative_collision_base/"
if (
  cd "$clone_a/site"
  node scripts/compose-publication-artifact.mjs \
    --base "$negative_collision_base" \
    --output "$tmp_root/negative-overlay-output" \
    --origin "$public_origin" \
    --receipt "$tmp_root/negative-overlay-receipt.json" \
    --routes "$tmp_root/negative-overlay-routes.txt" >/dev/null 2>&1
); then
  echo 'HOLD: overlay collision negative control unexpectedly composed' >&2
  exit 1
fi
(
  cd "$clone_b/site"
  node scripts/compose-publication-artifact.mjs \
    --base "$build_b" \
    --output "$composed_b" \
    --origin "$public_origin" \
    --receipt "$receipt_b" \
    --routes "$routes_b"
)

cmp -s "$routes_a" "$routes_b" || {
  echo 'HOLD: isolated route inventories differ' >&2
  exit 1
}

(
  cd "$clone_a/site"
  exec env WRANGLER_SEND_METRICS=false npx wrangler pages dev "$composed_a" \
    --ip 127.0.0.1 \
    --port 4260
) > "$tmp_root/pages-runtime.log" 2>&1 &
server_pid=$!
for _ in $(seq 1 100); do
  if curl -fsS http://127.0.0.1:4260/ >/dev/null 2>&1; then break; fi
  sleep 0.1
done
curl -fsS http://127.0.0.1:4260/ >/dev/null
(
  cd "$clone_a/site"
  FACTLANE_VERIFY_BASE_URL=http://127.0.0.1:4260 \
  FACTLANE_EXPECTED_ORIGIN="$public_origin" \
  FACTLANE_VERIFY_MODE=public \
  node scripts/check-publication-readiness.mjs > "$readiness"
)
kill "$server_pid" 2>/dev/null || true
wait "$server_pid" 2>/dev/null || true
server_pid=''

(
  cd "$clone_a/site"
  node scripts/freeze-publication-package.mjs \
    --root-a "$composed_a" \
    --root-b "$composed_b" \
    --package "$package_dir" \
    --origin "$public_origin" \
    --eligibility "$eligibility" \
    --readiness "$readiness" \
    --composition "$receipt_a" | tee "$freeze_result"
)

expected_package_digest=$(sha256sum "$package_dir/PACKAGE_CONTENTS.sha256" | awk '{print $1}')
(
  cd "$package_dir"
  sha256sum -c PACKAGE_CONTENTS.sha256
  node consumer/scripts/verify-publication-package.mjs \
    --package . \
    --expected-package-contents-sha256 "$expected_package_digest" \
    --extract "$verified_root"
)

if (
  cd "$clone_a/site"
  node scripts/check-publication-crossing.mjs \
    --package "$package_dir" \
    --expected-package-contents-sha256 "$expected_package_digest" \
    --production-authorization-sha256 0000000000000000000000000000000000000000000000000000000000000000 \
    --expected-origin "$public_origin" \
    --expected-project factlane >/dev/null 2>&1
); then
  echo 'HOLD: unauthorized crossing negative control unexpectedly passed' >&2
  exit 1
fi

node "$package_dir/consumer/scripts/serve-publication-static.mjs" \
  --root "$verified_root" \
  --routes "$package_dir/ROUTES.txt" \
  --host 127.0.0.1 \
  --port 4261 > "$tmp_root/consumer-static-host.log" 2>&1 &
server_pid=$!
for _ in $(seq 1 100); do
  if curl -fsS http://127.0.0.1:4261/ >/dev/null 2>&1; then break; fi
  sleep 0.1
done
curl -fsS http://127.0.0.1:4261/ >/dev/null
FACTLANE_VERIFY_BASE_URL=http://127.0.0.1:4261 \
FACTLANE_EXPECTED_ORIGIN="$public_origin" \
FACTLANE_VERIFY_MODE=public \
node "$package_dir/consumer/scripts/check-publication-readiness.mjs" > "$consumer_readiness"
kill "$server_pid" 2>/dev/null || true
wait "$server_pid" 2>/dev/null || true
server_pid=''

cp "$eligibility" "$output_root/PUBLICATION_ELIGIBILITY.json"
cp "$receipt_a" "$output_root/OVERLAY_COMPOSITION.json"
cp "$readiness" "$output_root/READINESS_RESULT.json"

cat > "$output_root/QUALIFICATION.env" <<EOF
SOURCE_COMMIT=$source_commit
SOURCE_TREE=$source_tree
PUBLIC_ORIGIN=$public_origin
PACKAGE_CONTENTS_SHA256=$expected_package_digest
PUBLICATION_CLASS=$(node -p "require('$eligibility').publicationClass")
PUBLICATION_ELIGIBILITY=$(node -p "require('$eligibility').publicationEligibility")
EOF

echo "PUBLICATION_CI=PASS"
cat "$output_root/QUALIFICATION.env"

#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
site_root=$(cd -- "$script_dir/.." && pwd)
repo_root=$(cd -- "$site_root/.." && pwd)
public_origin=${FACTLANE_SITE_URL:-https://factlane.pages.dev}
output_root=${FACTLANE_PUBLICATION_CI_OUTPUT:-$site_root/.publication-ci}
safe_output_root=$site_root/.publication-ci
protected_base_commit=${FACTLANE_PROTECTED_BASE_COMMIT:-}

output_root=$(node -e 'const path=require("node:path"); process.stdout.write(path.resolve(process.argv[1]))' "$output_root")
safe_output_root=$(node -e 'const path=require("node:path"); process.stdout.write(path.resolve(process.argv[1]))' "$safe_output_root")
if [[ "$output_root" != "$safe_output_root" ]]; then
  echo "HOLD: FACTLANE_PUBLICATION_CI_OUTPUT must be exactly $safe_output_root" >&2
  exit 1
fi

if [[ ${FACTLANE_PUBLIC_BUILD:-1} != 1 ]]; then
  echo 'HOLD: publication CI must run with FACTLANE_PUBLIC_BUILD=1' >&2
  exit 1
fi

if [[ ! "$protected_base_commit" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'HOLD: FACTLANE_PROTECTED_BASE_COMMIT must be the exact externally supplied protected/base SHA' >&2
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
baseline_clone="$tmp_root/released-baseline-source"
build_a="$tmp_root/build-a"
build_b="$tmp_root/build-b"
baseline_build="$tmp_root/released-baseline-build"
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
negative_llms_build="$tmp_root/negative-llms-release-bound-build"
negative_footer_build="$tmp_root/negative-footer-release-bound-build"
negative_external_href_build="$tmp_root/negative-external-href-release-bound-build"
negative_collision_base="$tmp_root/negative-overlay-collision-base"
package_dir="$output_root/package"
freeze_result="$output_root/FREEZE_RESULT.json"
consumer_readiness="$output_root/CONSUMER_READINESS_RESULT.json"
crossing_result="$output_root/CROSSING_GATE_RESULT.json"
server_pid=''

stop_server() {
  if [[ -n "$server_pid" ]]; then
    kill -TERM -- "-$server_pid" 2>/dev/null || kill -TERM "$server_pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      if ! kill -0 "$server_pid" 2>/dev/null; then break; fi
      sleep 0.1
    done
    kill -KILL -- "-$server_pid" 2>/dev/null || kill -KILL "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
    server_pid=''
  fi
}

cleanup() {
  stop_server
  rm -rf "$tmp_root"
}
trap cleanup EXIT

rm -rf "$output_root"
mkdir -p "$output_root"

echo "PUBLICATION_CI_SOURCE_COMMIT=$source_commit"
echo "PUBLICATION_CI_SOURCE_TREE=$source_tree"
echo "PUBLICATION_CI_ORIGIN=$public_origin"
echo "PUBLICATION_CI_PROTECTED_BASE_COMMIT=$protected_base_commit"

released_snapshot="$site_root/publication/released-contract-v0.1.3.json"
released_snapshot_relative=${released_snapshot#"$repo_root/"}
control_introduction_commit=$(git -C "$repo_root" log --diff-filter=A --format=%H --reverse -- "$released_snapshot_relative" | head -n 1)
[[ "$control_introduction_commit" =~ ^[0-9a-f]{40}$ ]] || {
  echo 'HOLD: could not derive the historical publication-control introduction commit' >&2
  exit 1
}
released_baseline_commit=$(git -C "$repo_root" rev-parse "$control_introduction_commit^")
if git -C "$repo_root" cat-file -e "$protected_base_commit:$released_snapshot_relative" 2>/dev/null; then
  [[ $(git -C "$repo_root" merge-base "$control_introduction_commit" "$protected_base_commit") == "$control_introduction_commit" ]] || {
    echo 'HOLD: publication-control introduction is not part of the externally supplied protected/base history' >&2
    exit 1
  }
else
  [[ "$released_baseline_commit" == "$protected_base_commit" ]] || {
    echo 'HOLD: bootstrap publication baseline must equal the externally supplied protected/base SHA' >&2
    exit 1
  }
fi
[[ $(git -C "$repo_root" merge-base "$protected_base_commit" "$source_commit") == "$protected_base_commit" ]] || {
  echo 'HOLD: externally supplied protected/base SHA is not an ancestor of the candidate' >&2
  exit 1
}

probe_loopback() {
  curl --connect-timeout 1 --max-time 3 -fsS "$1" >/dev/null 2>&1
}

wait_for_loopback() {
  local url=$1
  local timeout_seconds=${2:-90}
  local deadline=$((SECONDS + timeout_seconds))
  while (( SECONDS < deadline )); do
    if probe_loopback "$url"; then return 0; fi
    sleep 0.5
  done
  return 1
}

allocate_loopback_port() {
  node - <<'NODE'
const net = require('node:net');
const server = net.createServer();
server.unref();
server.listen(0, '127.0.0.1', () => {
  process.stdout.write(String(server.address().port));
  server.close();
});
NODE
}

git clone --quiet --local --no-hardlinks "$repo_root" "$clone_a"
git clone --quiet --local --no-hardlinks "$repo_root" "$clone_b"
git clone --quiet --local --no-hardlinks "$repo_root" "$baseline_clone"
git -C "$clone_a" checkout --quiet --detach "$source_commit"
git -C "$clone_b" checkout --quiet --detach "$source_commit"
git -C "$baseline_clone" checkout --quiet --detach "$released_baseline_commit"

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
build_one "$baseline_clone" "$baseline_build"

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
  node scripts/check-publication-eligibility.mjs \
    --build "$build_a" \
    --baseline-build "$baseline_build" \
    --protected-base-commit "$protected_base_commit" \
    --receipt "$eligibility"
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
import sys

path = Path(sys.argv[1])
text = path.read_text()
old = 'Codex and Hermes are tested stdio hosts.'
new = 'Codex, Hermes and ExampleHost are tested stdio hosts.'
if old not in text:
    raise SystemExit('negative control could not find tested-host support claim')
path.write_text(text.replace(old, new, 1))
PY
negative_eligibility="$tmp_root/NEGATIVE_PUBLICATION_ELIGIBILITY.json"
(
  cd "$clone_a/site"
  node scripts/check-publication-eligibility.mjs \
    --build "$negative_build" \
    --baseline-build "$baseline_build" \
    --protected-base-commit "$protected_base_commit" \
    --receipt "$negative_eligibility" >/dev/null
)
[[ $(node -p "require('$negative_eligibility').publicationClass") == RELEASE_BOUND ]] || {
  echo 'HOLD: release-bound negative control was not classified RELEASE_BOUND' >&2
  exit 1
}
[[ $(node -p "require('$negative_eligibility').publicationEligibility") == HOLD_UNRELEASED_CONTRACT ]] || {
  echo 'HOLD: release-bound negative control did not fail closed for publication' >&2
  exit 1
}

cp -a "$build_a" "$negative_llms_build"
printf '\nFactLane v9.9.9 is now universally supported.\n' >> "$negative_llms_build/llms.txt"
negative_llms_eligibility="$tmp_root/NEGATIVE_LLMS_PUBLICATION_ELIGIBILITY.json"
(
  cd "$clone_a/site"
  node scripts/check-publication-eligibility.mjs \
    --build "$negative_llms_build" \
    --baseline-build "$baseline_build" \
    --protected-base-commit "$protected_base_commit" \
    --receipt "$negative_llms_eligibility" >/dev/null
)
[[ $(node -p "require('$negative_llms_eligibility').publicationClass") == RELEASE_BOUND ]] || {
  echo 'HOLD: llms.txt release-bound negative control was not classified RELEASE_BOUND' >&2
  exit 1
}
[[ $(node -p "require('$negative_llms_eligibility').publicationEligibility") == HOLD_UNRELEASED_CONTRACT ]] || {
  echo 'HOLD: llms.txt release-bound negative control did not fail closed for publication' >&2
  exit 1
}

cp -a "$build_a" "$negative_footer_build"
python - "$negative_footer_build/ar/index.html" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text()
pattern = r'(<footer\b[\s\S]*?)FactLane v0\.1\.3([\s\S]*?</footer>)'
updated, count = re.subn(pattern, r'\1FactLane v9.9.9\2', text, count=1, flags=re.I)
if count != 1:
    raise SystemExit('negative control could not find Arabic global footer release claim')
path.write_text(updated)
PY
negative_footer_eligibility="$tmp_root/NEGATIVE_FOOTER_PUBLICATION_ELIGIBILITY.json"
(
  cd "$clone_a/site"
  node scripts/check-publication-eligibility.mjs \
    --build "$negative_footer_build" \
    --baseline-build "$baseline_build" \
    --protected-base-commit "$protected_base_commit" \
    --receipt "$negative_footer_eligibility" >/dev/null
)
[[ $(node -p "require('$negative_footer_eligibility').publicationClass") == RELEASE_BOUND ]] || {
  echo 'HOLD: footer release-bound negative control was not classified RELEASE_BOUND' >&2
  exit 1
}
[[ $(node -p "require('$negative_footer_eligibility').publicationEligibility") == HOLD_UNRELEASED_CONTRACT ]] || {
  echo 'HOLD: footer release-bound negative control did not fail closed for publication' >&2
  exit 1
}

cp -a "$build_a" "$negative_external_href_build"
python - "$negative_external_href_build/docs/RELEASE_OPERATIONS/index.html" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
old = 'https://github.com/Habib1001-m/factlane/releases/tag/v0.1.3'
new = 'https://github.com/Habib1001-m/factlane/releases/tag/v9.9.9'
if old not in text:
    raise SystemExit('negative control could not find released GitHub target')
path.write_text(text.replace(old, new, 1))
PY
negative_external_href_eligibility="$tmp_root/NEGATIVE_EXTERNAL_HREF_PUBLICATION_ELIGIBILITY.json"
(
  cd "$clone_a/site"
  node scripts/check-publication-eligibility.mjs \
    --build "$negative_external_href_build" \
    --baseline-build "$baseline_build" \
    --protected-base-commit "$protected_base_commit" \
    --receipt "$negative_external_href_eligibility" >/dev/null
)
[[ $(node -p "require('$negative_external_href_eligibility').publicationClass") == RELEASE_BOUND ]] || {
  echo 'HOLD: external-href release-bound negative control was not classified RELEASE_BOUND' >&2
  exit 1
}
[[ $(node -p "require('$negative_external_href_eligibility').publicationEligibility") == HOLD_UNRELEASED_CONTRACT ]] || {
  echo 'HOLD: external-href release-bound negative control did not fail closed for publication' >&2
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

pages_port=$(allocate_loopback_port)
setsid bash -c 'cd "$1" && exec env WRANGLER_SEND_METRICS=false npx wrangler pages dev "$2" --ip 127.0.0.1 --port "$3"' \
  _ "$clone_a/site" "$composed_a" "$pages_port" > "$tmp_root/pages-runtime.log" 2>&1 &
server_pid=$!
wait_for_loopback "http://127.0.0.1:$pages_port/" 90 || {
  echo 'HOLD: Wrangler Pages runtime did not become ready within bounded probe window' >&2
  tail -n 80 "$tmp_root/pages-runtime.log" >&2 || true
  exit 1
}
probe_loopback "http://127.0.0.1:$pages_port/"
(
  cd "$clone_a/site"
  FACTLANE_VERIFY_BASE_URL="http://127.0.0.1:$pages_port" \
  FACTLANE_EXPECTED_ORIGIN="$public_origin" \
  FACTLANE_VERIFY_MODE=public \
  node scripts/check-publication-readiness.mjs > "$readiness"
)
stop_server

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
    --expected-package-contents-sha256 0000000000000000000000000000000000000000000000000000000000000000 \
    --expected-origin "$public_origin" \
    --expected-project factlane >/dev/null 2>&1
); then
  echo 'HOLD: wrong package trust-anchor negative control unexpectedly passed' >&2
  exit 1
fi

(
  cd "$clone_a/site"
  node scripts/check-publication-crossing.mjs \
    --package "$package_dir" \
    --expected-package-contents-sha256 "$expected_package_digest" \
    --expected-origin "$public_origin" \
    --expected-project factlane > "$crossing_result"
)

consumer_port=$(allocate_loopback_port)
setsid node "$package_dir/consumer/scripts/serve-publication-static.mjs" \
  --root "$verified_root" \
  --routes "$package_dir/ROUTES.txt" \
  --host 127.0.0.1 \
  --port "$consumer_port" > "$tmp_root/consumer-static-host.log" 2>&1 &
server_pid=$!
wait_for_loopback "http://127.0.0.1:$consumer_port/" 90 || {
  echo 'HOLD: consumer static host did not become ready within bounded probe window' >&2
  tail -n 80 "$tmp_root/consumer-static-host.log" >&2 || true
  exit 1
}
probe_loopback "http://127.0.0.1:$consumer_port/"
FACTLANE_VERIFY_BASE_URL="http://127.0.0.1:$consumer_port" \
FACTLANE_EXPECTED_ORIGIN="$public_origin" \
FACTLANE_VERIFY_MODE=public \
node "$package_dir/consumer/scripts/check-publication-readiness.mjs" > "$consumer_readiness"
stop_server

cp "$eligibility" "$output_root/PUBLICATION_ELIGIBILITY.json"
cp "$receipt_a" "$output_root/OVERLAY_COMPOSITION.json"
cp "$readiness" "$output_root/READINESS_RESULT.json"

cat > "$output_root/QUALIFICATION.env" <<EOF
SOURCE_COMMIT=$source_commit
SOURCE_TREE=$source_tree
PROTECTED_BASE_COMMIT=$protected_base_commit
PUBLIC_ORIGIN=$public_origin
PACKAGE_CONTENTS_SHA256=$expected_package_digest
PUBLICATION_CLASS=$(node -p "require('$eligibility').publicationClass")
PUBLICATION_ELIGIBILITY=$(node -p "require('$eligibility').publicationEligibility")
EOF

echo "PUBLICATION_CI=PASS"
cat "$output_root/QUALIFICATION.env"

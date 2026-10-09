# FactLane site publication readiness

This is an operational runbook for the already accepted site semantics. It is not a new SEO/GEO
cycle, does not authorize publication, and does not change the R19 product/search contract.

## Public-site CI and controlled publication boundary

The protected GitHub `test` context now includes public-site qualification. CI checks Development
correctness and produces an immutable publication-candidate package; it never deploys Cloudflare
Pages, creates or promotes a release, or grants Production authority.

The qualification path is intentionally split into four controls:

1. Two independent clean checkouts of the exact source commit build the public site with the
   committed lockfile, Node `22.22.3`, pinned Wrangler `4.148.0`, the approved HTTPS origin and
   `FACTLANE_PUBLIC_BUILD=1`.
   Complete base manifests must be byte-identical. Source **commit and tree are both provenance**:
   source-aware sitemap `lastmod` uses Git history, so tree equality alone does not identify the
   publication bytes after a squash or history rewrite.
2. `scripts/check-publication-eligibility.mjs` derives the current RELEASED product authority from
   the exact annotated release tag. The publication baseline is **not selected by the candidate
   snapshot**: it is derived from the parent of the historical commit that first introduced the
   release-control file, and bootstrap qualification requires that parent to equal the externally
   supplied protected/base SHA. Later qualifications require that historical introduction to
   already belong to the protected/base history. CI then compares a generated public-claim
   projection for **all canonical routes** against a clean build of that derived baseline. The
   projection includes title, description, all rendered page text (including global chrome/footer),
   JSON-LD and root `llms.txt`, so release/support claims cannot evade classification by moving
   between sections or out of `<main>`. The baseline hashes are derived at qualification time from
   immutable Git history; they are not editable snapshot values. A recognized release-bound
   Development change may keep the required `test` context green while the receipt records
   `HOLD_UNRELEASED_CONTRACT`; unknown/stale release authority is also publication HOLD.
3. `scripts/compose-publication-artifact.mjs` overlays only the manifest-bound hosting/discovery
   inputs under `site/publication/`. Stable host redirects are separated from GSC/Bing/IndexNow
   ownership files. Path collisions, missing files, digest drift, redirect collisions, origin drift,
   or nondeterministic composition fail closed. The verification files are operational publication
   inputs, not product/source authority. The exact composed root is exercised through
   `wrangler pages dev` before freeze so the `_redirects` behavior is tested against the Pages local
   runtime rather than inferred only from a generic static server.
4. `scripts/freeze-publication-package.mjs` requires byte-identical composed artifacts, a PASS
   publication-readiness receipt and exact commit/tree/release/overlay provenance before producing
   the checksummed consumer handoff. The package always records `PRODUCTION_AUTHORIZED=NO`.

The CI entry point is:

```bash
FACTLANE_SITE_URL=https://factlane.pages.dev \
FACTLANE_PUBLIC_BUILD=1 \
FACTLANE_PROTECTED_BASE_COMMIT=<fresh-protected-base-sha> \
npm run check:publication:ci
```

Its `.publication-ci/` output is transportable qualification evidence. GitHub Actions may retain
that directory as a CI artifact, but an Actions artifact is not itself the trust anchor. A future
Production crossing must bind owner authorization to the exact SHA-256 of the candidate package's
`PACKAGE_CONTENTS.sha256`, authenticate that digest with trusted tooling, verify the package, and
consume the frozen archive bytes without rebuilding them.

`scripts/check-publication-crossing.mjs` is a non-deploying package-integrity and eligibility gate.
It authenticates the exact candidate against the expected package digest supplied by the caller,
verifies every package member plus the frozen archive and consumer verifier, and rejects a HOLD
eligibility receipt. The script **does not authenticate owner authorization** and never labels its
own PASS as authorization. A Production crossing still requires a separately authenticated owner
approval bound externally to the exact accepted package digest, followed by fresh live Cloudflare
project/deployment-state checks before Direct Upload. Production rollback is likewise never
automatic and requires explicit or pre-approved incident/runbook authority.

## Reviewed publication shape

The reviewed deployment artifact is the static `site/build/` output at the public origin root.
Production is **not** a Docusaurus/Node application server and the site is not reviewed for hosting
under a URL subpath.

Build the public artifact only after the public origin is approved:

```bash
FACTLANE_SITE_URL=https://<approved-public-origin> \
FACTLANE_PUBLIC_BUILD=1 \
npm run build
```

`FACTLANE_SITE_URL` must be the final HTTPS origin only. A local/default build stays deliberately
non-public: HTML is `noindex,nofollow,noarchive`, `robots.txt` disallows crawling, and sitemaps plus
`llms.txt` are absent.

## Publication crossing preflight: reproducible artifact freeze

Before any separately authorized deployment, bind the crossing to one exact site commit/tree and
prove that the public artifact is reproducible from clean source state. Use two independent detached
worktrees/checkouts of the same commit, the same approved HTTPS origin, the committed lockfile, and
the same Node/npm toolchain. Do not reuse a previous `build/`, `.docusaurus/`, bundler cache, or
publication artifact as an input.

For each isolated source tree, install from the lockfile, clear Docusaurus build/cache state, and
build with the same public environment:

```bash
npm ci --prefer-offline --no-audit --no-fund
npm run clear
FACTLANE_SITE_URL=https://<approved-public-origin> \
FACTLANE_PUBLIC_BUILD=1 \
npm run build -- --out-dir <fresh-public-artifact-dir>
```

Generate a byte manifest from each fresh artifact with paths relative to the artifact root:

```bash
(cd <artifact-a> && find . -type f -print0 | sort -z | xargs -0 sha256sum) > <manifest-a>
(cd <artifact-b> && find . -type f -print0 | sort -z | xargs -0 sha256sum) > <manifest-b>
cmp -s <manifest-a> <manifest-b>
sha256sum <manifest-a> <manifest-b>
```

The reproducibility preflight is **PASS** only when the complete manifests are byte-identical. Any
path, filename, or file-content difference is **HOLD** until its smallest cause is understood and
fixed or the crossing is explicitly abandoned. Do not waive a mismatch merely because canonical,
robots, or rendered text appear equivalent.

Freeze exactly one of the identical artifacts. Record at minimum the commit, tree, approved origin,
Node/npm/Docusaurus versions, `package-lock.json` SHA-256, complete artifact-manifest SHA-256, route
inventory, and the checksums/paths for canonical HTML entry points, `robots.txt`, both sitemaps,
root `llms.txt`, answer pages, publication icons/logo/social images, and generated CSS/JS entry
assets. Reject any artifact containing an absolute development/worktree path or a reference to a
missing local file.

A deterministic archive may be used as the immutable crossing object. Normalize archive metadata
and record its SHA-256, for example with GNU tar plus gzip:

```bash
SOURCE_DATE_EPOCH=$(git show -s --format=%ct <exact-site-commit>)
tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" --owner=0 --group=0 --numeric-owner \
  --format=gnu -C <artifact-dir> -cf - . | gzip -n > <frozen-public-artifact>.tar.gz
sha256sum <frozen-public-artifact>.tar.gz
```

Before deployment, the operator must match the exact commit/tree, public origin, lockfile/toolchain
provenance, complete manifest SHA-256, and frozen archive SHA-256 recorded by the accepted preflight.
If any binding differs, publication is **HOLD** and a new local preflight is required. Deployment
must consume the frozen artifact bytes; do not silently rebuild different bytes during the crossing.

The crossing handoff must also be usable by a consumer that receives the frozen package without the
source checkout or its `node_modules`. Ship checksummed copies of the package verifier, the static
rehearsal host, the publication-readiness harness, and the exact answer-authority snapshot required
by that harness. The handoff package must document their SHA-256 values and the external
`PACKAGE_CONTENTS.sha256` digest bound by the crossing authorization. A self-contained checksum file
inside the package detects corruption but is not an authenticity trust anchor by itself; if the
separately supplied expected package digest is absent or does not match, crossing is **HOLD**.

Consumer rehearsal must start from the received frozen package only. Verify package contents first,
preflight the archive before extraction, and reject symlinks, hardlinks/devices, absolute or `..`
paths, hidden/dev/cache paths, unexpected permissions, missing or extra archive files, critical-file
drift, and any route/provenance mismatch. Extract to a fresh path, then require the extracted regular
file inventory to equal the complete artifact manifest exactly; an extra deploy file is a **HOLD**,
not an ignored local convenience.

The deploy root is the extracted artifact root itself: it must contain `index.html`, `404.html`,
`robots.txt`, `sitemap.xml`, `llms.txt`, and `ar/index.html` directly at their reviewed locations.
Do not deploy a parent directory that merely contains the artifact as a child directory, and do not
serve an arbitrary subdirectory as `/`. The consumer verifier must also reject missing local
HTML/CSS resource targets and absolute development-path references.

Reference consumer commands from the received package directory:

```bash
EXPECTED_PACKAGE_CONTENTS_SHA256=<digest-from-crossing-authorization>
ACTUAL_PACKAGE_CONTENTS_SHA256=$(sha256sum PACKAGE_CONTENTS.sha256 | awk '{print $1}')
test "$ACTUAL_PACKAGE_CONTENTS_SHA256" = "$EXPECTED_PACKAGE_CONTENTS_SHA256" || {
  echo "HOLD: package trust-anchor mismatch" >&2
  exit 1
}
sha256sum -c PACKAGE_CONTENTS.sha256 || {
  echo "HOLD: received package file checksum mismatch" >&2
  exit 1
}

node consumer/scripts/verify-publication-package.mjs \
  --package . \
  --expected-package-contents-sha256 "$EXPECTED_PACKAGE_CONTENTS_SHA256" \
  --extract ./verified-static-root

node consumer/scripts/serve-publication-static.mjs \
  --root ./verified-static-root \
  --routes ./ROUTES.txt \
  --host 127.0.0.1 \
  --port 4260

FACTLANE_VERIFY_BASE_URL=http://127.0.0.1:4260 \
FACTLANE_EXPECTED_ORIGIN=https://<approved-public-origin> \
FACTLANE_VERIFY_MODE=public \
node consumer/scripts/check-publication-readiness.mjs
```

The `sha256sum` bootstrap above must run with a trusted system executable before any script carried
inside the received package is executed. If the handoff arrives as an outer immutable archive, first
compare that archive itself against the separately authorized outer-archive SHA-256 before
extracting it, then perform the authenticated `PACKAGE_CONTENTS.sha256` bootstrap above. A package-
contained verifier is defense in depth after trust is established; it is never the first trust
anchor.

The rehearsal static server is loopback verification tooling, not the production serving stack. It
must fail to start when pointed at the wrong deploy-root nesting and must never add an SPA fallback.
The production host is still governed by the hosting assumptions and post-deploy checks below.

## Hosting assumptions that must remain true

The host/CDN must preserve the built artifact rather than reinterpret it as an SPA:

- Serve the static build at `/`; do not mount it below another path.
- Serve canonical directory routes such as `/docs/TOOLS/`, `/answers/`, and `/ar/answers/` as real
  HTML with HTTP 200. Slashless aliases may redirect to the slash form or return the same content,
  but their HTML canonical must point to the trailing-slash route.
- Preserve the accepted static alias boundary for generated files: `.html` aliases, explicit
  directory `index.html` aliases, duplicate-slash variants, and query-string variants may resolve
  directly or through at most the accepted redirect bound, but must end on HTTP 200 content whose
  canonical is the trailing-slash route. Directory-index serving must not create a second canonical
  URL or bypass the 404/case boundary.
- Unknown routes, default-locale `/en/` routes, and wrong-case route variants must return **HTTP
  404**, not a 200 SPA fallback. The rendered 404 must retain `noindex,nofollow,noarchive`.
- Do not configure a catch-all rewrite to `/index.html`. That would destroy the accepted 404 and
  wrong-case boundary.
- Keep path matching case-sensitive. Do not normalize `CORE_CONCEPTS`/`QUICKSTART`-style route
  segments into a different case.
- Do not override, synthesize, localize, or redirect the built `robots.txt`, `sitemap.xml`,
  `/ar/sitemap.xml`, or root `/llms.txt`. `/ar/llms.txt` must stay absent.
- Do not add CDN/edge rules that rewrite canonical or hreflang URLs, inject a different robots meta,
  inject a restrictive public `X-Robots-Tag`, strip JSON-LD, or replace SSR HTML with client-only
  output.
- Preserve the final HTTPS public origin. Hostname redirects, if any, must terminate before the
  configured canonical origin is used for verification.
- Compression and caching are fine if response bytes remain semantically identical. After a real
  deployment, stale edge caches must not mix old and new HTML/discovery files.

These are hosting assumptions, not ranking requirements.

## Optional HTTP verification harness

`scripts/check-publication-readiness.mjs` is deliberately **not** wired into `npm run build` or the
build-fatal SEO guard. It performs live HTTP checks and therefore belongs to publication
verification, not deterministic build acceptance.

Local rehearsal against a served public build:

```bash
FACTLANE_VERIFY_BASE_URL=http://127.0.0.1:4220 \
FACTLANE_EXPECTED_ORIGIN=https://<the-origin-used-to-build-the-artifact> \
FACTLANE_VERIFY_MODE=public \
npm run check:publication:readiness
```

After an explicitly authorized production deployment:

```bash
FACTLANE_VERIFY_BASE_URL=https://<approved-public-origin> \
FACTLANE_EXPECTED_ORIGIN=https://<approved-public-origin> \
FACTLANE_VERIFY_MODE=public \
npm run check:publication:readiness
```

For a deliberately non-public preview built in local mode, set `FACTLANE_VERIFY_MODE=local` and
bind `FACTLANE_EXPECTED_ORIGIN` to the canonical origin used for that build.

The harness checks:

- canonical route HTTP 200/status and redirect behavior;
- trailing-slash, `.html`, explicit `index.html`, duplicate-slash, and query variants, including the
  accepted redirect bound and canonical convergence;
- wrong-case, missing, and `/en/` routes as real HTTP 404 + noindex;
- canonical, hreflang, locale direction, H1 and SSR `<main>` presence;
- the SSR navbar product mark, primary favicon, Apple touch icon, Open Graph/Twitter image metadata,
  and the publication-facing logo/favicon/social files themselves, including expected media type,
  PNG dimensions and EN/AR byte parity;
- absence of a public `X-Robots-Tag` that would impose `noindex`, `nofollow`, or `none`;
- landing/docs/answers JSON-LD boundaries;
- all nine EN/AR answer sections and their canonical source links;
- public/local `robots.txt`, sitemaps, `lastmod` integrity, root-only `llms.txt` behavior;
- raw-HTML/no-JS SSR parity across browser, Googlebot, Bingbot, OAI-SearchBot,
  Claude-SearchBot, and Claude-User.

The crawler set is intentionally bounded to identities already accepted from official guidance in
R19. No xAI-specific crawler identity is guessed. A future official crawler-policy change is a
reason to review the harness, not to silently add a user agent.

The harness does **not** test rankings, citation outcomes, search-engine response time, or whether
an external engine has indexed the site. Network availability is never a build gate.

## Publication crossing: immediate checks

Once publication itself is separately authorized and the final public origin exists:

1. Bind the deployment to the exact approved site commit/tree and build with the final HTTPS origin
   plus `FACTLANE_PUBLIC_BUILD=1`.
2. Confirm the deployed artifact is static and the host does not apply an SPA fallback.
3. Run the publication-readiness harness against the production origin.
4. Manually spot-check `/`, `/ar/`, `/answers/`, `/ar/answers/`, one EN doc, one AR doc, a
   slashless/`index.html`/duplicate-slash alias, a wrong-case URL, and a definitely missing URL.
5. In EN and AR, inspect desktop and narrow/mobile layouts in light and dark modes: confirm the
   FactLane mark is loaded (never a broken/default icon), headings and paragraphs reflow without
   clipping or horizontal overflow, section spacing remains intentional, and primary actions remain
   visually prominent.
6. Confirm the favicon and Apple touch icon load directly and the social-preview URL resolves to the
   expected 1280×640 PNG. Inspect a share-preview debugger only after publication when one is
   available; do not treat external cache refresh as a build gate.
7. Confirm production `robots.txt`, `/sitemap.xml`, `/ar/sitemap.xml`, root `/llms.txt`, and absent
   `/ar/llms.txt` match the expected deployment mode.
8. Confirm the answer pages expose the direct SSR answers and source links before hydration.
9. If canonical/robots/404/crawler parity or publication-facing brand assets are broken, treat
   publication as **HOLD** and fix or roll
   back the smallest hosting/deployment defect before any search-vendor submission.

## Evidence that comes later, not at build time

After the public domain exists and separate authority permits the integrations:

- **Google Search Console:** verify ownership, submit/inspect the sitemap, review indexation,
  selected canonical, crawl/URL Inspection evidence, and real query/impression/snippet data.
- **Bing Webmaster Tools:** equivalent sitemap/indexation/crawl evidence.
- **Server/CDN logs:** verify real crawler access and identify unexpected 403/404/429/5xx behavior
  for documented crawler identities. Logs are evidence; they are not a requirement to build.
- **Field/RUM evidence:** use representative field data for performance questions, including the
  carried R17 cold-LCP uncertainty. Do not convert synthetic wall-clock timings into build gates.
- **Real discovery evidence:** actual queries, referrals, snippets, and attributable citations may
  reveal missing answer coverage. Absence immediately after launch is not itself a defect.

## The only triggers for an R20 SEO/GEO cycle

Open R20 only when evidence shows at least one of these:

1. indexation, selected-canonical, crawl, or snippet failures;
2. an official crawler/discovery guidance change that affects the accepted policy;
3. a material IA, route, schema, robots, sitemap, `llms.txt`, or answer-surface change;
4. real query/citation/referral data exposes a specific answer or authority gap.

Do **not** open R20 merely for another optimization pass, a third-party GEO score, speculative
`llms.txt` advice, more keywords, lack of immediate rankings, or cosmetic content churn.

## Authority boundary

This runbook and harness prepare verification only. They do not authorize push, PR, merge, deploy,
DNS/domain changes, repository settings, social-preview changes, search-vendor setup/submission,
analytics, tags, or releases.

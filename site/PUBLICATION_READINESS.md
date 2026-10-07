# FactLane site publication readiness

This is an operational runbook for the already accepted site semantics. It is not a new SEO/GEO
cycle, does not authorize publication, and does not change the R19 product/search contract.

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

## Hosting assumptions that must remain true

The host/CDN must preserve the built artifact rather than reinterpret it as an SPA:

- Serve the static build at `/`; do not mount it below another path.
- Serve canonical directory routes such as `/docs/TOOLS/`, `/answers/`, and `/ar/answers/` as real
  HTML with HTTP 200. Slashless aliases may redirect to the slash form or return the same content,
  but their HTML canonical must point to the trailing-slash route.
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
- trailing-slash aliases and query variants;
- wrong-case, missing, and `/en/` routes as real HTTP 404 + noindex;
- canonical, hreflang, locale direction, H1 and SSR `<main>` presence;
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
   wrong-case URL, and a definitely missing URL.
5. Confirm production `robots.txt`, `/sitemap.xml`, `/ar/sitemap.xml`, root `/llms.txt`, and absent
   `/ar/llms.txt` match the expected deployment mode.
6. Confirm the answer pages expose the direct SSR answers and source links before hydration.
7. If canonical/robots/404/crawler parity is broken, treat publication as **HOLD** and fix or roll
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

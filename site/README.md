# FactLane site workspace

This directory contains the Docusaurus product/docs site. It is intentionally separate from the FactLane runtime package.

## Local development

```bash
npm ci
npm run start
```

English remains the default locale at `/`. To develop the Arabic locale directly:

```bash
npm run start:ar
```

The Arabic site is served under `/ar/` and uses native Docusaurus RTL mode. English remains LTR.
Locale switching is handled by Docusaurus; do not hardcode `/ar/` into authored navigation links.

The pre-start/pre-build hook generates two ignored inputs:

- `.generated-docs/` from canonical repository docs, `SECURITY.md`, and the using-factlane Skill;
- `.generated-static/` from the small subset of canonical brand assets the site actually uses.

Do not hand-edit either generated directory. The tracked repository files remain the source of truth.

## Validation

```bash
npm run check
```

This runs TypeScript checking followed by a production Docusaurus build for both configured locales.
Broken links and broken anchors fail the build. Locale-specific build helpers are also available:

```bash
npm run build:en
npm run build:ar
```

Arabic documentation translations live under
`i18n/ar/docusaurus-plugin-content-docs/current/`. They are localized derivatives of the canonical
English repository docs; the English sources remain authoritative for technical truth. Commands,
paths, tool names, versions and other exact identifiers must remain unchanged inside translations.

### Dependency-audit boundary

The current Docusaurus 3.10.2 dependency graph reports upstream `npm audit` findings through
build/tooling transitives (including glob/dev-server/serialization paths). npm still reports those
paths with `--omit=dev` because the Docusaurus packages are site dependencies, even though the
intended deployed artifact is static HTML/CSS/JS rather than a Node/Docusaurus application server.
That deployment shape reduces runtime exposure; it does **not** erase build-time or supply-chain
risk. The current graph has no direct non-breaking audit fix for the Docusaurus core/preset chain
that carries those advisories.

The intended published artifact is the generated static `build/` output; it does not run a
Docusaurus/Node application server in production. Re-run the dependency audit before any public
deployment and treat future fixed upstream versions as a separate dependency-upgrade slice rather
than silently changing the site toolchain.

## Canonical URL / publication safety

Local builds are non-public by default and emit a `noindex,nofollow,noarchive` robots directive.
The sitemap is disabled and generated `robots.txt` disallows crawling. Supplying a canonical URL
alone does **not** remove those guards.

A future public build must provide both the approved canonical origin and an explicit publication
flag:

```bash
FACTLANE_SITE_URL=https://<approved-public-host> FACTLANE_PUBLIC_BUILD=1 npm run build
```

`FACTLANE_SITE_URL` must be an HTTPS origin only: no credentials, path, query string, or fragment.
`FACTLANE_PUBLIC_BUILD` accepts only `0` or `1`. Public mode refuses IP literals, local/reserved
hostnames and the placeholder origin. It also fails closed unless the complete required Arabic
documentation set and Arabic UI translation files are present. When enabled with a syntactically
public DNS hostname, generated `robots.txt` allows crawling and advertises both the English
`/sitemap.xml` and Arabic `/ar/sitemap.xml`. Passing this build gate does not prove domain ownership
or authorize deployment.

The English locale uses `baseUrl=/`; Arabic uses the explicit locale base URL `/ar/`. Hosting the
whole site below another URL subpath remains outside the reviewed publication profile and needs a
separate configuration change.

Do not substitute a real public URL until hosting/domain ownership has been approved separately.

## Public boundary

Building this directory does not authorize a branch push, pull request, deployment, DNS change, repository setting change, analytics integration, or search-vendor integration.

import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const siteRoot = path.resolve(here, '..');
const repoRoot = path.resolve(siteRoot, '..');
const publicDocs = JSON.parse(
  await readFile(path.join(siteRoot, 'public-docs.json'), 'utf8'),
);

const evergreen = [
  'README.md',
  'site/README.md',
  'site/PUBLICATION_READINESS.md',
  'docs/assets/brand/readme/factlane-readme-hero-light.svg',
  'docs/assets/brand/readme/factlane-readme-hero-dark.svg',
  'docs/assets/brand/readme/factlane-readme-hero-mobile-light.svg',
  'docs/assets/brand/readme/factlane-readme-hero-mobile-dark.svg',
  'docs/assets/brand/diagrams/factlane-memory-lifecycle.svg',
  'docs/assets/brand/diagrams/factlane-memory-lifecycle-mobile.svg',
  'skills/using-factlane/references/host-bootstrap.md',
  'site/src/content/homeCopy.ts',
  'site/src/content/answerAuthority.json',
  'site/src/pages/index.tsx',
  'site/docusaurus.config.ts',
  'site/plugins/seo-regression-guard.mjs',
  'site/i18n/ar/code.json',
  'site/i18n/ar/docusaurus-theme-classic/navbar.json',
  'site/i18n/ar/docusaurus-theme-classic/footer.json',
  ...publicDocs.filter((doc) => !doc.releaseBound).map((doc) => doc.source),
  ...publicDocs
    .filter((doc) => !doc.releaseBound)
    .map((doc) => 'site/i18n/ar/docusaurus-plugin-content-docs/current/' + doc.id + '.md'),
];

const semanticVersionPattern = /(?<![\d.])[vV]?\d+\.\d+\.\d+(?![\d.])/g;
const technicalVersionContext =
  /\b(?:Python|SQLite|Docusaurus|React|TypeScript|Node(?:\.js)?|npm|Wrangler)\b([^\n]{0,56})$/i;

function factLaneReleaseReferences(text) {
  const matches = [];
  for (const match of text.matchAll(semanticVersionPattern)) {
    const index = match.index ?? 0;
    const token = match[0];
    const before = text.slice(Math.max(0, index - 96), index);
    const after = text.slice(index + token.length, index + token.length + 48);
    const immediateBefore = before.slice(-80);
    const technicalTail = immediateBefore.match(technicalVersionContext)?.[1];
    if (
      technicalTail !== undefined &&
      !/FactLane|\brelease\b|\breleased\b|\btag\b|\bpackage\s+version\b/i.test(technicalTail)
    ) {
      continue;
    }
    const nearby = before + token + after;
    const factLaneContext = /FactLane|factlane[-_.=<>!~@]/i.test(nearby);
    const releaseContext =
      /\b(?:release|released|published|tag|package version|source\/RC|RC line|release candidate)\b/i.test(
        before.slice(-72),
      );
    const versionTag = /^[vV]/.test(token);
    if (factLaneContext || releaseContext || versionTag) {
      matches.push(token);
    }
  }
  return matches;
}

function assertNoFactLaneReleaseReferences(relative, text) {
  const matches = factLaneReleaseReferences(text);
  if (matches.length) {
    throw new Error(
      'Evergreen content embeds a FactLane release number: ' +
        relative +
        ' (' +
        matches.slice(0, 5).join(', ') +
        '). Move release-specific truth to CHANGELOG / RELEASE_OPERATIONS / PROJECT_HISTORY.',
    );
  }
}

function section(text, startHeading, endHeading) {
  const start = text.indexOf(startHeading);
  const end = text.indexOf(endHeading, start + startHeading.length);
  if (start === -1 || end === -1 || end <= start) {
    throw new Error(`Content-policy section boundary missing: ${startHeading} -> ${endHeading}`);
  }
  return text.slice(start, end);
}

for (const relative of evergreen) {
  const text = await readFile(path.join(repoRoot, relative), 'utf8');
  assertNoFactLaneReleaseReferences(relative, text);
}

const docSeo = JSON.parse(
  await readFile(path.join(siteRoot, 'src', 'content', 'docSeo.json'), 'utf8'),
);
const hostRedirects = (
  await readFile(path.join(siteRoot, 'publication', 'host', 'redirects.txt'), 'utf8')
).split(/\r?\n/);

for (const doc of publicDocs) {
  for (const localePrefix of ['', '/ar']) {
    const route = doc.id === 'INTRO' ? `${localePrefix}/docs/` : `${localePrefix}/docs/${doc.id}/`;
    const alias = route === `${localePrefix}/docs/`
      ? `${localePrefix}/docs.html`
      : `${route.slice(0, -1)}.html`;
    const expected = `${alias} ${route} 301`;
    if (!hostRedirects.includes(expected)) {
      throw new Error(`Missing stable HTML alias redirect for public doc ${doc.id}: ${expected}`);
    }
  }
}

for (const locale of ['en', 'ar']) {
  for (const doc of publicDocs.filter((entry) => !entry.releaseBound)) {
    const seo = docSeo[locale]?.[doc.id];
    if (!seo) {
      throw new Error('Missing evergreen SEO entry: ' + locale + '/' + doc.id);
    }
    const text = JSON.stringify(seo);
    const matches = factLaneReleaseReferences(text);
    if (matches.length) {
      throw new Error(
        'Evergreen SEO embeds a FactLane release number: ' +
          locale +
          '/' +
          doc.id +
          ' (' +
          matches.join(', ') +
          ')',
      );
    }
  }
}

for (const relative of publicDocs.filter((doc) => doc.releaseBound).map((doc) => doc.source)) {
  const text = await readFile(path.join(repoRoot, relative), 'utf8');
  const found = [...text.matchAll(semanticVersionPattern)].length > 0;
  if (!found) {
    throw new Error('Release-bound surface has no release identity: ' + relative);
  }
}

const releaseOperations = await readFile(
  path.join(repoRoot, 'docs', 'RELEASE_OPERATIONS.md'),
  'utf8',
);
for (const [label, value] of [
  [
    'docs/RELEASE_OPERATIONS.md install procedure',
    section(releaseOperations, '## Install an exact release', '## Upgrade procedure for later releases'),
  ],
  [
    'docs/RELEASE_OPERATIONS.md reverification procedure',
    section(releaseOperations, '## Reverification after install or upgrade', '## Rollback procedure'),
  ],
]) {
  assertNoFactLaneReleaseReferences(label, value);
}

const arabicReleaseOperations = await readFile(
  path.join(
    siteRoot,
    'i18n',
    'ar',
    'docusaurus-plugin-content-docs',
    'current',
    'RELEASE_OPERATIONS.md',
  ),
  'utf8',
);
for (const [label, value] of [
  [
    'Arabic RELEASE_OPERATIONS install procedure',
    section(arabicReleaseOperations, '## تثبيت إصدار دقيق', '## إجراء الترقية للإصدارات اللاحقة'),
  ],
  [
    'Arabic RELEASE_OPERATIONS reverification procedure',
    section(arabicReleaseOperations, '## إعادة التحقق بعد التثبيت أو الترقية', '## إجراء التراجع'),
  ],
]) {
  assertNoFactLaneReleaseReferences(label, value);
}

const landingSource = await readFile(path.join(siteRoot, 'src', 'pages', 'index.tsx'), 'utf8');
for (const renderedSection of [
  '<QuickStart copy={copy} />',
  '<Boundaries copy={copy} />',
  '<Tools copy={copy} />',
  '<Architecture copy={copy} />',
]) {
  if (!landingSource.includes(renderedSection)) {
    throw new Error('Homepage no longer renders required product section: ' + renderedSection);
  }
}

const homeCopy = await readFile(path.join(siteRoot, 'src', 'content', 'homeCopy.ts'), 'utf8');
for (const capability of [
  'OwnerControlService',
  'factlane-owner',
  'Owner WebUI',
  'REVOKE',
  'REJECT',
  'recheck',
  'readmit',
]) {
  const occurrences = homeCopy.split(capability).length - 1;
  if (occurrences < 2) {
    throw new Error(
      'Homepage bilingual capability coverage drifted: ' +
        capability +
        ' occurs only ' +
        occurrences +
        ' time(s)',
    );
  }
}

console.log('Evergreen/release-bound content policy passed');

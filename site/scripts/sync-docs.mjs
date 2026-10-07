import {access, cp, mkdir, readdir, readFile, rm, writeFile} from 'node:fs/promises';
import {isIP} from 'node:net';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const siteRoot = path.resolve(here, '..');
const repoRoot = path.resolve(siteRoot, '..');
const sourceDocs = path.join(repoRoot, 'docs');
const output = path.join(siteRoot, '.generated-docs');
const staticOutput = path.join(siteRoot, '.generated-static');
const arabicDocsRoot = path.join(
  siteRoot,
  'i18n',
  'ar',
  'docusaurus-plugin-content-docs',
  'current',
);
const configuredSiteUrl = process.env.FACTLANE_SITE_URL ?? 'https://factlane.local';
const parsedSiteUrl = new URL(configuredSiteUrl);
const publicBuildFlag = process.env.FACTLANE_PUBLIC_BUILD ?? '0';

if (
  parsedSiteUrl.protocol !== 'https:' ||
  parsedSiteUrl.username ||
  parsedSiteUrl.password ||
  parsedSiteUrl.pathname !== '/' ||
  parsedSiteUrl.search ||
  parsedSiteUrl.hash
) {
  throw new Error(
    'FACTLANE_SITE_URL must be an HTTPS origin with no credentials, path, query, or fragment',
  );
}

if (!['0', '1'].includes(publicBuildFlag)) {
  throw new Error('FACTLANE_PUBLIC_BUILD must be exactly 0 or 1');
}

const siteUrl = parsedSiteUrl.origin;
const publicBuild = publicBuildFlag === '1';

const publicHostname = parsedSiteUrl.hostname.toLowerCase();
const reservedPublicHostname =
  publicHostname.endsWith('.') ||
  isIP(publicHostname) !== 0 ||
  !publicHostname.includes('.') ||
  publicHostname === 'localhost' ||
  publicHostname.endsWith('.localhost') ||
  publicHostname.endsWith('.local') ||
  publicHostname.endsWith('.test') ||
  publicHostname.endsWith('.invalid') ||
  publicHostname.endsWith('.example') ||
  ['example.com', 'example.net', 'example.org'].some(
    (domain) => publicHostname === domain || publicHostname.endsWith('.' + domain),
  );

if (publicBuild && reservedPublicHostname) {
  throw new Error(
    'FACTLANE_PUBLIC_BUILD=1 requires a non-local, non-reserved DNS hostname approved for publication',
  );
}

const requiredArabicDocs = [
  'ARCHITECTURE.md',
  'CORE_CONCEPTS.md',
  'ENVIRONMENT.md',
  'FAQ.md',
  'INTRO.md',
  'PROJECT_HISTORY.md',
  'QUICKSTART.md',
  'RELEASE_OPERATIONS.md',
  'SECURITY.md',
  'TOOLS.md',
  'USE_CASES.md',
  'USING_FACTLANE_SKILL.md',
];

const requiredArabicUiTranslations = [
  path.join(siteRoot, 'i18n', 'ar', 'code.json'),
  path.join(siteRoot, 'i18n', 'ar', 'docusaurus-theme-classic', 'navbar.json'),
  path.join(siteRoot, 'i18n', 'ar', 'docusaurus-theme-classic', 'footer.json'),
  path.join(siteRoot, 'i18n', 'ar', 'docusaurus-plugin-content-docs', 'current.json'),
];

if (publicBuild) {
  for (const fileName of requiredArabicDocs) {
    try {
      await access(path.join(arabicDocsRoot, fileName));
    } catch {
      throw new Error(
        `FACTLANE_PUBLIC_BUILD=1 requires the complete Arabic docs set; missing ${fileName}`,
      );
    }
  }
  for (const translationPath of requiredArabicUiTranslations) {
    try {
      await access(translationPath);
    } catch {
      throw new Error(
        `FACTLANE_PUBLIC_BUILD=1 requires Arabic UI translations; missing ${path.relative(
          siteRoot,
          translationPath,
        )}`,
      );
    }
  }
}

const replacements = [
  ['](../README.md)', '](/)'],
  ['](../SECURITY.md)', '](SECURITY.md)'],
  ['](../skills/using-factlane/SKILL.md)', '](USING_FACTLANE_SKILL.md)'],
  ['](docs/ARCHITECTURE.md)', '](ARCHITECTURE.md)'],
  ['](docs/ENVIRONMENT.md)', '](ENVIRONMENT.md)'],
  ['](docs/RELEASE_OPERATIONS.md)', '](RELEASE_OPERATIONS.md)'],
];

function adaptLinks(source) {
  return replacements.reduce(
    (text, [from, to]) => text.split(from).join(to),
    source,
  );
}

await rm(output, {recursive: true, force: true});
await mkdir(output, {recursive: true});
await rm(staticOutput, {recursive: true, force: true});
await mkdir(staticOutput, {recursive: true});

for (const entry of await readdir(sourceDocs, {withFileTypes: true})) {
  if (!entry.isFile() || !entry.name.endsWith('.md')) {
    continue;
  }
  const src = path.join(sourceDocs, entry.name);
  const dst = path.join(output, entry.name);
  const text = await readFile(src, 'utf8');
  await writeFile(dst, adaptLinks(text));
}

for (const [sourceRelative, targetName] of [
  ['SECURITY.md', 'SECURITY.md'],
  ['skills/using-factlane/SKILL.md', 'USING_FACTLANE_SKILL.md'],
]) {
  const source = path.join(repoRoot, sourceRelative);
  const target = path.join(output, targetName);
  const text = await readFile(source, 'utf8');
  await writeFile(target, adaptLinks(text));
}

await cp(
  path.join(repoRoot, 'docs', 'assets', 'brand', 'diagrams', 'factlane-memory-lifecycle.svg'),
  path.join(output, 'factlane-memory-lifecycle.svg'),
);

for (const assetRelative of [
  'favicon/favicon-32.png',
  'logo/factlane-mark.svg',
  'social/factlane-github-social-preview.png',
]) {
  const source = path.join(repoRoot, 'docs', 'assets', 'brand', assetRelative);
  const target = path.join(staticOutput, assetRelative);
  await mkdir(path.dirname(target), {recursive: true});
  await cp(source, target);
}

await writeFile(
  path.join(staticOutput, 'robots.txt'),
  publicBuild
    ? `User-agent: OAI-SearchBot\nAllow: /\n\nUser-agent: *\nAllow: /\nSitemap: ${siteUrl}/sitemap.xml\nSitemap: ${siteUrl}/ar/sitemap.xml\n`
    : 'User-agent: *\nDisallow: /\n',
);

if (publicBuild) {
  await writeFile(
    path.join(staticOutput, 'llms.txt'),
    `# FactLane

> FactLane is a free, local-first governed MCP memory layer for compatible AI agents. It keeps bounded reusable facts with scope, provenance, freshness, and trusted Candidate → Current verification.

FactLane is not a universal memory replacement or a general agent-safety system. Current user instructions, live project state, and verified live sources outrank remembered facts. The supported named release is v0.1.3 over command-launched stdio with a compatible MCP host.

## Start here

- [FactLane overview](${siteUrl}/): What FactLane is, when it fits, and the agent-assisted start path.
- [Core concepts](${siteUrl}/docs/CORE_CONCEPTS/): Scope, provenance, freshness, Candidate, Current, and authority boundaries.
- [Use cases](${siteUrl}/docs/USE_CASES/): The small reusable facts that belong in the governed lane.
- [Quick Start](${siteUrl}/docs/QUICKSTART/): Install the supported v0.1.3 local profile and connect a compatible stdio MCP host.

## Product contract

- [Tools](${siteUrl}/docs/TOOLS/): The exact five-tool public MCP surface and its semantics.
- [Architecture](${siteUrl}/docs/ARCHITECTURE/): Storage, lifecycle, host, and trust-boundary architecture.
- [Environment](${siteUrl}/docs/ENVIRONMENT/): Supported runtime and local Ollama embedding profile.
- [Security](${siteUrl}/docs/SECURITY/): Read-only defaults, write authority, and trust boundaries.
- [Source repository](https://github.com/Habib1001-m/factlane): Apache-2.0 source for FactLane.

## Arabic

- [FactLane بالعربية](${siteUrl}/ar/): نظرة عامة ومسار البدء مع الوكيل.
- [المفاهيم الأساسية](${siteUrl}/ar/docs/CORE_CONCEPTS/): النطاق والمصدر والحداثة ودورة Candidate → Current وحدود الصلاحيات.
- [حالات الاستخدام](${siteUrl}/ar/docs/USE_CASES/): الحقائق الصغيرة التي تناسب المسار المحكوم.
- [الإعداد السريع](${siteUrl}/ar/docs/QUICKSTART/): إعداد الإصدار v0.1.3 ضمن ملف التشغيل المحلي المدعوم.

## Optional

- [Project history](${siteUrl}/docs/PROJECT_HISTORY/): Project provenance and release history.
- [Release operations](${siteUrl}/docs/RELEASE_OPERATIONS/): Documented release and operational procedures.
`,
  );
}

console.log(
  'Synced canonical FactLane docs and selected brand assets into generated site inputs',
);

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
    ? `User-agent: OAI-SearchBot\nAllow: /\n\nUser-agent: Claude-SearchBot\nAllow: /\n\nUser-agent: Claude-User\nAllow: /\n\nUser-agent: *\nAllow: /\nSitemap: ${siteUrl}/sitemap.xml\nSitemap: ${siteUrl}/ar/sitemap.xml\n`
    : 'User-agent: *\nDisallow: /\n',
);

// llms.txt is emitted once at the public root by the post-build SEO guard.
// Keeping it out of the localized static directory prevents Docusaurus from
// copying an unintended duplicate to /ar/llms.txt.
await rm(path.join(staticOutput, 'llms.txt'), {force: true});

console.log(
  'Synced canonical FactLane docs and selected brand assets into generated site inputs',
);

import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

import {
  assert,
  git,
  parseArgs,
  readJson,
  sha256File,
  sha256Text,
  writeJson,
} from './publication-control-lib.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const siteRoot = path.resolve(here, '..');
const repoRoot = path.resolve(siteRoot, '..');
const args = parseArgs(process.argv.slice(2));
const buildRoot = path.resolve(args.build ?? '');
const baselineBuildRoot = path.resolve(args['baseline-build'] ?? '');
const protectedBaseCommit = args['protected-base-commit'] ?? '';
const receiptPath = path.resolve(args.receipt ?? path.join(buildRoot, '..', 'PUBLICATION_ELIGIBILITY.json'));
const snapshotPath = path.resolve(
  args.snapshot ?? path.join(siteRoot, 'publication', 'released-contract-v0.1.4.json'),
);

assert(args.build, '--build is required');
assert(args['baseline-build'], '--baseline-build is required');
assert(/^[0-9a-f]{40}$/.test(protectedBaseCommit), '--protected-base-commit must be an exact lowercase Git SHA');
const snapshot = await readJson(snapshotPath);
assert(snapshot.schemaVersion === 1, `Unsupported released-contract schema: ${snapshot.schemaVersion}`);
const authority = snapshot.releaseAuthority;
const baseline = snapshot.releasedPublicationBaseline;
const releasedSiteBaseline = snapshot.releasedSiteBaseline ?? null;
assert(authority && typeof authority === 'object', 'Released-contract snapshot is missing releaseAuthority');
assert(baseline && typeof baseline === 'object', 'Released-contract snapshot is missing releasedPublicationBaseline');

const snapshotRepoRelative = path.relative(repoRoot, snapshotPath).split(path.sep).join('/');
assert(
  snapshotRepoRelative && !snapshotRepoRelative.startsWith('../') && !path.isAbsolute(snapshotRepoRelative),
  'Released-contract snapshot must live inside the repository',
);
const introductionCommits = git(repoRoot, [
  'log', '--diff-filter=A', '--format=%H', '--reverse', '--', snapshotRepoRelative,
]).split(/\r?\n/).filter(Boolean);
assert(introductionCommits.length === 1, `Expected one historical introduction for ${snapshotRepoRelative}`);
const controlIntroductionCommit = introductionCommits[0];
const legacyDerivedAcceptedMainCommit = git(repoRoot, ['rev-parse', `${controlIntroductionCommit}^`]);
const derivedAcceptedMainCommit = releasedSiteBaseline?.sourceCommit ?? legacyDerivedAcceptedMainCommit;
const derivedAcceptedMainTree = releasedSiteBaseline?.sourceTree
  ?? git(repoRoot, ['rev-parse', `${derivedAcceptedMainCommit}^{tree}`]);
if (releasedSiteBaseline) {
  assert(/^[0-9a-f]{40}$/.test(releasedSiteBaseline.sourceCommit ?? ''), 'Released-site baseline commit is invalid');
  assert(/^[0-9a-f]{40}$/.test(releasedSiteBaseline.sourceTree ?? ''), 'Released-site baseline tree is invalid');
  assert(
    releasedSiteBaseline.sourceCommit === authority.commit,
    'Released-site baseline commit must equal released product authority commit',
  );
  assert(
    releasedSiteBaseline.sourceTree === authority.tree,
    'Released-site baseline tree must equal released product authority tree',
  );
  assert(
    git(repoRoot, ['rev-parse', `${releasedSiteBaseline.sourceCommit}^{tree}`]) === releasedSiteBaseline.sourceTree,
    'Released-site baseline commit/tree binding is invalid',
  );
}
assert(
  git(repoRoot, ['rev-parse', `${protectedBaseCommit}^{commit}`]) === protectedBaseCommit,
  'Protected/base authority commit is unavailable in the checkout',
);
assert(
  git(repoRoot, ['merge-base', protectedBaseCommit, 'HEAD']) === protectedBaseCommit,
  'Protected/base authority is not an ancestor of the candidate',
);

let snapshotExistedAtProtectedBase = true;
try {
  git(repoRoot, ['show', `${protectedBaseCommit}:${snapshotRepoRelative}`]);
} catch {
  snapshotExistedAtProtectedBase = false;
}
if (!snapshotExistedAtProtectedBase) {
  assert(
    derivedAcceptedMainCommit === protectedBaseCommit,
    'Bootstrap publication baseline must equal the externally supplied protected/base commit',
  );
} else {
  assert(
    git(repoRoot, ['merge-base', controlIntroductionCommit, protectedBaseCommit]) === controlIntroductionCommit,
    'Historical publication-control introduction is not part of the protected/base authority',
  );
}

const tagRef = `refs/tags/${authority.tag}`;
assert(git(repoRoot, ['cat-file', '-t', tagRef]) === 'tag', `Released authority tag is not annotated: ${authority.tag}`);
assert(git(repoRoot, ['rev-parse', tagRef]) === authority.tagObject, `Released tag object drift: ${authority.tag}`);
assert(git(repoRoot, ['rev-parse', `${authority.tag}^{commit}`]) === authority.commit, `Released commit drift: ${authority.tag}`);
assert(git(repoRoot, ['rev-parse', `${authority.tag}^{tree}`]) === authority.tree, `Released tree drift: ${authority.tag}`);
if (!releasedSiteBaseline) {
  assert(
    derivedAcceptedMainTree === baseline.productionSourceTree,
    'Legacy accepted main baseline and Production source tree must remain identical',
  );
}
assert(
  git(repoRoot, ['merge-base', authority.commit, derivedAcceptedMainCommit]) === authority.commit,
  'Accepted publication baseline is not descended from the released product authority',
);
assert(
  git(repoRoot, ['merge-base', derivedAcceptedMainCommit, 'HEAD']) === derivedAcceptedMainCommit,
  'Current source is not descended from the accepted publication baseline',
);

const releasedPyproject = git(repoRoot, ['show', `${authority.tag}:pyproject.toml`]);
const releasedContract = git(repoRoot, ['show', `${authority.tag}:src/factlane/contract.py`]);
const releasedServer = git(repoRoot, ['show', `${authority.tag}:src/factlane/server.py`]);
const releasedEnvironment = git(repoRoot, ['show', `${authority.tag}:docs/ENVIRONMENT.md`]);
const currentPyproject = await readFile(path.join(repoRoot, 'pyproject.toml'), 'utf8');

const releasedVersion = releasedPyproject.match(/^version\s*=\s*"([^"]+)"/m)?.[1];
const currentPackageVersion = currentPyproject.match(/^version\s*=\s*"([^"]+)"/m)?.[1];
const releasedPython = releasedPyproject.match(/^requires-python\s*=\s*"([^"]+)"/m)?.[1];
const releasedRevision = Number(releasedContract.match(/^PUBLIC_CONTRACT_REVISION\s*=\s*(\d+)/m)?.[1]);
const releasedTools = [...releasedServer.matchAll(/@server\.tool\(name="([^"]+)"/g)].map((match) => match[1]);

assert(releasedVersion === authority.packageVersion, 'Released package version does not match snapshot');
assert(releasedPython === authority.pythonRequires, 'Released Python boundary does not match snapshot');
assert(releasedRevision === authority.publicContractRevision, 'Released public-contract revision does not match snapshot');
assert(JSON.stringify(releasedTools) === JSON.stringify(authority.tools), 'Released five-tool surface does not match snapshot');
assert(
  releasedEnvironment.includes(`Linked SQLite | **${authority.linkedSqliteMinimum} or newer**.`),
  'Released SQLite boundary does not match snapshot',
);
assert(releasedEnvironment.includes(authority.transport), 'Released transport boundary does not match snapshot');
assert(releasedEnvironment.includes(authority.embeddingProvider), 'Released embedding-provider boundary does not match snapshot');

const reachableTags = git(repoRoot, ['tag', '--merged', 'HEAD', '--sort=-version:refname', '--list', 'v*'])
  .split(/\r?\n/)
  .filter((value) => /^v\d+\.\d+\.\d+$/.test(value));
assert(reachableTags.length > 0, 'No reachable semantic release tag found');
const latestReachableReleaseTag = reachableTags[0];
const authorityVersionParts = authority.tag.slice(1).split('.').map(Number);
function atOrBeforeAuthority(tag) {
  const parts = tag.slice(1).split('.').map(Number);
  for (let index = 0; index < 3; index += 1) {
    if (parts[index] < authorityVersionParts[index]) return true;
    if (parts[index] > authorityVersionParts[index]) return false;
  }
  return true;
}
const allowedNamedVersions = new Set(
  git(repoRoot, ['tag', '--list', 'v*'])
    .split(/\r?\n/)
    .filter((value) => /^v\d+\.\d+\.\d+$/.test(value) && atOrBeforeAuthority(value)),
);
if (/^\d+\.\d+\.\d+$/.test(currentPackageVersion ?? '')) {
  allowedNamedVersions.add(`v${currentPackageVersion}`);
}

function decodeHtml(value) {
  return value
    .replaceAll('&amp;', '&')
    .replaceAll('&quot;', '"')
    .replaceAll('&#x27;', "'")
    .replaceAll('&#39;', "'")
    .replaceAll('&lt;', '<')
    .replaceAll('&gt;', '>');
}

function visibleText(fragment) {
  return decodeHtml(
    fragment
      .replace(/<script\b[\s\S]*?<\/script>/gi, ' ')
      .replace(/<style\b[\s\S]*?<\/style>/gi, ' ')
      .replace(/<[^>]+>/g, ' ')
      .replace(/\s+/g, ' ')
      .trim(),
  );
}

function attr(tag, name) {
  const match = tag.match(new RegExp(`\\b${name}=["']([^"']*)["']`, 'i'));
  return match ? decodeHtml(match[1]) : '';
}

function stableValue(value) {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([key, nested]) => [key, stableValue(nested)]),
    );
  }
  return value;
}

function fileForRoute(root, route) {
  if (route === '/') return path.join(root, 'index.html');
  return path.join(root, route.replace(/^\//, ''), 'index.html');
}

async function routesFromBuild(root) {
  const routes = [];
  for (const relative of ['sitemap.xml', 'ar/sitemap.xml']) {
    const xml = await readFile(path.join(root, relative), 'utf8');
    for (const match of xml.matchAll(/<loc>([^<]+)<\/loc>/g)) {
      const parsed = new URL(match[1].trim());
      routes.push(parsed.pathname);
    }
  }
  const unique = [...new Set(routes)].sort();
  assert(unique.length === routes.length, `Duplicate canonical route in ${root}`);
  assert(unique.length > 0, `No canonical routes found in ${root}`);
  return unique;
}

async function routeProjection(root, route) {
  const html = await readFile(fileForRoute(root, route), 'utf8');
  const title = decodeHtml(html.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1]?.trim() ?? '');
  const metaTags = [...html.matchAll(/<meta\b[^>]*>/gi)].map((match) => match[0]);
  const descriptionTag = metaTags.find((tag) => attr(tag, 'name').toLowerCase() === 'description');
  const description = descriptionTag ? attr(descriptionTag, 'content') : '';
  const main = html.match(/<main\b[^>]*>([\s\S]*?)<\/main>/i)?.[1];
  assert(main !== undefined, `Release projection route is missing SSR <main>: ${route}`);
  const jsonLd = [...html.matchAll(/<script[^>]+type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi)]
    .map((match) => stableValue(JSON.parse(match[1])));
  const externalHrefs = [...html.matchAll(/<a\b[^>]+href=["']([^"']+)["'][^>]*>/gi)]
    .map((match) => decodeHtml(match[1]).trim())
    .filter((href) => /^https?:\/\//i.test(href))
    .map((href) => new URL(href).href)
    .filter((href, index, values) => values.indexOf(href) === index)
    .sort();
  const projection = {
    title,
    description,
    pageText: visibleText(html),
    jsonLd,
    externalHrefs,
  };
  return {
    sha256: sha256Text(JSON.stringify(projection)),
    pageChars: projection.pageText.length,
  };
}

async function buildProjection(root) {
  const routes = await routesFromBuild(root);
  const byRoute = {};
  for (const route of routes) byRoute[route] = await routeProjection(root, route);
  const nonHtml = {};
  for (const relative of ['llms.txt']) {
    const text = await readFile(path.join(root, relative), 'utf8');
    nonHtml[relative] = sha256Text(text);
  }
  const hashes = {
    routes: Object.fromEntries(routes.map((route) => [route, byRoute[route].sha256])),
    nonHtml,
  };
  return {
    routes,
    byRoute,
    nonHtml,
    sha256: sha256Text(JSON.stringify(hashes)),
  };
}

const [releasedProjection, currentProjection] = await Promise.all([
  buildProjection(baselineBuildRoot),
  buildProjection(buildRoot),
]);
const routeSetDrift = JSON.stringify(currentProjection.routes) !== JSON.stringify(releasedProjection.routes);
const projectionRouteDiffs = [...new Set([...releasedProjection.routes, ...currentProjection.routes])]
  .filter((route) => releasedProjection.byRoute[route]?.sha256 !== currentProjection.byRoute[route]?.sha256)
  .sort();
const projectionFileDiffs = [...new Set([
  ...Object.keys(releasedProjection.nonHtml),
  ...Object.keys(currentProjection.nonHtml),
])]
  .filter((relative) => releasedProjection.nonHtml[relative] !== currentProjection.nonHtml[relative])
  .sort();

const publicClaimSources = [
  'src/content/homeCopy.ts',
  'src/content/answerAuthority.json',
  'src/content/docSeo.json',
  'src/pages/index.tsx',
  'plugins/seo-regression-guard.mjs',
  'docusaurus.config.ts',
];
const namedVersions = new Set();
const releasedNamedVersions = new Set();
for (const relative of publicClaimSources) {
  const text = await readFile(path.join(siteRoot, relative), 'utf8');
  for (const match of text.matchAll(/\bv\d+\.\d+\.\d+\b/g)) namedVersions.add(match[0]);
  const releasedText = git(repoRoot, ['show', `${authority.tag}:site/${relative}`]);
  for (const match of releasedText.matchAll(/\bv\d+\.\d+\.\d+\b/g)) releasedNamedVersions.add(match[0]);
}
for (const value of releasedNamedVersions) allowedNamedVersions.add(value);
const unexpectedNamedVersions = [...namedVersions]
  .filter((value) => !allowedNamedVersions.has(value))
  .sort();

const indexSource = await readFile(path.join(siteRoot, 'src', 'pages', 'index.tsx'), 'utf8');
const softwareVersion = indexSource.match(/softwareVersion:\s*'([^']+)'/)?.[1];
assert(
  softwareVersion === authority.packageVersion || softwareVersion === currentPackageVersion,
  `Published softwareVersion must match released authority ${authority.packageVersion} or current package ${currentPackageVersion}`,
);
const publishedVersionTag = `v${softwareVersion}`;
const answerAuthority = await readJson(path.join(siteRoot, 'src', 'content', 'answerAuthority.json'));
for (const locale of ['en', 'ar']) {
  const fiveTools = answerAuthority[locale]?.answers?.find((answer) => answer.id === 'five-tools')?.answer ?? '';
  for (const tool of authority.tools) assert(fiveTools.includes(tool), `Published ${locale} five-tool answer missing ${tool}`);
  const environment = answerAuthority[locale]?.answers?.find((answer) => answer.id === 'supported-environment')?.answer ?? '';
  for (const token of [publishedVersionTag, 'Python 3.11+', 'SQLite 3.42.0+', authority.transport, authority.embeddingProvider]) {
    assert(environment.includes(token), `Published ${locale} environment answer missing release-bound token ${token}`);
  }
}

const releaseSnapshotStale = latestReachableReleaseTag !== authority.tag;
const releaseClaimDrift =
  routeSetDrift ||
  projectionRouteDiffs.length > 0 ||
  projectionFileDiffs.length > 0 ||
  unexpectedNamedVersions.length > 0 ||
  softwareVersion !== authority.packageVersion;

let publicationClass = 'RELEASE_NEUTRAL';
let publicationEligibility = 'ELIGIBLE_PENDING_OTHER_GATES';
if (releaseSnapshotStale) {
  publicationClass = 'UNCLASSIFIED';
  publicationEligibility = 'HOLD_RELEASE_SNAPSHOT_STALE';
} else if (releaseClaimDrift) {
  publicationClass = 'RELEASE_BOUND';
  publicationEligibility = 'HOLD_UNRELEASED_CONTRACT';
}

const receipt = {
  status: 'PASS',
  schemaVersion: 1,
  source: {
    commit: git(repoRoot, ['rev-parse', 'HEAD']),
    tree: git(repoRoot, ['rev-parse', 'HEAD^{tree}']),
  },
  releasedAuthority: {
    ...authority,
    latestReachableReleaseTag,
    snapshotSha256: await sha256File(snapshotPath),
  },
  releasedPublicationBaseline: {
    ...baseline,
    controlIntroductionCommit,
    derivedAcceptedMainCommit,
    derivedAcceptedMainTree,
    protectedBaseCommit,
    snapshotExistedAtProtectedBase,
  },
  releasedSiteBaseline: {
    sourceCommit: derivedAcceptedMainCommit,
    sourceTree: derivedAcceptedMainTree,
    explicit: Boolean(releasedSiteBaseline),
  },
  productionDeploymentBaseline: {...baseline},
  publicationClass,
  publicationEligibility,
  releaseSnapshotStale,
  releaseClaimDrift,
  routeSetDrift,
  projectionRouteDiffs,
  projectionFileDiffs,
  releasedProjectionSha256: releasedProjection.sha256,
  currentProjectionSha256: currentProjection.sha256,
  releasedProjectionRoutes: releasedProjection.routes.length,
    currentProjectionRoutes: currentProjection.routes.length,
    namedVersions: [...namedVersions].sort(),
    releasedNamedVersions: [...releasedNamedVersions].sort(),
    unexpectedNamedVersions,
  currentPackageVersion,
  softwareVersion,
};

await writeJson(receiptPath, receipt);
console.log(JSON.stringify(receipt, null, 2));

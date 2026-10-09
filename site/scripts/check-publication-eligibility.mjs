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
const receiptPath = path.resolve(args.receipt ?? path.join(buildRoot, '..', 'PUBLICATION_ELIGIBILITY.json'));
const snapshotPath = path.resolve(
  args.snapshot ?? path.join(siteRoot, 'publication', 'released-contract-v0.1.3.json'),
);

assert(args.build, '--build is required');
const snapshot = await readJson(snapshotPath);
assert(snapshot.schemaVersion === 1, `Unsupported released-contract schema: ${snapshot.schemaVersion}`);
const authority = snapshot.releaseAuthority;
assert(authority && typeof authority === 'object', 'Released-contract snapshot is missing releaseAuthority');

const tagRef = `refs/tags/${authority.tag}`;
assert(git(repoRoot, ['cat-file', '-t', tagRef]) === 'tag', `Released authority tag is not annotated: ${authority.tag}`);
assert(git(repoRoot, ['rev-parse', tagRef]) === authority.tagObject, `Released tag object drift: ${authority.tag}`);
assert(git(repoRoot, ['rev-parse', `${authority.tag}^{commit}`]) === authority.commit, `Released commit drift: ${authority.tag}`);
assert(git(repoRoot, ['rev-parse', `${authority.tag}^{tree}`]) === authority.tree, `Released tree drift: ${authority.tag}`);

const releasedPyproject = git(repoRoot, ['show', `${authority.tag}:pyproject.toml`]);
const releasedContract = git(repoRoot, ['show', `${authority.tag}:src/factlane/contract.py`]);
const releasedServer = git(repoRoot, ['show', `${authority.tag}:src/factlane/server.py`]);
const releasedEnvironment = git(repoRoot, ['show', `${authority.tag}:docs/ENVIRONMENT.md`]);

const releasedVersion = releasedPyproject.match(/^version\s*=\s*"([^"]+)"/m)?.[1];
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

function fileForRoute(route) {
  if (route === '/') return path.join(buildRoot, 'index.html');
  return path.join(buildRoot, route.replace(/^\//, ''), 'index.html');
}

const routeDiffs = [];
const currentContract = {};
for (const [route, expectedHash] of Object.entries(snapshot.contractMainText)) {
  const html = await readFile(fileForRoute(route), 'utf8');
  const main = html.match(/<main\b[^>]*>([\s\S]*?)<\/main>/i)?.[1];
  assert(main !== undefined, `Released-contract route is missing SSR <main>: ${route}`);
  const digest = sha256Text(visibleText(main));
  currentContract[route] = digest;
  if (digest !== expectedHash) routeDiffs.push(route);
}

const landingSectionDiffs = [];
const currentLandingSections = {};
for (const [route, sections] of Object.entries(snapshot.landingReleaseSections ?? {})) {
  const html = await readFile(fileForRoute(route), 'utf8');
  currentLandingSections[route] = {};
  for (const [sectionId, expectedHash] of Object.entries(sections)) {
    const escapedId = sectionId.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const section = html.match(
      new RegExp(`<section\\b[^>]*id=["']${escapedId}["'][^>]*>([\\s\\S]*?)<\\/section>`, 'i'),
    )?.[1];
    assert(section !== undefined, `Released landing section is missing: ${route}#${sectionId}`);
    const digest = sha256Text(visibleText(section));
    currentLandingSections[route][sectionId] = digest;
    if (digest !== expectedHash) landingSectionDiffs.push(`${route}#${sectionId}`);
  }
}

const publicClaimSources = [
  'src/content/homeCopy.ts',
  'src/content/answerAuthority.json',
  'src/content/docSeo.json',
  'src/pages/index.tsx',
  'plugins/seo-regression-guard.mjs',
  'docusaurus.config.ts',
];
const namedVersions = new Set();
for (const relative of publicClaimSources) {
  const text = await readFile(path.join(siteRoot, relative), 'utf8');
  for (const match of text.matchAll(/\bv\d+\.\d+\.\d+\b/g)) namedVersions.add(match[0]);
}
const unexpectedNamedVersions = [...namedVersions]
  .filter((value) => !snapshot.allowedNamedVersions.includes(value))
  .sort();

const indexSource = await readFile(path.join(siteRoot, 'src', 'pages', 'index.tsx'), 'utf8');
const softwareVersion = indexSource.match(/softwareVersion:\s*'([^']+)'/)?.[1];
const answerAuthority = await readJson(path.join(siteRoot, 'src', 'content', 'answerAuthority.json'));
for (const locale of ['en', 'ar']) {
  const fiveTools = answerAuthority[locale]?.answers?.find((answer) => answer.id === 'five-tools')?.answer ?? '';
  for (const tool of authority.tools) assert(fiveTools.includes(tool), `Published ${locale} five-tool answer missing ${tool}`);
  const environment = answerAuthority[locale]?.answers?.find((answer) => answer.id === 'supported-environment')?.answer ?? '';
  for (const token of [authority.tag, 'Python 3.11+', 'SQLite 3.42.0+', authority.transport, authority.embeddingProvider]) {
    assert(environment.includes(token), `Published ${locale} environment answer missing released token ${token}`);
  }
}

const releaseSnapshotStale = latestReachableReleaseTag !== authority.tag;
const releaseClaimDrift =
  routeDiffs.length > 0 ||
  landingSectionDiffs.length > 0 ||
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
  releasedPublicationBaseline: snapshot.releasedPublicationBaseline,
  publicationClass,
  publicationEligibility,
  releaseSnapshotStale,
  releaseClaimDrift,
  contractRouteDiffs: routeDiffs,
  landingSectionDiffs,
  namedVersions: [...namedVersions].sort(),
  unexpectedNamedVersions,
  softwareVersion,
  contractMainText: currentContract,
  landingReleaseSections: currentLandingSections,
};

await writeJson(receiptPath, receipt);
console.log(JSON.stringify(receipt, null, 2));

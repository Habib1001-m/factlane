import {execFileSync} from 'node:child_process';
import {copyFile, mkdir, mkdtemp, readFile, readdir, rename, rm, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

import {
  assert,
  checksumText,
  exists,
  git,
  manifestForRoot,
  parseArgs,
  readJson,
  safeRelative,
  sha256File,
  sha256Text,
} from './publication-control-lib.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const siteRoot = path.resolve(here, '..');
const repoRoot = path.resolve(siteRoot, '..');
const args = parseArgs(process.argv.slice(2));
const rootA = path.resolve(args['root-a'] ?? '');
const rootB = path.resolve(args['root-b'] ?? '');
const packageDir = path.resolve(args.package ?? '');
const origin = new URL(args.origin ?? '').origin;
const eligibilityPath = path.resolve(args.eligibility ?? '');
const readinessPath = path.resolve(args.readiness ?? '');
const compositionPath = path.resolve(args.composition ?? '');

for (const [name, value] of [
  ['--root-a', args['root-a']],
  ['--root-b', args['root-b']],
  ['--package', args.package],
  ['--origin', args.origin],
  ['--eligibility', args.eligibility],
  ['--readiness', args.readiness],
  ['--composition', args.composition],
]) {
  assert(value, `${name} is required`);
}
assert(new URL(args.origin).href === `${origin}/` && origin.startsWith('https://'), '--origin must be an HTTPS origin only');
assert(!(await exists(packageDir)), `Package directory must not already exist: ${packageDir}`);

const sourceCommit = git(repoRoot, ['rev-parse', 'HEAD']);
const sourceTree = git(repoRoot, ['rev-parse', 'HEAD^{tree}']);
const sourceParent = git(repoRoot, ['rev-parse', 'HEAD^']);
const sourceCommitIso = git(repoRoot, ['show', '-s', '--format=%cI', 'HEAD']);
const sourceDateEpoch = git(repoRoot, ['show', '-s', '--format=%ct', 'HEAD']);

const eligibility = await readJson(eligibilityPath);
assert(eligibility.status === 'PASS', 'Publication eligibility evaluation must complete successfully');
assert(eligibility.source?.commit === sourceCommit, 'Eligibility receipt commit does not match package source');
assert(eligibility.source?.tree === sourceTree, 'Eligibility receipt tree does not match package source');

const readiness = await readJson(readinessPath);
assert(readiness.status === 'PASS', 'Publication readiness must PASS before freezing');
assert(readiness.expectedOrigin === origin, 'Readiness receipt origin mismatch');
assert(readiness.mode === 'public', 'Readiness receipt must be public mode');

const composition = await readJson(compositionPath);
assert(composition.status === 'PASS', 'Overlay composition must PASS before freezing');
assert(composition.publicOrigin === origin, 'Overlay composition origin mismatch');

const a = await manifestForRoot(rootA);
const b = await manifestForRoot(rootB);
const manifestTextA = checksumText(a.manifest);
const manifestTextB = checksumText(b.manifest);
assert(manifestTextA === manifestTextB, 'Isolated composed artifact manifests are not byte-identical');
const artifactManifestSha256 = sha256Text(manifestTextA);
assert(
  composition.composedArtifact?.manifestSha256 === artifactManifestSha256,
  'Composition receipt does not bind the frozen artifact manifest',
);
assert(composition.composedArtifact?.files === a.manifest.size, 'Composition receipt file count mismatch');

const routes = composition.routes;
assert(Array.isArray(routes) && routes.length === 28, `Expected 28 canonical routes, found ${routes?.length}`);
const routesText = `${routes.join('\n')}\n`;
assert(sha256Text(routesText) === composition.routeInventorySha256, 'Composition route inventory digest mismatch');

const critical = new Map();
for (const relative of [
  'index.html',
  'ar/index.html',
  'answers/index.html',
  'ar/answers/index.html',
  '404.html',
  'ar/404.html',
  'robots.txt',
  'ar/robots.txt',
  'sitemap.xml',
  'ar/sitemap.xml',
  'llms.txt',
  'favicon/favicon-16.png',
  'favicon/favicon-32.png',
  'favicon/favicon-256.png',
  'logo/factlane-mark.svg',
  'social/factlane-github-social-preview.png',
  'ar/social/factlane-github-social-preview.png',
  '_redirects',
  '1d226189096d15c62deace5c0bb3ade7.txt',
  'google0227effcabf0f657.html',
  'BingSiteAuth.xml',
]) {
  assert(a.manifest.has(relative), `Critical publication file missing: ${relative}`);
  critical.set(relative, a.manifest.get(relative));
}
for (const [label, pattern] of [
  ['EN stylesheet', /^assets\/css\/styles\.[0-9a-f]+\.css$/],
  ['EN main JS', /^assets\/js\/main\.[0-9a-f]+\.js$/],
  ['EN runtime JS', /^assets\/js\/runtime~main\.[0-9a-f]+\.js$/],
  ['AR stylesheet', /^ar\/assets\/css\/styles\.[0-9a-f]+\.css$/],
  ['AR main JS', /^ar\/assets\/js\/main\.[0-9a-f]+\.js$/],
  ['AR runtime JS', /^ar\/assets\/js\/runtime~main\.[0-9a-f]+\.js$/],
]) {
  const matches = [...a.manifest.keys()].filter((relative) => pattern.test(relative));
  assert(matches.length === 1, `Expected exactly one ${label}, found ${matches.length}`);
  critical.set(matches[0], a.manifest.get(matches[0]));
}
const criticalText = checksumText(critical);

await mkdir(packageDir, {recursive: true});
const temporary = await mkdtemp(path.join(os.tmpdir(), 'factlane-freeze-'));
const shortCommit = sourceCommit.slice(0, 8);
const archiveName = `factlane-site-public-${shortCommit}.tar.gz`;

function createArchive(root, targetBase) {
  const tarPath = `${targetBase}.tar`;
  execFileSync('tar', [
    '--sort=name',
    `--mtime=@${sourceDateEpoch}`,
    '--owner=0',
    '--group=0',
    '--numeric-owner',
    '--format=gnu',
    '-C',
    root,
    '-cf',
    tarPath,
    '.',
  ]);
  execFileSync('gzip', ['-n', '-f', tarPath]);
  return `${tarPath}.gz`;
}

let archiveA;
let archiveB;
try {
  archiveA = createArchive(rootA, path.join(temporary, 'artifact-a'));
  archiveB = createArchive(rootB, path.join(temporary, 'artifact-b'));
  const archiveShaA = await sha256File(archiveA);
  const archiveShaB = await sha256File(archiveB);
  assert(archiveShaA === archiveShaB, 'Deterministic archives diverged across isolated builds');
  await rename(archiveA, path.join(packageDir, archiveName));

  await writeFile(path.join(packageDir, 'ARTIFACT_MANIFEST.sha256'), manifestTextA, {mode: 0o644});
  await writeFile(path.join(packageDir, 'CRITICAL_FILES.sha256'), criticalText, {mode: 0o644});
  await writeFile(path.join(packageDir, 'ROUTES.txt'), routesText, {mode: 0o644});
  await copyFile(eligibilityPath, path.join(packageDir, 'PUBLICATION_ELIGIBILITY.json'));
  await copyFile(compositionPath, path.join(packageDir, 'OVERLAY_COMPOSITION.json'));
  await copyFile(readinessPath, path.join(packageDir, 'READINESS_RESULT.json'));

  const consumerScripts = [
    'verify-publication-package.mjs',
    'serve-publication-static.mjs',
    'check-publication-readiness.mjs',
  ];
  for (const script of consumerScripts) {
    const destination = path.join(packageDir, 'consumer', 'scripts', script);
    await mkdir(path.dirname(destination), {recursive: true});
    await copyFile(path.join(siteRoot, 'scripts', script), destination);
  }
  const authorityDestination = path.join(packageDir, 'consumer', 'src', 'content', 'answerAuthority.json');
  await mkdir(path.dirname(authorityDestination), {recursive: true});
  await copyFile(path.join(siteRoot, 'src', 'content', 'answerAuthority.json'), authorityDestination);

  const archiveSha256 = archiveShaA;
  const overlayManifestPath = path.join(siteRoot, 'publication', 'overlays', 'manifest.json');
  const releasedSnapshotPath = path.join(siteRoot, 'publication', 'released-contract-v0.1.3.json');
  await copyFile(overlayManifestPath, path.join(packageDir, 'OVERLAY_CONTROL.json'));
  await copyFile(releasedSnapshotPath, path.join(packageDir, 'RELEASED_CONTRACT_SNAPSHOT.json'));
  const packageJson = JSON.parse(await readFile(path.join(siteRoot, 'package.json'), 'utf8'));
  const handoff = `# FactLane controlled publication handoff\n\n` +
    `This package is a qualified publication candidate only. It does not authorize Production deployment, rollback, a GitHub merge, a release, or a tag.\n\n` +
    `- Source commit: \`${sourceCommit}\`\n` +
    `- Source tree: \`${sourceTree}\`\n` +
    `- Public origin: \`${origin}\`\n` +
    `- Publication class: \`${eligibility.publicationClass}\`\n` +
    `- Publication eligibility: \`${eligibility.publicationEligibility}\`\n` +
    `- Artifact files: ${a.manifest.size}\n` +
    `- Artifact manifest SHA-256: \`${artifactManifestSha256}\`\n` +
    `- Frozen archive SHA-256: \`${archiveSha256}\`\n\n` +
    `Before any separately authorized crossing, authenticate PACKAGE_CONTENTS.sha256 with the owner-authorized digest, run sha256sum -c, run the bundled package verifier, and deploy only the exact extracted archive bytes. A rebuild is not an equivalent crossing object.\n`;
  await writeFile(path.join(packageDir, 'CONSUMER_HANDOFF.md'), handoff, {encoding: 'utf8', mode: 0o644});

  const consumerVerifierSha = await sha256File(path.join(packageDir, 'consumer', 'scripts', 'verify-publication-package.mjs'));
  const consumerStaticSha = await sha256File(path.join(packageDir, 'consumer', 'scripts', 'serve-publication-static.mjs'));
  const consumerReadinessSha = await sha256File(path.join(packageDir, 'consumer', 'scripts', 'check-publication-readiness.mjs'));
  const consumerAuthoritySha = await sha256File(authorityDestination);
  const handoffSha = await sha256File(path.join(packageDir, 'CONSUMER_HANDOFF.md'));
  const eligibilitySha = await sha256File(path.join(packageDir, 'PUBLICATION_ELIGIBILITY.json'));
  const compositionSha = await sha256File(path.join(packageDir, 'OVERLAY_COMPOSITION.json'));
  const readinessSha = await sha256File(path.join(packageDir, 'READINESS_RESULT.json'));

  const provenanceLines = [
    'PACKAGE=FACTLANE_SITE_CONTROLLED_PUBLICATION_CI_R1',
    `SOURCE_COMMIT=${sourceCommit}`,
    `SOURCE_TREE=${sourceTree}`,
    `SOURCE_PARENT=${sourceParent}`,
    `SOURCE_COMMIT_ISO=${sourceCommitIso}`,
    `SOURCE_DATE_EPOCH=${sourceDateEpoch}`,
    `PUBLIC_ORIGIN=${origin}`,
    'FACTLANE_PUBLIC_BUILD=1',
    `NODE_VERSION=${process.version}`,
    `NPM_VERSION=${execFileSync('npm', ['--version'], {encoding: 'utf8'}).trim()}`,
    `DOCUSAURUS_VERSION=${packageJson.dependencies['@docusaurus/core']}`,
    `WRANGLER_VERSION=${packageJson.devDependencies.wrangler}`,
    `PACKAGE_LOCK_SHA256=${await sha256File(path.join(siteRoot, 'package-lock.json'))}`,
    `PACKAGE_JSON_SHA256=${await sha256File(path.join(siteRoot, 'package.json'))}`,
    `BABEL_CONFIG_SHA256=${await sha256File(path.join(siteRoot, 'babel.config.js'))}`,
    `RUNBOOK_SHA256=${await sha256File(path.join(siteRoot, 'PUBLICATION_READINESS.md'))}`,
    'ISOLATED_BUILD_COUNT=2',
    `ARTIFACT_FILE_COUNT=${a.manifest.size}`,
    `ARTIFACT_MANIFEST_SHA256=${artifactManifestSha256}`,
    'ARTIFACT_MANIFESTS_BYTE_IDENTICAL=YES',
    `ROUTE_COUNT=${routes.length}`,
    `ROUTE_INVENTORY_SHA256=${sha256Text(routesText)}`,
    `CRITICAL_FILE_COUNT=${critical.size}`,
    `CRITICAL_FILES_SHA256=${sha256Text(criticalText)}`,
    `FROZEN_ARCHIVE=${archiveName}`,
    `FROZEN_ARCHIVE_SHA256=${archiveSha256}`,
    'ARCHIVE_REPRODUCIBLE_ACROSS_ISOLATED_BUILDS=YES',
    'ARCHIVE_RECIPE=GNU_tar_sort_name_fixed_commit_mtime_owner_0_group_0_numeric_owner_format_gnu_then_gzip_n',
    `PUBLICATION_CLASS=${eligibility.publicationClass}`,
    `PUBLICATION_ELIGIBILITY=${eligibility.publicationEligibility}`,
    `PUBLICATION_ELIGIBILITY_SHA256=${eligibilitySha}`,
    `RELEASED_TAG=${eligibility.releasedAuthority.tag}`,
    `RELEASED_TAG_OBJECT=${eligibility.releasedAuthority.tagObject}`,
    `RELEASED_COMMIT=${eligibility.releasedAuthority.commit}`,
    `RELEASED_TREE=${eligibility.releasedAuthority.tree}`,
    `RELEASED_PUBLICATION_BASELINE_COMMIT=${eligibility.releasedPublicationBaseline.acceptedMainCommit}`,
    `RELEASED_PUBLICATION_BASELINE_TREE=${eligibility.releasedPublicationBaseline.acceptedMainTree}`,
    `RELEASED_PUBLICATION_PROJECTION_SHA256=${eligibility.releasedProjectionSha256}`,
    `CURRENT_PUBLICATION_PROJECTION_SHA256=${eligibility.currentProjectionSha256}`,
    `RELEASED_CONTRACT_SNAPSHOT_SHA256=${await sha256File(releasedSnapshotPath)}`,
    `OVERLAY_CONTROL_SHA256=${await sha256File(overlayManifestPath)}`,
    `OVERLAY_COMPOSITION_SHA256=${compositionSha}`,
    `COMPOSED_REDIRECTS_SHA256=${composition.redirects.sha256}`,
    `READINESS_RESULT_SHA256=${readinessSha}`,
    'CLOUDFLARE_PROJECT=factlane',
    'CLOUDFLARE_DEPLOYMENT_METHOD=PAGES_DIRECT_UPLOAD',
    'CLOUDFLARE_GIT_SOURCE_REQUIRED=NO',
    'PUBLIC_CROSSING_AUTHORIZED=NO',
    'PRODUCTION_AUTHORIZED=NO',
    `CONSUMER_PACKAGE_VERIFIER_SHA256=${consumerVerifierSha}`,
    `CONSUMER_STATIC_HOST_SHA256=${consumerStaticSha}`,
    `CONSUMER_READINESS_HARNESS_SHA256=${consumerReadinessSha}`,
    `CONSUMER_ANSWER_AUTHORITY_SHA256=${consumerAuthoritySha}`,
    `CONSUMER_HANDOFF_SHA256=${handoffSha}`,
    `CONSUMER_NODE_PROFILE=${process.version}`,
    'CONSUMER_TRUSTED_SHA256SUM_BOOTSTRAP=REQUIRED_BEFORE_PACKAGE_CODE',
  ];
  await writeFile(path.join(packageDir, 'PROVENANCE.txt'), `${provenanceLines.join('\n')}\n`, {encoding: 'utf8', mode: 0o644});

  const verificationLines = [
    'SITE_CI_QUALIFICATION=PASS',
    'ISOLATED_BUILD_COUNT=2',
    'BASE_AND_COMPOSED_REPRODUCIBILITY=PASS',
    `PUBLICATION_CLASS=${eligibility.publicationClass}`,
    `PUBLICATION_ELIGIBILITY=${eligibility.publicationEligibility}`,
    `RELEASE_CONTRACT_ROUTE_SET_DRIFT=${eligibility.routeSetDrift ? 'YES' : 'NO'}`,
    `RELEASE_CONTRACT_PROJECTION_DIFFS=${eligibility.projectionRouteDiffs.length}`,
    `RELEASED_PUBLICATION_PROJECTION_SHA256=${eligibility.releasedProjectionSha256}`,
    `CURRENT_PUBLICATION_PROJECTION_SHA256=${eligibility.currentProjectionSha256}`,
    'OVERLAY_COMPOSITION=PASS',
    `OVERLAY_FILES=${composition.overlayFiles.length}`,
    `REDIRECT_RULES=${composition.redirects.rules}`,
    `READINESS_STATUS=${readiness.status}`,
    `READINESS_CANONICAL_ROUTES=${readiness.canonicalRoutes}`,
    `READINESS_CRAWLER_COMPARISONS=${readiness.crawlerComparisons}`,
    'PRODUCTION_AUTHORIZED=NO',
  ];
  await writeFile(path.join(packageDir, 'VERIFICATION.txt'), `${verificationLines.join('\n')}\n`, {encoding: 'utf8', mode: 0o644});

  const packageFiles = [];
  async function listPackage(current, relative = '') {
    const entries = await readdir(current, {withFileTypes: true});
    for (const entry of entries) {
      const rel = relative ? `${relative}/${entry.name}` : entry.name;
      if (rel === 'PACKAGE_CONTENTS.sha256') continue;
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) await listPackage(full, rel);
      else if (entry.isFile()) packageFiles.push(rel);
      else assert(false, `Unsupported package object: ${rel}`);
    }
  }
  await listPackage(packageDir);
  packageFiles.sort();
  let packageContents = '';
  for (const relative of packageFiles) {
    safeRelative(relative, 'package file');
    packageContents += `${await sha256File(path.join(packageDir, relative))}  ./${relative}\n`;
  }
  await writeFile(path.join(packageDir, 'PACKAGE_CONTENTS.sha256'), packageContents, {encoding: 'utf8', mode: 0o644});
  const packageContentsSha256 = await sha256File(path.join(packageDir, 'PACKAGE_CONTENTS.sha256'));

  console.log(JSON.stringify({
    status: 'PASS',
    packageDir,
    sourceCommit,
    sourceTree,
    publicationClass: eligibility.publicationClass,
    publicationEligibility: eligibility.publicationEligibility,
    artifactFiles: a.manifest.size,
    artifactManifestSha256,
    archive: archiveName,
    archiveSha256,
    packageContentsSha256,
  }, null, 2));
} finally {
  await rm(temporary, {recursive: true, force: true});
}

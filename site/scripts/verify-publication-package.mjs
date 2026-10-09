import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {
  lstat,
  mkdir,
  readFile,
  readdir,
  rm,
} from 'node:fs/promises';
import path from 'node:path';

function fail(message) {
  console.error(`PUBLICATION_PACKAGE_VERIFY=HOLD ${message}`);
  throw new Error(message);
}

function assert(condition, message) {
  if (!condition) fail(message);
}

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith('--')) fail(`Unexpected argument: ${token}`);
    const key = token.slice(2);
    const value = argv[index + 1];
    if (!value || value.startsWith('--')) fail(`Missing value for --${key}`);
    result[key] = value;
    index += 1;
  }
  return result;
}

const args = parseArgs(process.argv.slice(2));
const packageDir = path.resolve(args.package ?? '.');
assert(!(args.extract && args.root), '--extract and --root are mutually exclusive; verify the bytes you actually extract');
const extractDir = args.extract ? path.resolve(args.extract) : null;
const rootToVerify = args.root ? path.resolve(args.root) : extractDir;
const expectedPackageContentsSha256 = args['expected-package-contents-sha256'];
assert(
  /^[0-9a-f]{64}$/.test(expectedPackageContentsSha256 ?? ''),
  '--expected-package-contents-sha256 is required and must be a lowercase SHA-256 digest from the crossing authorization',
);

async function sha256File(filePath) {
  const hash = createHash('sha256');
  hash.update(await readFile(filePath));
  return hash.digest('hex');
}

function safeRelative(value, label) {
  assert(value.length > 0, `${label} path is empty`);
  assert(!path.posix.isAbsolute(value), `${label} path must be relative: ${value}`);
  assert(!value.includes('\\'), `${label} path must use POSIX separators: ${value}`);
  const normalized = value.replace(/^\.\//, '').replace(/\/$/, '');
  if (!normalized) return '';
  const parts = normalized.split('/');
  assert(parts.every((part) => part && part !== '.' && part !== '..'), `${label} path escapes root: ${value}`);
  return normalized;
}

function parseChecksumFile(text, label) {
  const records = new Map();
  for (const rawLine of text.split(/\r?\n/)) {
    if (!rawLine) continue;
    const match = rawLine.match(/^([0-9a-f]{64})  (.+)$/);
    assert(match, `Malformed ${label} line: ${rawLine}`);
    const relative = safeRelative(match[2], label);
    assert(relative, `${label} may not describe artifact root`);
    assert(!records.has(relative), `Duplicate ${label} path: ${relative}`);
    records.set(relative, match[1]);
  }
  return records;
}

function parseKeyValue(text, label) {
  const values = new Map();
  for (const rawLine of text.split(/\r?\n/)) {
    if (!rawLine) continue;
    const index = rawLine.indexOf('=');
    assert(index > 0, `Malformed ${label} line: ${rawLine}`);
    const key = rawLine.slice(0, index);
    const value = rawLine.slice(index + 1);
    assert(!values.has(key), `Duplicate ${label} key: ${key}`);
    values.set(key, value);
  }
  return values;
}

async function walk(root) {
  const files = [];
  const directories = [];
  const symlinks = [];
  async function visit(current, relative = '') {
    const entries = await readdir(current, {withFileTypes: true});
    for (const entry of entries) {
      const nextRelative = relative ? `${relative}/${entry.name}` : entry.name;
      const full = path.join(current, entry.name);
      const stat = await lstat(full);
      if (stat.isSymbolicLink()) {
        symlinks.push(nextRelative);
      } else if (stat.isDirectory()) {
        directories.push(nextRelative);
        await visit(full, nextRelative);
      } else if (stat.isFile()) {
        files.push(nextRelative);
      } else {
        fail(`Unsupported filesystem object: ${nextRelative}`);
      }
    }
  }
  await visit(root);
  return {files: files.sort(), directories: directories.sort(), symlinks: symlinks.sort()};
}

async function verifyPackageContents() {
  const checksumPath = path.join(packageDir, 'PACKAGE_CONTENTS.sha256');
  const checksumDigest = await sha256File(checksumPath);
  assert(
    checksumDigest === expectedPackageContentsSha256,
    `External package trust-anchor mismatch: PACKAGE_CONTENTS.sha256 -> ${checksumDigest}`,
  );
  const expected = parseChecksumFile(await readFile(checksumPath, 'utf8'), 'package checksum');
  const tree = await walk(packageDir);
  assert(tree.symlinks.length === 0, `Package contains symlinks: ${tree.symlinks.join(', ')}`);
  const actualFiles = tree.files.filter((file) => file !== 'PACKAGE_CONTENTS.sha256');
  assert(
    JSON.stringify(actualFiles) === JSON.stringify([...expected.keys()].sort()),
    'Package file inventory does not exactly match PACKAGE_CONTENTS.sha256',
  );
  for (const [relative, digest] of expected) {
    assert(await sha256File(path.join(packageDir, relative)) === digest, `Package checksum mismatch: ${relative}`);
  }
  return {expected, checksumDigest};
}

function tarList(archivePath) {
  const names = execFileSync('tar', ['-tzf', archivePath], {encoding: 'utf8'}).trimEnd().split('\n');
  const verbose = execFileSync('tar', ['-tvzf', archivePath], {encoding: 'utf8'}).trimEnd().split('\n');
  assert(names.length === verbose.length, 'Archive list/verbose entry count mismatch');
  const forbiddenSegments = new Set(['.git', '.docusaurus', '.cache', 'node_modules', 'build']);
  const regularFiles = [];
  const directories = [];
  for (let index = 0; index < names.length; index += 1) {
    const rawName = names[index];
    const normalized = safeRelative(rawName, 'archive');
    const type = verbose[index]?.[0] ?? '';
    assert(type === '-' || type === 'd', `Archive contains unsupported link/device entry: ${rawName}`);
    if (!normalized) {
      assert(type === 'd', 'Archive root entry must be a directory');
      continue;
    }
    const segments = normalized.split('/');
    assert(
      !segments.some((segment) => segment.startsWith('.') || forbiddenSegments.has(segment)),
      `Archive contains hidden/dev/cache path: ${normalized}`,
    );
    const permissions = verbose[index].slice(0, 10);
    if (type === '-') {
      assert(permissions === '-rw-r--r--', `Unexpected published file permissions: ${normalized} -> ${permissions}`);
      regularFiles.push(normalized);
    } else {
      assert(permissions === 'drwxr-xr-x', `Unexpected published directory permissions: ${normalized} -> ${permissions}`);
      directories.push(normalized);
    }
  }
  return {regularFiles: regularFiles.sort(), directories: directories.sort(), entryCount: names.length};
}

function routeInventory(text, expectedOrigin) {
  const routes = text.split(/\r?\n/).filter(Boolean);
  assert(new Set(routes).size === routes.length, 'ROUTES.txt contains duplicate routes');
  const origin = new URL(expectedOrigin).origin;
  for (const value of routes) {
    const parsed = new URL(value);
    assert(parsed.origin === origin, `Route inventory origin mismatch: ${value}`);
    assert(parsed.search === '' && parsed.hash === '', `Route inventory must contain canonical URLs only: ${value}`);
  }
  return routes;
}

function localResourcePath(raw, sourceRelative, expectedOrigin) {
  if (!raw || raw.startsWith('#') || /^(?:data|mailto|tel|javascript):/i.test(raw)) return null;
  assert(!raw.includes('/tmp/') && !raw.includes('/home/') && !raw.startsWith('file:'), `Development path reference: ${sourceRelative} -> ${raw}`);
  const parsed = new URL(raw, `${expectedOrigin}/${sourceRelative}`);
  if (parsed.origin !== expectedOrigin) return null;
  const pathname = decodeURIComponent(parsed.pathname);
  const relative = safeRelative(pathname.replace(/^\//, ''), 'resource reference');
  return relative;
}

async function verifyResourceReferences(root, expectedOrigin) {
  const tree = await walk(root);
  const fileSet = new Set(tree.files);
  let checked = 0;
  function requireResource(raw, sourceRelative) {
    const relative = localResourcePath(raw, sourceRelative, expectedOrigin);
    if (relative === null || relative === '') return;
    assert(fileSet.has(relative), `Missing published resource: ${sourceRelative} -> ${raw}`);
    checked += 1;
  }
  for (const relative of tree.files) {
    if (relative.endsWith('.html')) {
      const html = await readFile(path.join(root, relative), 'utf8');
      for (const match of html.matchAll(/<(?:script|img|source)\b[^>]*\bsrc=["']([^"']+)["'][^>]*>/gi)) {
        requireResource(match[1], relative);
      }
      for (const match of html.matchAll(/\bsrcset=["']([^"']+)["']/gi)) {
        for (const item of match[1].split(',')) requireResource(item.trim().split(/\s+/)[0], relative);
      }
      for (const match of html.matchAll(/<link\b[^>]*>/gi)) {
        const tag = match[0];
        const rel = tag.match(/\brel=["']([^"']+)["']/i)?.[1]?.toLowerCase().split(/\s+/) ?? [];
        if (!rel.some((value) => ['stylesheet', 'icon', 'apple-touch-icon', 'preload', 'modulepreload'].includes(value))) continue;
        const href = tag.match(/\bhref=["']([^"']+)["']/i)?.[1] ?? '';
        requireResource(href, relative);
      }
      for (const match of html.matchAll(/<meta\b[^>]*>/gi)) {
        const tag = match[0];
        const marker = tag.match(/\b(?:property|name)=["']([^"']+)["']/i)?.[1]?.toLowerCase();
        if (!['og:image', 'twitter:image'].includes(marker)) continue;
        const content = tag.match(/\bcontent=["']([^"']+)["']/i)?.[1] ?? '';
        requireResource(content, relative);
      }
    } else if (relative.endsWith('.css')) {
      const css = await readFile(path.join(root, relative), 'utf8');
      for (const match of css.matchAll(/url\(([^)]+)\)/gi)) {
        requireResource(match[1].trim().replace(/^['"]|['"]$/g, ''), relative);
      }
    }
  }
  return checked;
}

async function verifyRoot(root, manifest, critical, routes, provenance, archiveDirectories) {
  const stat = await lstat(root);
  assert(stat.isDirectory(), `Deploy root is not a directory: ${root}`);
  const tree = await walk(root);
  assert(tree.symlinks.length === 0, `Deploy root contains symlinks: ${tree.symlinks.join(', ')}`);
  const expectedFiles = [...manifest.keys()].sort();
  assert(
    JSON.stringify(tree.files) === JSON.stringify(expectedFiles),
    'Deploy root file inventory differs from frozen artifact manifest',
  );
  assert(
    JSON.stringify(tree.directories) === JSON.stringify(archiveDirectories),
    'Deploy root directory inventory differs from frozen archive',
  );
  for (const relative of tree.files) {
    const stat = await lstat(path.join(root, relative));
    assert((stat.mode & 0o777) === 0o644, `Unexpected extracted file permissions: ${relative}`);
  }
  for (const relative of tree.directories) {
    const stat = await lstat(path.join(root, relative));
    assert((stat.mode & 0o777) === 0o755, `Unexpected extracted directory permissions: ${relative}`);
  }
  for (const [relative, digest] of manifest) {
    assert(await sha256File(path.join(root, relative)) === digest, `Artifact checksum mismatch: ${relative}`);
  }
  for (const [relative, digest] of critical) {
    assert(manifest.get(relative) === digest, `Critical file is not identically represented by full manifest: ${relative}`);
  }
  for (const required of [
    'index.html',
    '404.html',
    'robots.txt',
    'sitemap.xml',
    'llms.txt',
    'ar/index.html',
    'ar/404.html',
    'ar/robots.txt',
    'ar/sitemap.xml',
  ]) {
    assert(manifest.has(required), `Required static-root file missing from manifest: ${required}`);
  }
  assert(!manifest.has('ar/llms.txt'), 'Localized ar/llms.txt must remain absent');
  assert(Number(provenance.get('ARTIFACT_FILE_COUNT')) === manifest.size, 'Provenance artifact file count mismatch');
  assert(Number(provenance.get('CRITICAL_FILE_COUNT')) === critical.size, 'Provenance critical file count mismatch');
  assert(Number(provenance.get('ROUTE_COUNT')) === routes.length, 'Provenance route count mismatch');
  const referenceChecks = await verifyResourceReferences(root, provenance.get('PUBLIC_ORIGIN'));
  return {referenceChecks, fileCount: tree.files.length};
}

const {expected: packageContents, checksumDigest: packageContentsSha256} = await verifyPackageContents();
for (const required of [
  'PROVENANCE.txt',
  'ARTIFACT_MANIFEST.sha256',
  'CRITICAL_FILES.sha256',
  'ROUTES.txt',
  'VERIFICATION.txt',
  'READINESS_RESULT.json',
  'PUBLICATION_ELIGIBILITY.json',
  'OVERLAY_COMPOSITION.json',
  'OVERLAY_CONTROL.json',
  'RELEASED_CONTRACT_SNAPSHOT.json',
  'CONSUMER_HANDOFF.md',
  'consumer/scripts/verify-publication-package.mjs',
  'consumer/scripts/serve-publication-static.mjs',
  'consumer/scripts/check-publication-readiness.mjs',
  'consumer/src/content/answerAuthority.json',
]) {
  assert(packageContents.has(required), `Frozen package missing required handoff file: ${required}`);
}

const provenance = parseKeyValue(await readFile(path.join(packageDir, 'PROVENANCE.txt'), 'utf8'), 'provenance');
const eligibility = JSON.parse(await readFile(path.join(packageDir, 'PUBLICATION_ELIGIBILITY.json'), 'utf8'));
const composition = JSON.parse(await readFile(path.join(packageDir, 'OVERLAY_COMPOSITION.json'), 'utf8'));
const readiness = JSON.parse(await readFile(path.join(packageDir, 'READINESS_RESULT.json'), 'utf8'));
assert(eligibility.status === 'PASS', 'Publication eligibility receipt did not complete successfully');
assert(composition.status === 'PASS', 'Overlay composition receipt did not complete successfully');
assert(readiness.status === 'PASS', 'Publication readiness receipt is not PASS');
assert(
  await sha256File(path.join(packageDir, 'PUBLICATION_ELIGIBILITY.json')) === provenance.get('PUBLICATION_ELIGIBILITY_SHA256'),
  'Publication eligibility receipt digest mismatch',
);
assert(
  await sha256File(path.join(packageDir, 'OVERLAY_COMPOSITION.json')) === provenance.get('OVERLAY_COMPOSITION_SHA256'),
  'Overlay composition receipt digest mismatch',
);
assert(
  await sha256File(path.join(packageDir, 'OVERLAY_CONTROL.json')) === provenance.get('OVERLAY_CONTROL_SHA256'),
  'Overlay control manifest digest mismatch',
);
assert(
  await sha256File(path.join(packageDir, 'RELEASED_CONTRACT_SNAPSHOT.json')) === provenance.get('RELEASED_CONTRACT_SNAPSHOT_SHA256'),
  'Released-contract snapshot digest mismatch',
);
assert(
  await sha256File(path.join(packageDir, 'READINESS_RESULT.json')) === provenance.get('READINESS_RESULT_SHA256'),
  'Publication readiness receipt digest mismatch',
);
assert(eligibility.source?.commit === provenance.get('SOURCE_COMMIT'), 'Eligibility/source commit mismatch');
assert(eligibility.source?.tree === provenance.get('SOURCE_TREE'), 'Eligibility/source tree mismatch');
assert(eligibility.publicationClass === provenance.get('PUBLICATION_CLASS'), 'Publication class provenance mismatch');
assert(
  eligibility.publicationEligibility === provenance.get('PUBLICATION_ELIGIBILITY'),
  'Publication eligibility provenance mismatch',
);
assert(
  eligibility.releasedPublicationBaseline?.derivedAcceptedMainCommit === provenance.get('RELEASED_PUBLICATION_BASELINE_COMMIT'),
  'Released publication baseline commit provenance mismatch',
);
assert(
  eligibility.releasedPublicationBaseline?.derivedAcceptedMainTree === provenance.get('RELEASED_PUBLICATION_BASELINE_TREE'),
  'Released publication baseline tree provenance mismatch',
);
assert(
  eligibility.releasedPublicationBaseline?.controlIntroductionCommit === provenance.get('PUBLICATION_CONTROL_INTRODUCTION_COMMIT'),
  'Publication control introduction provenance mismatch',
);
assert(
  eligibility.releasedPublicationBaseline?.protectedBaseCommit === provenance.get('QUALIFICATION_PROTECTED_BASE_COMMIT'),
  'Qualification protected/base provenance mismatch',
);
assert(composition.publicOrigin === provenance.get('PUBLIC_ORIGIN'), 'Overlay composition origin mismatch');
assert(readiness.expectedOrigin === provenance.get('PUBLIC_ORIGIN'), 'Readiness origin mismatch');
assert(provenance.get('PRODUCTION_AUTHORIZED') === 'NO', 'Qualified package must not self-authorize Production');
for (const [relative, key] of [
  ['consumer/scripts/verify-publication-package.mjs', 'CONSUMER_PACKAGE_VERIFIER_SHA256'],
  ['consumer/scripts/serve-publication-static.mjs', 'CONSUMER_STATIC_HOST_SHA256'],
  ['consumer/scripts/check-publication-readiness.mjs', 'CONSUMER_READINESS_HARNESS_SHA256'],
  ['consumer/src/content/answerAuthority.json', 'CONSUMER_ANSWER_AUTHORITY_SHA256'],
  ['CONSUMER_HANDOFF.md', 'CONSUMER_HANDOFF_SHA256'],
]) {
  assert(provenance.get(key), `Provenance missing ${key}`);
  assert(await sha256File(path.join(packageDir, relative)) === provenance.get(key), `Consumer handoff digest mismatch: ${relative}`);
}
const archiveName = provenance.get('FROZEN_ARCHIVE');
assert(archiveName, 'Provenance missing FROZEN_ARCHIVE');
safeRelative(archiveName, 'archive filename');
const archivePath = path.join(packageDir, archiveName);
assert(await sha256File(archivePath) === provenance.get('FROZEN_ARCHIVE_SHA256'), 'Frozen archive digest does not match provenance');

const manifestPath = path.join(packageDir, 'ARTIFACT_MANIFEST.sha256');
const criticalPath = path.join(packageDir, 'CRITICAL_FILES.sha256');
const routesPath = path.join(packageDir, 'ROUTES.txt');
assert(await sha256File(manifestPath) === provenance.get('ARTIFACT_MANIFEST_SHA256'), 'Artifact manifest digest does not match provenance');
assert(await sha256File(criticalPath) === provenance.get('CRITICAL_FILES_SHA256'), 'Critical-files digest does not match provenance');
assert(await sha256File(routesPath) === provenance.get('ROUTE_INVENTORY_SHA256'), 'Route inventory digest does not match provenance');

const manifest = parseChecksumFile(await readFile(manifestPath, 'utf8'), 'artifact manifest');
const critical = parseChecksumFile(await readFile(criticalPath, 'utf8'), 'critical manifest');
const routes = routeInventory(await readFile(routesPath, 'utf8'), provenance.get('PUBLIC_ORIGIN'));
assert(
  composition.composedArtifact?.manifestSha256 === provenance.get('ARTIFACT_MANIFEST_SHA256'),
  'Overlay composition is not bound to the frozen artifact manifest',
);
assert(composition.composedArtifact?.files === manifest.size, 'Overlay composition file count mismatch');
assert(composition.routeInventorySha256 === provenance.get('ROUTE_INVENTORY_SHA256'), 'Overlay route inventory provenance mismatch');
assert(composition.redirects?.sha256 === provenance.get('COMPOSED_REDIRECTS_SHA256'), 'Composed redirect provenance mismatch');
assert(manifest.get('_redirects') === composition.redirects?.sha256, 'Frozen _redirects bytes do not match composition receipt');
for (const overlay of composition.overlayFiles ?? []) {
  assert(manifest.get(overlay.target) === overlay.sha256, `Frozen overlay file mismatch: ${overlay.target}`);
}
for (const [relative, digest] of critical) {
  assert(manifest.get(relative) === digest, `Critical file is missing or divergent in full manifest: ${relative}`);
}
for (const required of [
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
  assert(critical.has(required), `Critical freeze contract is missing required published file: ${required}`);
}
for (const [label, pattern] of [
  ['EN stylesheet entry', /^assets\/css\/styles\.[0-9a-f]+\.css$/],
  ['EN main JS entry', /^assets\/js\/main\.[0-9a-f]+\.js$/],
  ['EN runtime JS entry', /^assets\/js\/runtime~main\.[0-9a-f]+\.js$/],
  ['AR stylesheet entry', /^ar\/assets\/css\/styles\.[0-9a-f]+\.css$/],
  ['AR main JS entry', /^ar\/assets\/js\/main\.[0-9a-f]+\.js$/],
  ['AR runtime JS entry', /^ar\/assets\/js\/runtime~main\.[0-9a-f]+\.js$/],
]) {
  const matches = [...critical.keys()].filter((relative) => pattern.test(relative));
  assert(matches.length === 1, `Critical freeze contract must contain exactly one ${label}; found ${matches.length}`);
}

const archive = tarList(archivePath);
assert(
  JSON.stringify(archive.regularFiles) === JSON.stringify([...manifest.keys()].sort()),
  'Archive regular-file inventory differs from frozen artifact manifest',
);

if (extractDir) {
  try {
    await lstat(extractDir);
    fail(`Extraction target already exists: ${extractDir}`);
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
  }
  await mkdir(extractDir, {recursive: true});
  try {
    execFileSync('tar', ['-xzf', archivePath, '-C', extractDir, '--no-same-owner'], {stdio: 'inherit'});
  } catch (error) {
    await rm(extractDir, {recursive: true, force: true});
    throw error;
  }
}

let rootSummary = null;
if (rootToVerify) {
  rootSummary = await verifyRoot(rootToVerify, manifest, critical, routes, provenance, archive.directories);
}

console.log(JSON.stringify({
  status: 'PASS',
  packageDir,
  packageContentsSha256,
  archive: archiveName,
  archiveSha256: provenance.get('FROZEN_ARCHIVE_SHA256'),
  packageFilesVerified: packageContents.size,
  archiveEntries: archive.entryCount,
  archiveDirectories: archive.directories.length,
  artifactFiles: manifest.size,
  criticalFiles: critical.size,
  routes: routes.length,
  extractedRoot: rootToVerify,
  resourceReferencesChecked: rootSummary?.referenceChecks ?? null,
  developmentPathReferences: 0,
  symlinks: 0,
  absoluteOrTraversalPaths: 0,
  hiddenOrBuildCachePaths: 0,
}, null, 2));

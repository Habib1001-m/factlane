import {execFileSync} from 'node:child_process';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

import {
  assert,
  manifestForRoot,
  parseArgs,
  readJson,
  sha256File,
} from './publication-control-lib.mjs';

const args = parseArgs(process.argv.slice(2));
for (const name of [
  'package',
  'expected-package-contents-sha256',
  'production-authorization-sha256',
  'expected-origin',
  'expected-project',
]) {
  assert(args[name], `--${name} is required`);
}

const packageDir = path.resolve(args.package);
const expectedDigest = args['expected-package-contents-sha256'];
const authorizationDigest = args['production-authorization-sha256'];
assert(/^[0-9a-f]{64}$/.test(expectedDigest), 'Expected package digest must be lowercase SHA-256');
assert(/^[0-9a-f]{64}$/.test(authorizationDigest), 'Production authorization digest must be lowercase SHA-256');
assert(authorizationDigest === expectedDigest, 'Production authorization is not bound to this exact package digest');

const actualDigest = await sha256File(path.join(packageDir, 'PACKAGE_CONTENTS.sha256'));
assert(actualDigest === expectedDigest, 'Package trust anchor does not match the owner-authorized digest');

const expectedPackageFiles = new Map();
for (const line of (await readFile(path.join(packageDir, 'PACKAGE_CONTENTS.sha256'), 'utf8')).split(/\r?\n/)) {
  if (!line) continue;
  const match = line.match(/^([0-9a-f]{64})  \.\/(.+)$/);
  assert(match, `Malformed package checksum line: ${line}`);
  assert(!expectedPackageFiles.has(match[2]), `Duplicate package checksum path: ${match[2]}`);
  expectedPackageFiles.set(match[2], match[1]);
}
const packageTree = await manifestForRoot(packageDir);
const actualPackageFiles = [...packageTree.manifest.keys()]
  .filter((relative) => relative !== 'PACKAGE_CONTENTS.sha256')
  .sort();
assert(
  JSON.stringify(actualPackageFiles) === JSON.stringify([...expectedPackageFiles.keys()].sort()),
  'Package file inventory does not match authenticated PACKAGE_CONTENTS.sha256',
);
for (const [relative, digest] of expectedPackageFiles) {
  assert(packageTree.manifest.get(relative) === digest, `Authenticated package file mismatch: ${relative}`);
}

const provenance = new Map();
for (const line of (await readFile(path.join(packageDir, 'PROVENANCE.txt'), 'utf8')).split(/\r?\n/)) {
  if (!line) continue;
  const index = line.indexOf('=');
  assert(index > 0, `Malformed provenance line: ${line}`);
  const key = line.slice(0, index);
  assert(!provenance.has(key), `Duplicate provenance key: ${key}`);
  provenance.set(key, line.slice(index + 1));
}
const eligibility = await readJson(path.join(packageDir, 'PUBLICATION_ELIGIBILITY.json'));

assert(provenance.get('PUBLIC_ORIGIN') === args['expected-origin'], 'Authorized Production origin mismatch');
assert(provenance.get('CLOUDFLARE_PROJECT') === args['expected-project'], 'Authorized Cloudflare project mismatch');
assert(provenance.get('CLOUDFLARE_DEPLOYMENT_METHOD') === 'PAGES_DIRECT_UPLOAD', 'Deployment primitive drifted');
assert(provenance.get('CLOUDFLARE_GIT_SOURCE_REQUIRED') === 'NO', 'Qualified package unexpectedly requires Git-connected deployment');
assert(provenance.get('PRODUCTION_AUTHORIZED') === 'NO', 'Package must never self-authorize Production');
assert(eligibility.status === 'PASS', 'Publication eligibility evaluation did not complete');
assert(
  eligibility.publicationEligibility === 'ELIGIBLE_PENDING_OTHER_GATES',
  `Publication is HOLD: ${eligibility.publicationEligibility}`,
);
assert(
  ['RELEASE_NEUTRAL'].includes(eligibility.publicationClass),
  `Publication class is not deploy-eligible under the current released authority: ${eligibility.publicationClass}`,
);

const archive = provenance.get('FROZEN_ARCHIVE');
const archiveSha256 = provenance.get('FROZEN_ARCHIVE_SHA256');
assert(archive && archiveSha256, 'Frozen archive provenance is incomplete');
assert(await sha256File(path.join(packageDir, archive)) === archiveSha256, 'Frozen archive digest does not match authenticated provenance');

const verificationRoot = await mkdtemp(path.join(os.tmpdir(), 'factlane-crossing-verify-'));
try {
  execFileSync(process.execPath, [
    path.join(packageDir, 'consumer', 'scripts', 'verify-publication-package.mjs'),
    '--package', packageDir,
    '--expected-package-contents-sha256', expectedDigest,
    '--extract', verificationRoot,
  ], {stdio: ['ignore', 'pipe', 'pipe']});
} finally {
  await rm(verificationRoot, {recursive: true, force: true});
}

console.log(JSON.stringify({
  status: 'PASS',
  gate: 'OWNER_AUTHORIZED_EXACT_PACKAGE_READY_FOR_PREDEPLOY_EXTERNAL_STATE_CHECKS',
  packageContentsSha256: actualDigest,
  sourceCommit: provenance.get('SOURCE_COMMIT'),
  sourceTree: provenance.get('SOURCE_TREE'),
  publicOrigin: provenance.get('PUBLIC_ORIGIN'),
  cloudflareProject: provenance.get('CLOUDFLARE_PROJECT'),
  archive,
  archiveSha256,
  productionDeploymentPerformed: false,
}, null, 2));

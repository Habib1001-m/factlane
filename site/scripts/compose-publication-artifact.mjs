import {mkdir, readFile, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

import {
  assert,
  checksumText,
  copyFresh,
  exists,
  manifestForRoot,
  parseArgs,
  readJson,
  safeRelative,
  sha256File,
  sha256Text,
  writeJson,
} from './publication-control-lib.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const siteRoot = path.resolve(here, '..');
const args = parseArgs(process.argv.slice(2));
const baseRoot = path.resolve(args.base ?? '');
const outputRoot = path.resolve(args.output ?? '');
const receiptPath = path.resolve(args.receipt ?? path.join(outputRoot, '..', 'OVERLAY_COMPOSITION.json'));
const routesPath = path.resolve(args.routes ?? path.join(outputRoot, '..', 'ROUTES.txt'));
const origin = new URL(args.origin ?? '').origin;
const manifestPath = path.resolve(args.manifest ?? path.join(siteRoot, 'publication', 'overlays', 'manifest.json'));
const overlayRoot = path.dirname(manifestPath);
const publicationRoot = path.resolve(siteRoot, 'publication');

assert(args.base, '--base is required');
assert(args.output, '--output is required');
assert(args.origin, '--origin is required');
assert(new URL(args.origin).href === `${origin}/`, '--origin must be an HTTPS origin without path/query/fragment');
assert(origin.startsWith('https://'), '--origin must use HTTPS');
assert(!(await exists(outputRoot)), `Output root must not already exist: ${outputRoot}`);

const control = await readJson(manifestPath);
assert(control.schemaVersion === 1, `Unsupported overlay manifest schema: ${control.schemaVersion}`);
assert(control.publicOrigin === origin, `Overlay origin mismatch: ${control.publicOrigin} != ${origin}`);
assert(Array.isArray(control.files) && control.files.length > 0, 'Overlay manifest requires files');
assert(Array.isArray(control.redirectFragments) && control.redirectFragments.length > 0, 'Overlay manifest requires redirect fragments');

const {manifest: baseManifest} = await manifestForRoot(baseRoot);
const targets = new Set();
for (const entry of control.files) {
  const target = safeRelative(entry.target, 'overlay target');
  assert(!targets.has(target), `Duplicate overlay target: ${target}`);
  targets.add(target);
  assert(!baseManifest.has(target), `Overlay target collides with build output: ${target}`);
  assert(/^[0-9a-f]{64}$/.test(entry.sha256 ?? ''), `Invalid overlay digest for ${target}`);
}
assert(!baseManifest.has('_redirects'), 'Base build must not contain _redirects; host routing belongs to the publication overlay');

await copyFresh(baseRoot, outputRoot);

const overlayFiles = [];
for (const entry of control.files) {
  const source = path.resolve(overlayRoot, safeRelative(entry.source, 'overlay source'));
  assert(source.startsWith(`${publicationRoot}${path.sep}`), `Overlay source escaped publication control root: ${entry.source}`);
  const target = safeRelative(entry.target, 'overlay target');
  assert(await sha256File(source) === entry.sha256, `Overlay source digest mismatch: ${entry.source}`);
  const destination = path.join(outputRoot, target);
  await mkdir(path.dirname(destination), {recursive: true});
  const bytes = await readFile(source);
  await writeFile(destination, bytes, {mode: 0o644});
  assert(await sha256File(destination) === entry.sha256, `Overlay target digest mismatch after copy: ${target}`);
  overlayFiles.push({kind: entry.kind, target, sha256: entry.sha256});
}

const redirectSources = new Set();
const redirectLines = [];
const redirectFragments = [];
for (const fragment of control.redirectFragments) {
  const source = path.resolve(overlayRoot, fragment.source);
  assert(source.startsWith(`${publicationRoot}${path.sep}`), `Redirect source escaped publication control root: ${fragment.source}`);
  const text = await readFile(source, 'utf8');
  assert(await sha256File(source) === fragment.sha256, `Redirect fragment digest mismatch: ${fragment.source}`);
  const lines = text.split(/\r?\n/).filter(Boolean);
  assert(lines.length === fragment.expectedRuleCount, `Redirect rule count mismatch: ${fragment.source}`);
  for (const line of lines) {
    const fields = line.trim().split(/\s+/);
    assert(fields.length === 3, `Malformed redirect rule: ${line}`);
    const [from, to, status] = fields;
    assert(from.startsWith('/') && to.startsWith('/'), `Redirect rule must remain same-origin: ${line}`);
    assert(['200', '301'].includes(status), `Unsupported redirect status: ${line}`);
    assert(!redirectSources.has(from), `Duplicate redirect source: ${from}`);
    redirectSources.add(from);
    redirectLines.push(`${from} ${to} ${status}`);
  }
  redirectFragments.push({
    kind: fragment.kind,
    source: fragment.source,
    sha256: fragment.sha256,
    ruleCount: lines.length,
  });
}
const redirectsText = `${redirectLines.join('\n')}\n`;
await writeFile(path.join(outputRoot, '_redirects'), redirectsText, {encoding: 'utf8', mode: 0o644});

const {manifest: composedManifest} = await manifestForRoot(outputRoot);
const composedManifestText = checksumText(composedManifest);
const routes = [];
for (const sitemapRelative of ['sitemap.xml', 'ar/sitemap.xml']) {
  const xml = await readFile(path.join(outputRoot, sitemapRelative), 'utf8');
  for (const match of xml.matchAll(/<loc>([^<]+)<\/loc>/g)) {
    const value = match[1].trim();
    const parsed = new URL(value);
    assert(parsed.origin === origin, `Sitemap origin mismatch: ${value}`);
    assert(parsed.search === '' && parsed.hash === '', `Sitemap URL is not canonical: ${value}`);
    routes.push(value);
  }
}
const uniqueRoutes = [...new Set(routes)].sort();
assert(uniqueRoutes.length === routes.length, 'Sitemap route inventory contains duplicates');
assert(uniqueRoutes.length > 0, 'Route inventory is empty');
await writeFile(routesPath, `${uniqueRoutes.join('\n')}\n`, {encoding: 'utf8', mode: 0o644});

const receipt = {
  status: 'PASS',
  schemaVersion: 1,
  publicOrigin: origin,
  overlayManifestSha256: await sha256File(manifestPath),
  baseArtifact: {
    files: baseManifest.size,
    manifestSha256: sha256Text(checksumText(baseManifest)),
  },
  redirects: {
    fragments: redirectFragments,
    rules: redirectLines.length,
    sha256: sha256Text(redirectsText),
  },
  overlayFiles,
  composedArtifact: {
    files: composedManifest.size,
    manifestSha256: sha256Text(composedManifestText),
  },
  routes: uniqueRoutes,
  routeInventorySha256: sha256Text(`${uniqueRoutes.join('\n')}\n`),
};
await writeJson(receiptPath, receipt);
console.log(JSON.stringify(receipt, null, 2));

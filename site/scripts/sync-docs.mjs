import {cp, mkdir, readdir, readFile, rm, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const siteRoot = path.resolve(here, '..');
const repoRoot = path.resolve(siteRoot, '..');
const sourceDocs = path.join(repoRoot, 'docs');
const output = path.join(siteRoot, '.generated-docs');

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

console.log('Synced canonical FactLane docs into site/.generated-docs');

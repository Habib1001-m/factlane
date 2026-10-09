import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {chmod, cp, lstat, mkdir, readFile, readdir, rm, writeFile} from 'node:fs/promises';
import path from 'node:path';

export function fail(message) {
  throw new Error(message);
}

export function assert(condition, message) {
  if (!condition) fail(message);
}

export function parseArgs(argv) {
  const out = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    assert(token.startsWith('--'), `Unexpected argument: ${token}`);
    const key = token.slice(2);
    const value = argv[index + 1];
    assert(value && !value.startsWith('--'), `Missing value for --${key}`);
    assert(!(key in out), `Duplicate argument: --${key}`);
    out[key] = value;
    index += 1;
  }
  return out;
}

export function sha256Buffer(value) {
  return createHash('sha256').update(value).digest('hex');
}

export function sha256Text(value) {
  return sha256Buffer(Buffer.from(value, 'utf8'));
}

export async function sha256File(file) {
  return sha256Buffer(await readFile(file));
}

export function git(cwd, args) {
  return execFileSync('git', args, {
    cwd,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
}

export async function exists(target) {
  try {
    await lstat(target);
    return true;
  } catch (error) {
    if (error?.code === 'ENOENT') return false;
    throw error;
  }
}

export function safeRelative(value, label = 'path') {
  assert(typeof value === 'string' && value.length > 0, `${label} must be a non-empty string`);
  assert(!path.posix.isAbsolute(value), `${label} must be relative: ${value}`);
  assert(!value.includes('\\'), `${label} must use POSIX separators: ${value}`);
  const normalized = value.replace(/^\.\//, '').replace(/\/$/, '');
  assert(normalized.length > 0, `${label} may not be artifact root`);
  const parts = normalized.split('/');
  assert(parts.every((part) => part && part !== '.' && part !== '..'), `${label} escapes root: ${value}`);
  return normalized;
}

export async function walk(root) {
  const files = [];
  const directories = [];
  const symlinks = [];
  async function visit(current, relative = '') {
    for (const entry of await readdir(current, {withFileTypes: true})) {
      const rel = relative ? `${relative}/${entry.name}` : entry.name;
      const full = path.join(current, entry.name);
      const stat = await lstat(full);
      if (stat.isSymbolicLink()) {
        symlinks.push(rel);
      } else if (stat.isDirectory()) {
        directories.push(rel);
        await visit(full, rel);
      } else if (stat.isFile()) {
        files.push(rel);
      } else {
        fail(`Unsupported filesystem object: ${rel}`);
      }
    }
  }
  await visit(root);
  return {
    files: files.sort(),
    directories: directories.sort(),
    symlinks: symlinks.sort(),
  };
}

export async function manifestForRoot(root) {
  const tree = await walk(root);
  assert(tree.symlinks.length === 0, `Symlinks are forbidden: ${tree.symlinks.join(', ')}`);
  const manifest = new Map();
  for (const relative of tree.files) {
    manifest.set(relative, await sha256File(path.join(root, relative)));
  }
  return {manifest, tree};
}

export function checksumText(manifest) {
  return [...manifest.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([relative, digest]) => `${digest}  ./${relative}\n`)
    .join('');
}

export async function normalizeTreePermissions(root) {
  const tree = await walk(root);
  assert(tree.symlinks.length === 0, `Symlinks are forbidden: ${tree.symlinks.join(', ')}`);
  for (const relative of tree.directories) await chmod(path.join(root, relative), 0o755);
  for (const relative of tree.files) await chmod(path.join(root, relative), 0o644);
}

export async function copyFresh(source, target) {
  assert(await exists(source), `Source path does not exist: ${source}`);
  assert(!(await exists(target)), `Target must not already exist: ${target}`);
  await mkdir(path.dirname(target), {recursive: true});
  await cp(source, target, {recursive: true, force: false, errorOnExist: true});
  await normalizeTreePermissions(target);
}

export async function writeJson(file, value) {
  await mkdir(path.dirname(file), {recursive: true});
  await writeFile(file, `${JSON.stringify(value, null, 2)}\n`, {encoding: 'utf8', mode: 0o644});
}

export async function readJson(file) {
  return JSON.parse(await readFile(file, 'utf8'));
}

export async function resetDirectory(target) {
  await rm(target, {recursive: true, force: true});
  await mkdir(target, {recursive: true});
}

export async function regularFile(target, label = target) {
  const stat = await lstat(target);
  assert(stat.isFile() && !stat.isSymbolicLink(), `${label} must be a regular file`);
  return stat;
}

import {createReadStream} from 'node:fs';
import {lstat, readFile, realpath} from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';

function fail(message) {
  console.error(`STATIC_PUBLICATION_REHEARSAL=HOLD ${message}`);
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
    const value = argv[index + 1];
    if (!value || value.startsWith('--')) fail(`Missing value for ${token}`);
    result[token.slice(2)] = value;
    index += 1;
  }
  return result;
}

const args = parseArgs(process.argv.slice(2));
const root = path.resolve(args.root ?? '');
const routesPath = path.resolve(args.routes ?? '');
const host = args.host ?? '127.0.0.1';
const port = Number(args.port ?? '4260');

assert(host === '127.0.0.1' || host === '::1', 'Rehearsal static host is loopback-only');
assert(Number.isInteger(port) && port >= 1024 && port <= 65535, `Invalid port: ${args.port}`);

const rootStat = await lstat(root);
assert(rootStat.isDirectory(), `Static root is not a directory: ${root}`);
for (const required of ['index.html', '404.html', 'robots.txt', 'sitemap.xml', 'llms.txt', 'ar/index.html']) {
  let stat;
  try {
    stat = await lstat(path.join(root, required));
  } catch (error) {
    if (error?.code === 'ENOENT') fail(`Wrong deploy root; required file not at root: ${required}`);
    throw error;
  }
  assert(stat.isFile() && !stat.isSymbolicLink(), `Wrong deploy root; required file is not a regular file: ${required}`);
}
const realRoot = await realpath(root);

const routeUrls = (await readFile(routesPath, 'utf8')).split(/\r?\n/).filter(Boolean);
assert(routeUrls.length > 0, 'Route inventory is empty');
const canonicalRoutes = new Set(routeUrls.map((value) => new URL(value).pathname));

function htmlAlias(route) {
  return route === '/' ? '/index.html' : `${route.slice(0, -1)}.html`;
}

function indexHtmlAlias(route) {
  return route === '/' ? '/index.html' : `${route}index.html`;
}

function duplicateSlashVariant(route) {
  if (route === '/') return '//';
  return route.replace(/^\//, '//').replace('/docs/', '//docs//');
}

const aliases = new Map();
for (const route of canonicalRoutes) {
  if (route !== '/') aliases.set(route.slice(0, -1), route);
  aliases.set(htmlAlias(route), route);
  aliases.set(indexHtmlAlias(route), route);
  aliases.set(duplicateSlashVariant(route), route);
}

function contentType(filePath) {
  const extension = path.extname(filePath).toLowerCase();
  return {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.svg': 'image/svg+xml',
    '.xml': 'application/xml; charset=utf-8',
    '.txt': 'text/plain; charset=utf-8',
  }[extension] ?? 'application/octet-stream';
}

function canonicalFile(route) {
  return route === '/' ? 'index.html' : `${route.replace(/^\//, '')}index.html`;
}

async function safeFile(relative) {
  const decoded = decodeURIComponent(relative).replace(/^\/+/, '');
  assert(!decoded.includes('\\'), 'Backslash path is outside static-host contract');
  const segments = decoded.split('/').filter(Boolean);
  assert(segments.every((segment) => segment !== '.' && segment !== '..'), 'Traversal path rejected');
  const candidate = path.resolve(root, ...segments);
  assert(candidate === root || candidate.startsWith(`${root}${path.sep}`), 'Path escapes static root');
  let stat;
  try {
    stat = await lstat(candidate);
  } catch (error) {
    if (error?.code === 'ENOENT') return null;
    throw error;
  }
  if (!stat.isFile() || stat.isSymbolicLink()) return null;
  const actual = await realpath(candidate);
  assert(actual === realRoot || actual.startsWith(`${realRoot}${path.sep}`), 'Resolved file escapes static root');
  return {candidate, stat};
}

async function sendFile(response, relative, status = 200) {
  const resolved = await safeFile(relative);
  if (!resolved) return false;
  response.writeHead(status, {
    'content-type': contentType(resolved.candidate),
    'content-length': resolved.stat.size,
    'cache-control': 'no-store',
  });
  createReadStream(resolved.candidate).pipe(response);
  return true;
}

async function send404(response, rawPath) {
  const relative = rawPath.startsWith('/ar/') ? 'ar/404.html' : '404.html';
  if (!(await sendFile(response, relative, 404))) {
    response.writeHead(404, {'content-type': 'text/plain; charset=utf-8'});
    response.end('Not Found\n');
  }
}

const server = http.createServer(async (request, response) => {
  try {
    const rawTarget = request.url ?? '/';
    const queryIndex = rawTarget.indexOf('?');
    const rawPath = queryIndex === -1 ? rawTarget : rawTarget.slice(0, queryIndex);
    if (aliases.has(rawPath) && rawPath !== aliases.get(rawPath)) {
      response.writeHead(301, {location: aliases.get(rawPath)});
      response.end();
      return;
    }
    if (canonicalRoutes.has(rawPath)) {
      await sendFile(response, canonicalFile(rawPath));
      return;
    }
    const staticRelative = rawPath.replace(/^\//, '');
    if (staticRelative && await sendFile(response, staticRelative)) return;
    await send404(response, rawPath);
  } catch (error) {
    response.writeHead(400, {'content-type': 'text/plain; charset=utf-8'});
    response.end(`Bad Request: ${error.message}\n`);
  }
});

server.listen(port, host, () => {
  console.log(`STATIC_PUBLICATION_REHEARSAL_READY=http://${host.includes(':') ? `[${host}]` : host}:${port}`);
  console.log(`STATIC_PUBLICATION_ROOT=${root}`);
});

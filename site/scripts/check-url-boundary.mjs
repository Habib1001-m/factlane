import {readFile, readdir} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const siteRoot = path.resolve(here, '..');
const buildRoot = path.resolve(siteRoot, process.env.FACTLANE_BUILD_DIR ?? 'build');
const httpBase = process.env.FACTLANE_HTTP_BASE;
const mode = process.env.FACTLANE_SEO_MODE ?? 'public';
const configuredSiteUrl = process.env.FACTLANE_SITE_URL ?? 'https://factlane.local';
const origin = new URL(configuredSiteUrl).origin;

if (!httpBase) throw new Error('FACTLANE_HTTP_BASE is required');
if (!['public', 'local'].includes(mode)) {
  throw new Error('FACTLANE_SEO_MODE must be exactly public or local');
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
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

function firstMatch(html, regex) {
  const match = html.match(regex);
  return match ? decodeHtml(match[1].trim()) : '';
}

function parseHtml(html) {
  return {
    title: firstMatch(html, /<title[^>]*>([^<]*)<\/title>/),
    canonical: firstMatch(html, /<link[^>]+rel="canonical"[^>]+href="([^"]+)"[^>]*>/),
    robots: firstMatch(html, /<meta[^>]+name="robots"[^>]+content="([^"]*)"[^>]*>/),
    htmlLang: firstMatch(html, /<html[^>]+lang="([^"]+)"[^>]*>/),
    h1Count: [...html.matchAll(/<h1\b/g)].length,
    hasMain: /<main\b[\s\S]*?<\/main>/.test(html),
  };
}

async function sitemapUrls(file) {
  const xml = await readFile(file, 'utf8');
  return [...xml.matchAll(/<loc>([^<]+)<\/loc>/g)].map((match) => decodeHtml(match[1]));
}

async function discoverIndexFiles(root, prefix = '') {
  const out = [];
  for (const entry of await readdir(root, {withFileTypes: true})) {
    if (entry.name === 'assets') continue;
    const absolute = path.join(root, entry.name);
    const relative = path.posix.join(prefix, entry.name);
    if (entry.isDirectory()) {
      out.push(...(await discoverIndexFiles(absolute, relative)));
    } else if (entry.isFile() && entry.name === 'index.html') {
      out.push({absolute, relative});
    }
  }
  return out;
}

function requestUrl(pathname) {
  return `${httpBase}${pathname}`;
}

async function fetchOnce(pathname) {
  const response = await fetch(requestUrl(pathname), {
    redirect: 'manual',
    headers: {
      accept: 'text/html',
      'user-agent': 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
    },
  });
  const body = await response.text();
  return {
    path: pathname,
    status: response.status,
    location: response.headers.get('location'),
    contentType: response.headers.get('content-type') ?? '',
    body,
    page: parseHtml(body),
  };
}

async function fetchChain(start, maxRedirects = 4) {
  const chain = [];
  const seen = new Set();
  let current = start;
  for (let step = 0; step <= maxRedirects; step += 1) {
    assert(!seen.has(current), `Redirect loop from ${start}: ${current}`);
    seen.add(current);
    const result = await fetchOnce(current);
    chain.push(result);
    if (![301, 302, 303, 307, 308].includes(result.status)) return chain;
    assert(result.location, `Redirect without Location: ${current}`);
    const next = new URL(result.location, requestUrl(current));
    assert(next.origin === new URL(httpBase).origin, `Unexpected cross-origin redirect from ${current}: ${result.location}`);
    current = `${next.pathname}${next.search}`;
  }
  throw new Error(`Redirect chain too long from ${start}`);
}

function routeFromCanonical(canonical) {
  const parsed = new URL(canonical);
  assert(parsed.origin === origin, `Unexpected canonical origin: ${canonical}`);
  return parsed.pathname;
}

function slashToggle(route) {
  if (route === '/') return null;
  return route.endsWith('/') ? route.slice(0, -1) : `${route}/`;
}

function htmlAlias(route) {
  if (route === '/') return '/index.html';
  return route.endsWith('/') ? `${route.slice(0, -1)}.html` : `${route}.html`;
}

function indexHtmlAlias(route) {
  if (route === '/') return '/index.html';
  return route.endsWith('/') ? `${route}index.html` : `${route}/index.html`;
}

function caseVariant(route) {
  if (route === '/') return null;
  const parts = route.split('/');
  const lastIndex = route.endsWith('/') ? parts.length - 2 : parts.length - 1;
  const last = parts[lastIndex];
  if (!last) return null;
  parts[lastIndex] = /[A-Z]/.test(last) ? last.toLowerCase() : last.toUpperCase();
  return parts.join('/');
}

function duplicateSlashVariant(route) {
  if (route === '/') return '//';
  return route.replace(/^\//, '//').replace('/docs/', '//docs//');
}

const indexFiles = await discoverIndexFiles(buildRoot);
assert(indexFiles.length === 26, `Expected 26 index pages, found ${indexFiles.length}`);

const canonicalPages = [];
for (const file of indexFiles) {
  const page = parseHtml(await readFile(file.absolute, 'utf8'));
  assert(page.canonical, `Missing canonical in ${file.relative}`);
  canonicalPages.push({route: routeFromCanonical(page.canonical), canonical: page.canonical, file: file.relative});
}

const routeOwners = new Map();
for (const page of canonicalPages) {
  assert(!routeOwners.has(page.route), `Route collision: ${page.route}`);
  routeOwners.set(page.route, page.file);
}
const canonicalRoutes = [...routeOwners.keys()].sort();
assert(canonicalRoutes.includes('/'), 'Missing EN landing route');
assert(canonicalRoutes.includes('/ar/'), 'Missing AR landing route');
for (const route of canonicalRoutes) {
  assert(
    route === '/' || route.endsWith('/'),
    `Canonical route must use the explicit trailing-slash policy: ${route}`,
  );
}

if (mode === 'public') {
  const sitemap = [
    ...(await sitemapUrls(path.join(buildRoot, 'sitemap.xml'))),
    ...(await sitemapUrls(path.join(buildRoot, 'ar', 'sitemap.xml'))),
  ].sort();
  const expected = canonicalRoutes.map((route) => `${origin}${route}`).sort();
  assert(JSON.stringify(sitemap) === JSON.stringify(expected), 'Public sitemap URLs must exactly match built canonical routes');
} else {
  const rootEntries = await readdir(buildRoot);
  const arEntries = await readdir(path.join(buildRoot, 'ar'));
  assert(!rootEntries.includes('sitemap.xml'), 'Local EN sitemap must be absent');
  assert(!arEntries.includes('sitemap.xml'), 'Local AR sitemap must be absent');
}

let duplicate200 = 0;
let maxRedirects = 0;
let checkedVariants = 0;
let errorRoutes = 0;

for (const route of canonicalRoutes) {
  const expectedCanonical = `${origin}${route}`;
  const canonical = await fetchChain(route);
  assert(canonical.length === 1, `Canonical route redirected: ${route}`);
  assert(canonical[0].status === 200, `Canonical route is not 200: ${route}`);
  assert(canonical[0].contentType.includes('text/html'), `Canonical route is not HTML: ${route}`);
  assert(canonical[0].page.canonical === expectedCanonical, `Canonical link mismatch at ${route}: ${canonical[0].page.canonical}`);
  assert(canonical[0].page.hasMain, `Missing SSR main at ${route}`);
  assert(canonical[0].page.h1Count === 1, `Expected one H1 at ${route}`);
  if (mode === 'public') {
    assert(!canonical[0].page.robots.toLowerCase().includes('noindex'), `Public canonical route is noindex: ${route}`);
  } else {
    assert(canonical[0].page.robots === 'noindex,nofollow,noarchive', `Local canonical robots mismatch: ${route}`);
  }

  const variants = new Set([
    slashToggle(route),
    htmlAlias(route),
    indexHtmlAlias(route),
    `${route}${route.includes('?') ? '&' : '?'}r15=1`,
    duplicateSlashVariant(route),
  ].filter(Boolean));

  for (const variant of variants) {
    if (variant === route) continue;
    const chain = await fetchChain(variant);
    checkedVariants += 1;
    maxRedirects = Math.max(maxRedirects, chain.length - 1);
    const first = chain[0];
    const final = chain.at(-1);
    if (first.status === 200) duplicate200 += 1;
    assert(final.status === 200, `Alias did not resolve to 200: ${variant}`);
    assert(final.page.canonical === expectedCanonical, `Alias canonical mismatch ${variant}: ${final.page.canonical} != ${expectedCanonical}`);
    if (mode === 'public') {
      assert(!final.page.robots.toLowerCase().includes('noindex'), `Public canonical content became noindex through alias: ${variant}`);
    } else {
      assert(final.page.robots === 'noindex,nofollow,noarchive', `Local alias robots mismatch: ${variant}`);
    }
  }

  const wrongCase = caseVariant(route);
  if (wrongCase && wrongCase !== route) {
    const result = await fetchChain(wrongCase);
    errorRoutes += 1;
    const final = result.at(-1);
    assert(final.status === 404, `Case variant must end 404: ${wrongCase}`);
    assert(final.page.robots === 'noindex,nofollow,noarchive', `Case-variant 404 must be noindex: ${wrongCase}`);
  }

  const missing = route === '/' ? '/__R15_MISSING__/' : `${route}__R15_MISSING__/`;
  const missingResult = await fetchChain(missing);
  errorRoutes += 1;
  const missingFinal = missingResult.at(-1);
  assert(missingFinal.status === 404, `Missing route must end 404: ${missing}`);
  assert(missingFinal.page.robots === 'noindex,nofollow,noarchive', `Missing route 404 must be noindex: ${missing}`);
}

for (const prefixed of ['/en/', '/en/docs/', '/en/docs/ARCHITECTURE']) {
  const result = await fetchChain(prefixed);
  errorRoutes += 1;
  const final = result.at(-1);
  assert(final.status === 404, `Default-locale prefix must end 404: ${prefixed}`);
  assert(final.page.robots === 'noindex,nofollow,noarchive', `Default-locale 404 must be noindex: ${prefixed}`);
}

for (const errorPath of ['/404', '/ar/404', '/404.html', '/ar/404.html']) {
  const chain = await fetchChain(errorPath);
  checkedVariants += 1;
  maxRedirects = Math.max(maxRedirects, chain.length - 1);
  const final = chain.at(-1);
  assert(final.status === 200, `Explicit error artifact must resolve: ${errorPath}`);
  assert(final.page.robots === 'noindex,nofollow,noarchive', `Explicit error artifact must be noindex: ${errorPath}`);
  assert(/Not Found|غير موجودة/u.test(final.page.title), `Explicit error artifact must remain a not-found page: ${errorPath}`);
}

const robots = await readFile(path.join(buildRoot, 'robots.txt'), 'utf8');
if (mode === 'public') {
  assert(/(^|\n)Allow: \/($|\n)/.test(robots), 'Public robots must Allow: /');
  assert(robots.includes(`Sitemap: ${origin}/sitemap.xml`), 'Public EN sitemap declaration missing');
  assert(robots.includes(`Sitemap: ${origin}/ar/sitemap.xml`), 'Public AR sitemap declaration missing');
} else {
  assert(/(^|\n)Disallow: \/($|\n)/.test(robots), 'Local robots must Disallow: /');
}

assert(maxRedirects <= 3, `Unexpected redirect chain longer than 3 hops: ${maxRedirects}`);

console.log([
  'URL boundary audit passed',
  `mode=${mode}`,
  `canonicalRoutes=${canonicalRoutes.length}`,
  `variants=${checkedVariants}`,
  `errorRoutes=${errorRoutes}`,
  `duplicate200WithCanonical=${duplicate200}`,
  `maxRedirects=${maxRedirects}`,
].join('; '));

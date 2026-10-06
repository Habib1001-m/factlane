import {readFile, readdir} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const siteRoot = path.resolve(here, '..');
const buildRoot = path.resolve(siteRoot, process.env.FACTLANE_BUILD_DIR ?? 'build');
const mode = process.env.FACTLANE_SEO_MODE ?? 'public';
const configuredSiteUrl = process.env.FACTLANE_SITE_URL ?? 'https://factlane.local';
const origin = new URL(configuredSiteUrl).origin;
const docSeo = JSON.parse(
  await readFile(path.join(siteRoot, 'src', 'content', 'docSeo.json'), 'utf8'),
);

if (!['public', 'local'].includes(mode)) {
  throw new Error('FACTLANE_SEO_MODE must be exactly public or local');
}

const docIds = [
  'INTRO',
  'USE_CASES',
  'FAQ',
  'CORE_CONCEPTS',
  'QUICKSTART',
  'TOOLS',
  'USING_FACTLANE_SKILL',
  'ARCHITECTURE',
  'ENVIRONMENT',
  'SECURITY',
  'RELEASE_OPERATIONS',
  'PROJECT_HISTORY',
];

const locales = ['en', 'ar'];

async function discoverDocIds(locale) {
  const localeRoot = locale === 'en' ? buildRoot : path.join(buildRoot, 'ar');
  const docsRoot = path.join(localeRoot, 'docs');
  const entries = await readdir(docsRoot, {withFileTypes: true});
  return [
    'INTRO',
    ...entries.filter((entry) => entry.isDirectory()).map((entry) => entry.name),
  ].sort();
}

function routePath(locale, id) {
  const localePrefix = locale === 'en' ? '' : '/ar';
  return id === 'INTRO' ? `${localePrefix}/docs/` : `${localePrefix}/docs/${id}`;
}

function htmlPath(locale, id) {
  const localeRoot = locale === 'en' ? buildRoot : path.join(buildRoot, 'ar');
  return id === 'INTRO'
    ? path.join(localeRoot, 'docs', 'index.html')
    : path.join(localeRoot, 'docs', id, 'index.html');
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

function allMatches(html, regex) {
  return [...html.matchAll(regex)].map((match) => decodeHtml(match[1].trim()));
}

function parsePage(html) {
  const alternateMatches = [...html.matchAll(/<link[^>]+rel="alternate"[^>]+href="([^"]+)"[^>]+hreflang="([^"]+)"[^>]*>/g)];
  const alternates = Object.fromEntries(alternateMatches.map((match) => [match[2], decodeHtml(match[1])]));
  const jsonLd = [];
  for (const match of html.matchAll(/<script[^>]+type="application\/ld\+json"[^>]*>([\s\S]*?)<\/script>/g)) {
    jsonLd.push(JSON.parse(match[1]));
  }

  return {
    title: firstMatch(html, /<title[^>]*>([^<]+)<\/title>/),
    description: firstMatch(html, /<meta[^>]+name="description"[^>]+content="([^"]*)"[^>]*>/),
    robots: firstMatch(html, /<meta[^>]+name="robots"[^>]+content="([^"]*)"[^>]*>/),
    canonical: firstMatch(html, /<link[^>]+rel="canonical"[^>]+href="([^"]+)"[^>]*>/),
    ogUrl: firstMatch(html, /<meta[^>]+property="og:url"[^>]+content="([^"]*)"[^>]*>/),
    ogTitle: firstMatch(html, /<meta[^>]+property="og:title"[^>]+content="([^"]*)"[^>]*>/),
    ogDescription: firstMatch(html, /<meta[^>]+property="og:description"[^>]+content="([^"]*)"[^>]*>/),
    h1: allMatches(html, /<h1[^>]*>([\s\S]*?)<\/h1>/g).map((value) => value.replace(/<[^>]+>/g, '').trim()),
    htmlLang: firstMatch(html, /<html[^>]+lang="([^"]+)"[^>]*>/),
    htmlDir: firstMatch(html, /<html[^>]+dir="([^"]+)"[^>]*>/),
    hrefs: allMatches(html, /<a[^>]+href="([^"]+)"[^>]*>/g),
    ids: new Set(allMatches(html, /\sid="([^"]+)"/g)),
    jsonLd,
  };
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

async function sitemapUrls(file) {
  const xml = await readFile(file, 'utf8');
  return new Set([...xml.matchAll(/<loc>([^<]+)<\/loc>/g)].map((match) => decodeHtml(match[1])));
}

const pages = [];
for (const locale of locales) {
  const discoveredIds = await discoverDocIds(locale);
  assert(
    JSON.stringify(discoveredIds) === JSON.stringify([...docIds].sort()),
    `Documentation route set mismatch for ${locale}: ${JSON.stringify(discoveredIds)}`,
  );
  for (const id of docIds) {
    const html = await readFile(htmlPath(locale, id), 'utf8');
    pages.push({locale, id, route: routePath(locale, id), ...parsePage(html)});
  }
}

assert(pages.length === 24, `Expected 24 documentation pages, found ${pages.length}`);

const expectedRoutes = new Set(pages.map((page) => page.route));
const expectedIndexableRoutes = new Set(['/', '/ar/', ...expectedRoutes]);
const pagesByRoute = new Map(pages.map((page) => [page.route, page]));
const titles = new Map();
const descriptions = new Map();
const canonicals = new Map();

for (const page of pages) {
  const expectedCanonical = `${origin}${page.route}`;
  const expectedSeo = docSeo[page.locale]?.[page.id];
  assert(expectedSeo, `Missing SEO manifest entry: ${page.locale}/${page.id}`);
  const expectedTitle = `${expectedSeo.title} | FactLane`;
  const expectedAlternates = {
    en: `${origin}${routePath('en', page.id)}`,
    ar: `${origin}${routePath('ar', page.id)}`,
    'x-default': `${origin}${routePath('en', page.id)}`,
  };

  assert(page.title, `Missing title: ${page.locale}/${page.id}`);
  assert(page.description, `Missing description: ${page.locale}/${page.id}`);
  assert(page.title === expectedTitle, `Manifest title mismatch: ${page.locale}/${page.id}`);
  assert(
    page.description === expectedSeo.description,
    `Manifest description mismatch: ${page.locale}/${page.id}`,
  );
  assert(page.title.length >= 10, `Title too short: ${page.locale}/${page.id} (${page.title.length})`);
  assert(page.title.length <= 75, `Title too long: ${page.locale}/${page.id} (${page.title.length})`);
  assert(page.description.length >= 80, `Description too short: ${page.locale}/${page.id} (${page.description.length})`);
  assert(page.description.length <= 190, `Description too long: ${page.locale}/${page.id} (${page.description.length})`);
  assert(/[.!?؟]$/u.test(page.description), `Description is not a complete sentence: ${page.locale}/${page.id}`);
  assert(page.canonical === expectedCanonical, `Canonical mismatch: ${page.locale}/${page.id}`);
  assert(page.ogUrl === expectedCanonical, `og:url mismatch: ${page.locale}/${page.id}`);
  assert(page.ogTitle === page.title, `og:title mismatch: ${page.locale}/${page.id}`);
  assert(page.ogDescription === page.description, `og:description mismatch: ${page.locale}/${page.id}`);
  const pageHtml = await readFile(htmlPath(page.locale, page.id), 'utf8');
  const alternateMatches = [...pageHtml.matchAll(/<link[^>]+rel="alternate"[^>]+href="([^"]+)"[^>]+hreflang="([^"]+)"[^>]*>/g)];
  const alternates = Object.fromEntries(alternateMatches.map((match) => [match[2], decodeHtml(match[1])]));
  assert(JSON.stringify(alternates) === JSON.stringify(expectedAlternates), `hreflang mismatch: ${page.locale}/${page.id}`);

  assert(page.h1.length === 1, `Expected one H1: ${page.locale}/${page.id}`);
  assert(page.htmlLang === page.locale, `HTML lang mismatch: ${page.locale}/${page.id}`);
  assert(page.htmlDir === (page.locale === 'ar' ? 'rtl' : 'ltr'), `HTML dir mismatch: ${page.locale}/${page.id}`);

  if (page.locale === 'ar') {
    assert(/[\u0600-\u06ff]/u.test(page.title), `Arabic title fallback: ${page.id}`);
    assert(/[\u0600-\u06ff]/u.test(page.description), `Arabic description fallback: ${page.id}`);
    assert(/[\u0600-\u06ff]/u.test(page.h1[0]), `Arabic H1 fallback: ${page.id}`);
  }

  const breadcrumbs = page.jsonLd.filter((entry) => entry?.['@type'] === 'BreadcrumbList');
  assert(breadcrumbs.length === 1, `Expected one BreadcrumbList schema: ${page.locale}/${page.id}`);
  assert(
    !page.jsonLd.some((entry) => entry?.['@type'] === 'SoftwareSourceCode'),
    `Landing SoftwareSourceCode schema leaked into docs: ${page.locale}/${page.id}`,
  );
  const items = breadcrumbs[0].itemListElement;
  assert(Array.isArray(items) && items.length >= 2, `Invalid BreadcrumbList items: ${page.locale}/${page.id}`);
  const expectedHome = page.locale === 'ar' ? `${origin}/ar/` : `${origin}/`;
  const expectedHomeName = page.locale === 'ar' ? 'الرئيسية' : 'Home page';
  assert(items[0]?.position === 1, `Breadcrumb home position mismatch: ${page.locale}/${page.id}`);
  assert(items[0]?.name === expectedHomeName, `Breadcrumb home name mismatch: ${page.locale}/${page.id}`);
  assert(items[0]?.item === expectedHome, `Breadcrumb home URL mismatch: ${page.locale}/${page.id}`);
  assert(
    items.every((item, index) => item?.position === index + 1),
    `Breadcrumb positions are not contiguous: ${page.locale}/${page.id}`,
  );
  assert(items.at(-1)?.item === expectedCanonical, `Breadcrumb canonical mismatch: ${page.locale}/${page.id}`);

  for (const [map, value, kind] of [
    [titles, page.title, 'title'],
    [descriptions, page.description, 'description'],
    [canonicals, page.canonical, 'canonical'],
  ]) {
    const owner = map.get(value);
    assert(!owner, `Duplicate ${kind}: ${owner} and ${page.locale}/${page.id}`);
    map.set(value, `${page.locale}/${page.id}`);
  }
}

for (const id of docIds) {
  const en = pages.find((page) => page.locale === 'en' && page.id === id);
  const ar = pages.find((page) => page.locale === 'ar' && page.id === id);
  assert(en.title !== ar.title, `Cross-locale title fallback: ${id}`);
  assert(en.description !== ar.description, `Cross-locale description fallback: ${id}`);
}

for (const page of pages) {
  for (const href of page.hrefs) {
    if (href === '#') {
      continue;
    }
    if (href.startsWith('#')) {
      const fragment = decodeURIComponent(href.slice(1));
      assert(page.ids.has(fragment), `Broken fragment from ${page.locale}/${page.id}: ${href}`);
      continue;
    }
    if (!href.startsWith('/')) {
      continue;
    }
    const [pathAndQuery, rawFragment] = href.split('#', 2);
    const clean = pathAndQuery.split('?', 1)[0];
    assert(expectedIndexableRoutes.has(clean), `Broken indexable internal link from ${page.locale}/${page.id}: ${href}`);
    if (rawFragment) {
      const target = pagesByRoute.get(clean);
      assert(target, `Fragment target is not a documentation route from ${page.locale}/${page.id}: ${href}`);
      const fragment = decodeURIComponent(rawFragment);
      assert(target.ids.has(fragment), `Broken target fragment from ${page.locale}/${page.id}: ${href}`);
    }
  }
}

const robots = await readFile(path.join(buildRoot, 'robots.txt'), 'utf8');

if (mode === 'public') {
  const enSitemap = await sitemapUrls(path.join(buildRoot, 'sitemap.xml'));
  const arSitemap = await sitemapUrls(path.join(buildRoot, 'ar', 'sitemap.xml'));
  const expectedEnSitemap = new Set([
    `${origin}/`,
    ...pages.filter((page) => page.locale === 'en').map((page) => page.canonical),
  ]);
  const expectedArSitemap = new Set([
    `${origin}/ar/`,
    ...pages.filter((page) => page.locale === 'ar').map((page) => page.canonical),
  ]);
  assert(/(^|\n)Allow: \/($|\n)/.test(robots), 'Public robots.txt must Allow: /');
  assert(robots.includes(`Sitemap: ${origin}/sitemap.xml`), 'Public robots missing EN sitemap');
  assert(robots.includes(`Sitemap: ${origin}/ar/sitemap.xml`), 'Public robots missing AR sitemap');
  assert(
    JSON.stringify([...enSitemap].sort()) === JSON.stringify([...expectedEnSitemap].sort()),
    `Public EN sitemap route set mismatch: ${JSON.stringify([...enSitemap].sort())}`,
  );
  assert(
    JSON.stringify([...arSitemap].sort()) === JSON.stringify([...expectedArSitemap].sort()),
    `Public AR sitemap route set mismatch: ${JSON.stringify([...arSitemap].sort())}`,
  );
  for (const page of pages) {
    const sitemap = page.locale === 'en' ? enSitemap : arSitemap;
    assert(sitemap.has(page.canonical), `Doc missing from sitemap: ${page.locale}/${page.id}`);
    assert(!page.robots.toLowerCase().includes('noindex'), `Public noindex found: ${page.locale}/${page.id}`);
  }
} else {
  assert(/(^|\n)Disallow: \/($|\n)/.test(robots), 'Local robots.txt must Disallow: /');
  for (const page of pages) {
    assert(page.robots === 'noindex,nofollow,noarchive', `Local robots meta mismatch: ${page.locale}/${page.id}`);
  }
  const rootEntries = await readdir(buildRoot, {withFileTypes: true});
  assert(!rootEntries.some((entry) => entry.name === 'sitemap.xml'), 'Local EN sitemap must be absent');
  const arEntries = await readdir(path.join(buildRoot, 'ar'), {withFileTypes: true});
  assert(!arEntries.some((entry) => entry.name === 'sitemap.xml'), 'Local AR sitemap must be absent');
}

console.log(
  `Documentation SEO audit passed: mode=${mode}; pages=24; titles=24 unique; descriptions=24 unique; canonicals=24 unique`,
);

import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const siteRoot = path.resolve(here, '..');

const verifyBaseRaw = process.env.FACTLANE_VERIFY_BASE_URL;
const expectedOriginRaw = process.env.FACTLANE_EXPECTED_ORIGIN;
const mode = process.env.FACTLANE_VERIFY_MODE ?? 'public';

if (!verifyBaseRaw) {
  throw new Error('FACTLANE_VERIFY_BASE_URL is required');
}
if (!expectedOriginRaw) {
  throw new Error('FACTLANE_EXPECTED_ORIGIN is required');
}
if (!['public', 'local'].includes(mode)) {
  throw new Error('FACTLANE_VERIFY_MODE must be exactly public or local');
}

function originOnly(value, name, allowedProtocols) {
  const parsed = new URL(value);
  if (
    !allowedProtocols.includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== '/' ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error(`${name} must be an origin only (${allowedProtocols.join(' or ')})`);
  }
  return parsed.origin;
}

const verifyBase = originOnly(verifyBaseRaw, 'FACTLANE_VERIFY_BASE_URL', ['http:', 'https:']);
const expectedOrigin = originOnly(expectedOriginRaw, 'FACTLANE_EXPECTED_ORIGIN', ['https:']);
const verifyBaseUrl = new URL(verifyBase);
const loopbackVerificationHost =
  verifyBaseUrl.hostname === 'localhost' ||
  verifyBaseUrl.hostname === '127.0.0.1' ||
  verifyBaseUrl.hostname === '[::1]';
if (mode === 'public' && verifyBaseUrl.protocol !== 'https:' && !loopbackVerificationHost) {
  throw new Error('Public production verification requires HTTPS; HTTP is allowed only for loopback rehearsal');
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

const authority = JSON.parse(
  await readFile(path.join(siteRoot, 'src', 'content', 'answerAuthority.json'), 'utf8'),
);

const standardUserAgents = {
  browser:
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36',
  googlebot: 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
  bingbot: 'Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)',
};

// These identities were accepted in R19 from current official vendor guidance.
// xAI is intentionally absent because R19 found no official crawler/robots UA policy to bind.
const documentedAiUserAgents = {
  oaiSearchBot:
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36; compatible; OAI-SearchBot/1.4; +https://openai.com/searchbot',
  claudeSearchBot: 'Claude-SearchBot',
  claudeUser: 'Claude-User',
};

const allUserAgents = {...standardUserAgents, ...documentedAiUserAgents};

function docRoute(locale, id) {
  const prefix = locale === 'en' ? '' : '/ar';
  return id === 'INTRO' ? `${prefix}/docs/` : `${prefix}/docs/${id}/`;
}

const canonicalRoutes = [
  '/',
  '/answers/',
  ...docIds.map((id) => docRoute('en', id)),
  '/ar/',
  '/ar/answers/',
  ...docIds.map((id) => docRoute('ar', id)),
].sort();

const canonicalRouteSet = new Set(canonicalRoutes);

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function attr(tag, name) {
  const match = tag.match(new RegExp(`\\b${name}=["']([^"']*)["']`, 'i'));
  return match ? decodeHtml(match[1]) : '';
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

function hash(value) {
  return createHash('sha256').update(value).digest('hex');
}

function parseHtml(html) {
  const title = decodeHtml(html.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1]?.trim() ?? '');
  const metaTags = [...html.matchAll(/<meta\b[^>]*>/gi)].map((match) => match[0]);
  const linkTags = [...html.matchAll(/<link\b[^>]*>/gi)].map((match) => match[0]);
  const canonicalTag = linkTags.find(
    (tag) => attr(tag, 'rel').toLowerCase() === 'canonical',
  );
  const alternates = {};
  for (const tag of linkTags) {
    if (attr(tag, 'rel').toLowerCase() !== 'alternate') continue;
    const hreflang = attr(tag, 'hreflang');
    const href = attr(tag, 'href');
    if (hreflang && href) alternates[hreflang] = href;
  }
  const sortedAlternates = Object.fromEntries(
    Object.entries(alternates).sort(([a], [b]) => a.localeCompare(b)),
  );
  const robotsTag = metaTags.find((tag) => attr(tag, 'name').toLowerCase() === 'robots');
  const descriptionTag = metaTags.find(
    (tag) => attr(tag, 'name').toLowerCase() === 'description',
  );
  const htmlTag = html.match(/<html\b[^>]*>/i)?.[0] ?? '';
  const jsonLd = [...html.matchAll(/<script[^>]+type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi)]
    .map((match) => JSON.parse(match[1]));
  const mainHtml = html.match(/<main\b[^>]*>([\s\S]*?)<\/main>/i)?.[1] ?? '';
  const hrefs = [...html.matchAll(/<a\b[^>]+href=["']([^"']+)["'][^>]*>/gi)].map((match) =>
    decodeHtml(match[1]),
  );
  const answerIds = [...html.matchAll(/data-answer-id=["']([^"']+)["']/gi)].map((match) =>
    decodeHtml(match[1]),
  );
  return {
    title,
    description: descriptionTag ? attr(descriptionTag, 'content') : '',
    canonical: canonicalTag ? attr(canonicalTag, 'href') : '',
    robots: robotsTag ? attr(robotsTag, 'content') : '',
    lang: attr(htmlTag, 'lang'),
    dir: attr(htmlTag, 'dir'),
    alternates: sortedAlternates,
    jsonLd,
    jsonLdHash: hash(JSON.stringify(jsonLd)),
    h1Count: [...html.matchAll(/<h1\b/gi)].length,
    mainText: visibleText(mainHtml),
    mainHash: hash(visibleText(mainHtml)),
    hrefs,
    hrefsHash: hash(JSON.stringify(hrefs)),
    answerIds,
    answerIdsHash: hash(JSON.stringify(answerIds)),
  };
}

function expectedAlternates(route) {
  if (route === '/' || route === '/ar/') {
    return {
      ar: `${expectedOrigin}/ar/`,
      en: `${expectedOrigin}/`,
      'x-default': `${expectedOrigin}/`,
    };
  }
  if (route === '/answers/' || route === '/ar/answers/') {
    return {
      ar: `${expectedOrigin}/ar/answers/`,
      en: `${expectedOrigin}/answers/`,
      'x-default': `${expectedOrigin}/answers/`,
    };
  }
  const match = route.match(/^\/(ar\/)?docs(?:\/([^/]+))?\/$/);
  assert(match, `Cannot derive hreflang counterpart for ${route}`);
  const id = match[2] ?? 'INTRO';
  return {
    ar: `${expectedOrigin}${docRoute('ar', id)}`,
    en: `${expectedOrigin}${docRoute('en', id)}`,
    'x-default': `${expectedOrigin}${docRoute('en', id)}`,
  };
}

function localizedRoute(locale, route) {
  if (locale === 'en' || !route.startsWith('/')) return route;
  if (route === '/') return '/ar/';
  return `/ar${route}`;
}

function slashless(route) {
  return route !== '/' && route.endsWith('/') ? route.slice(0, -1) : null;
}

function wrongCase(route) {
  if (route === '/' || route === '/ar/') return null;
  const parts = route.split('/');
  const index = route.endsWith('/') ? parts.length - 2 : parts.length - 1;
  const segment = parts[index];
  if (!segment) return null;
  parts[index] = /[A-Z]/.test(segment) ? segment.toLowerCase() : segment.toUpperCase();
  return parts.join('/');
}

function requestUrl(pathname) {
  return new URL(pathname, `${verifyBase}/`).toString();
}

async function fetchOnce(pathname, userAgent) {
  const response = await fetch(requestUrl(pathname), {
    redirect: 'manual',
    headers: {
      accept: '*/*',
      'user-agent': userAgent,
    },
  });
  const body = await response.text();
  return {
    path: pathname,
    status: response.status,
    location: response.headers.get('location') ?? '',
    contentType: response.headers.get('content-type') ?? '',
    xRobotsTag: response.headers.get('x-robots-tag') ?? '',
    body,
  };
}

async function fetchChain(start, userAgent, maxRedirects = 3) {
  const chain = [];
  const seen = new Set();
  let current = start;
  for (let step = 0; step <= maxRedirects; step += 1) {
    assert(!seen.has(current), `Redirect loop from ${start}: ${current}`);
    seen.add(current);
    const result = await fetchOnce(current, userAgent);
    chain.push(result);
    if (![301, 302, 303, 307, 308].includes(result.status)) return chain;
    assert(result.location, `Redirect without Location: ${current}`);
    const next = new URL(result.location, requestUrl(current));
    assert(
      next.origin === new URL(verifyBase).origin,
      `Cross-origin redirect is outside the reviewed root-host profile: ${current} -> ${result.location}`,
    );
    current = `${next.pathname}${next.search}`;
  }
  throw new Error(`Redirect chain longer than ${maxRedirects} from ${start}`);
}

function snapshot(chain) {
  const final = chain.at(-1);
  const page = final.contentType.includes('text/html') ? parseHtml(final.body) : null;
  const finalRequestPath = chain.length > 1
    ? new URL(chain.at(-2).location, requestUrl(chain.at(-2).path)).pathname
    : final.path.split('?', 1)[0];
  return {
    status: final.status,
    redirects: chain.length - 1,
    finalPath: finalRequestPath,
    contentType: final.contentType,
    xRobotsTag: final.xRobotsTag,
    bodyHash: hash(final.body),
    page,
  };
}

function assertCanonicalSnapshot(route, snap) {
  assert(snap.status === 200, `Canonical route must be 200: ${route} -> ${snap.status}`);
  assert(snap.redirects === 0, `Canonical route must not redirect: ${route}`);
  assert(snap.contentType.includes('text/html'), `Canonical route is not HTML: ${route}`);
  assert(snap.page, `Canonical HTML parse failed: ${route}`);
  assert(snap.page.canonical === `${expectedOrigin}${route}`, `Canonical mismatch: ${route}`);
  assert(snap.page.h1Count === 1, `Expected one H1: ${route}`);
  assert(snap.page.mainText.length > 0, `Missing SSR main content: ${route}`);
  const locale = route.startsWith('/ar/') ? 'ar' : 'en';
  assert(snap.page.lang === locale, `HTML lang mismatch: ${route}`);
  assert(snap.page.dir === (locale === 'ar' ? 'rtl' : 'ltr'), `HTML dir mismatch: ${route}`);
  assert(
    JSON.stringify(snap.page.alternates) === JSON.stringify(expectedAlternates(route)),
    `hreflang mismatch: ${route}`,
  );
  if (mode === 'public') {
    assert(!snap.page.robots.toLowerCase().includes('noindex'), `Public route is noindex: ${route}`);
    assert(
      !/(?:^|,)\s*(?:[a-z0-9_-]+\s*:\s*)?(?:noindex|nofollow|none)\b/i.test(
        snap.xRobotsTag,
      ),
      `Public route is restricted by X-Robots-Tag: ${route} -> ${snap.xRobotsTag}`,
    );
  } else {
    assert(
      snap.page.robots === 'noindex,nofollow,noarchive',
      `Local route robots mismatch: ${route}`,
    );
  }

  const isAnswer = route === '/answers/' || route === '/ar/answers/';
  const isLanding = route === '/' || route === '/ar/';
  const software = snap.page.jsonLd.filter((entry) => entry?.['@type'] === 'SoftwareSourceCode');
  const breadcrumbs = snap.page.jsonLd.filter((entry) => entry?.['@type'] === 'BreadcrumbList');
  if (isAnswer) {
    assert(snap.page.jsonLd.length === 0, `Answer route must not carry decorative JSON-LD: ${route}`);
    assert(snap.page.answerIds.length === 9, `Answer route must expose 9 SSR answer IDs: ${route}`);
  } else if (isLanding) {
    assert(software.length === 1, `Landing must expose one SoftwareSourceCode JSON-LD object: ${route}`);
    assert(breadcrumbs.length === 0, `BreadcrumbList must not leak into landing: ${route}`);
  } else {
    assert(breadcrumbs.length === 1, `Docs route must expose one BreadcrumbList: ${route}`);
    assert(software.length === 0, `SoftwareSourceCode must not leak into docs: ${route}`);
  }
}

function parseSitemap(xml) {
  return [...xml.matchAll(/<url>([\s\S]*?)<\/url>/g)].map((match) => {
    const block = match[1];
    return {
      loc: decodeHtml(block.match(/<loc>([^<]+)<\/loc>/)?.[1]?.trim() ?? ''),
      lastmod: block.match(/<lastmod>([^<]+)<\/lastmod>/)?.[1]?.trim() ?? '',
    };
  });
}

const browserUa = standardUserAgents.browser;
const canonicalBaseline = new Map();
let alias200 = 0;
let aliasRedirected = 0;
let queryChecks = 0;
let wrongCaseChecks = 0;

for (const route of canonicalRoutes) {
  const chain = await fetchChain(route, browserUa);
  const snap = snapshot(chain);
  assertCanonicalSnapshot(route, snap);
  canonicalBaseline.set(route, snap);

  const noSlash = slashless(route);
  if (noSlash) {
    const alias = snapshot(await fetchChain(noSlash, browserUa));
    assert(alias.status === 200, `Slashless alias did not resolve to canonical content: ${noSlash}`);
    assert(alias.page?.canonical === `${expectedOrigin}${route}`, `Slashless alias canonical mismatch: ${noSlash}`);
    if (alias.redirects === 0) alias200 += 1;
    else aliasRedirected += 1;
  }

  const query = snapshot(await fetchChain(`${route}?factlane_readiness=1`, browserUa));
  assert(query.status === 200, `Query variant did not resolve: ${route}`);
  assert(query.page?.canonical === `${expectedOrigin}${route}`, `Query variant canonical mismatch: ${route}`);
  queryChecks += 1;

  const caseVariant = wrongCase(route);
  if (caseVariant && caseVariant !== route) {
    const wrong = snapshot(await fetchChain(caseVariant, browserUa));
    assert(wrong.status === 404, `Wrong-case route must remain 404: ${caseVariant} -> ${wrong.status}`);
    assert(
      wrong.page?.robots === 'noindex,nofollow,noarchive',
      `Wrong-case 404 must remain noindex: ${caseVariant}`,
    );
    wrongCaseChecks += 1;
  }
}

for (const missing of [
  '/__factlane_publication_readiness_missing__/',
  '/ar/__factlane_publication_readiness_missing__/',
  '/en/',
]) {
  const miss = snapshot(await fetchChain(missing, browserUa));
  assert(miss.status === 404, `Missing/default-locale route must return HTTP 404: ${missing}`);
  assert(miss.page?.robots === 'noindex,nofollow,noarchive', `404 must remain noindex: ${missing}`);
}

const answerSourceTargets = new Set();
for (const locale of ['en', 'ar']) {
  const route = locale === 'en' ? '/answers/' : '/ar/answers/';
  const page = canonicalBaseline.get(route)?.page;
  assert(page, `Answer page baseline missing: ${locale}`);
  for (const answer of authority[locale].answers) {
    assert(page.answerIds.includes(answer.id), `Missing SSR answer section ${locale}#${answer.id}`);
    assert(page.mainText.includes(answer.question), `Missing SSR answer question ${locale}#${answer.id}`);
    assert(page.mainText.includes(answer.answer), `Missing SSR answer text ${locale}#${answer.id}`);
    for (const source of answer.sources) {
      const href = localizedRoute(locale, source.to);
      assert(page.hrefs.includes(href), `Missing answer source link ${locale}: ${href}`);
      answerSourceTargets.add(href);
    }
  }
}

for (const target of answerSourceTargets) {
  const source = snapshot(await fetchChain(target, browserUa));
  assert(source.status === 200, `Answer source target is not 200: ${target}`);
  assert(source.page?.canonical === `${expectedOrigin}${target}`, `Answer source canonical mismatch: ${target}`);
}

const robots = await fetchOnce('/robots.txt', browserUa);
assert(robots.status === 200, `robots.txt must be 200, got ${robots.status}`);
assert(robots.contentType.includes('text/plain'), `robots.txt must be text/plain: ${robots.contentType}`);

const llms = await fetchOnce('/llms.txt', browserUa);
const arLlms = await fetchOnce('/ar/llms.txt', browserUa);
const enSitemapResponse = await fetchOnce('/sitemap.xml', browserUa);
const arSitemapResponse = await fetchOnce('/ar/sitemap.xml', browserUa);

let sitemapLastmodMode = 'absent';
if (mode === 'public') {
  for (const token of ['OAI-SearchBot', 'Claude-SearchBot', 'Claude-User']) {
    assert(robots.body.includes(`User-agent: ${token}`), `Public robots missing ${token}`);
  }
  assert(robots.body.includes('User-agent: *\nAllow: /'), 'Public wildcard crawler access missing');
  assert(!robots.body.includes('Disallow: /'), 'Public robots must not contain a root crawl block');
  assert(robots.body.includes(`Sitemap: ${expectedOrigin}/sitemap.xml`), 'Public EN sitemap declaration missing');
  assert(robots.body.includes(`Sitemap: ${expectedOrigin}/ar/sitemap.xml`), 'Public AR sitemap declaration missing');

  assert(llms.status === 200, `Public root llms.txt must be 200, got ${llms.status}`);
  assert(llms.contentType.includes('text/plain'), `Public llms.txt must be text/plain: ${llms.contentType}`);
  assert(llms.body.includes(`${expectedOrigin}/answers/`), 'llms.txt missing EN answer surface');
  assert(llms.body.includes(`${expectedOrigin}/ar/answers/`), 'llms.txt missing AR answer surface');
  assert(llms.body.includes('v0.1.3'), 'llms.txt missing supported release boundary');
  assert(llms.body.includes('not a universal memory replacement'), 'llms.txt missing product-scope boundary');
  assert(arLlms.status === 404, `Public /ar/llms.txt must remain absent/404, got ${arLlms.status}`);

  assert(enSitemapResponse.status === 200, 'Public EN sitemap must be 200');
  assert(arSitemapResponse.status === 200, 'Public AR sitemap must be 200');
  const enSitemap = parseSitemap(enSitemapResponse.body);
  const arSitemap = parseSitemap(arSitemapResponse.body);
  const expectedEn = canonicalRoutes.filter((route) => !route.startsWith('/ar/')).map((route) => `${expectedOrigin}${route}`).sort();
  const expectedAr = canonicalRoutes.filter((route) => route.startsWith('/ar/')).map((route) => `${expectedOrigin}${route}`).sort();
  assert(
    JSON.stringify(enSitemap.map((entry) => entry.loc).sort()) === JSON.stringify(expectedEn),
    'Public EN sitemap route set mismatch',
  );
  assert(
    JSON.stringify(arSitemap.map((entry) => entry.loc).sort()) === JSON.stringify(expectedAr),
    'Public AR sitemap route set mismatch',
  );
  const allSitemap = [...enSitemap, ...arSitemap];
  const withLastmod = allSitemap.filter((entry) => entry.lastmod);
  assert(
    withLastmod.length === 0 || withLastmod.length === allSitemap.length,
    'Sitemap lastmod coverage must be complete or absent',
  );
  sitemapLastmodMode = withLastmod.length === allSitemap.length ? 'complete' : 'absent';
  for (const entry of withLastmod) {
    const timestamp = Date.parse(entry.lastmod);
    assert(!Number.isNaN(timestamp), `Invalid sitemap lastmod: ${entry.loc} -> ${entry.lastmod}`);
    assert(timestamp <= Date.now() + 5 * 60 * 1000, `Future sitemap lastmod: ${entry.loc} -> ${entry.lastmod}`);
  }
} else {
  assert(robots.body.includes('User-agent: *\nDisallow: /'), 'Local robots must disallow crawling');
  assert(!robots.body.includes('Sitemap:'), 'Local robots must not declare sitemaps');
  assert(llms.status === 404, `Local root llms.txt must be absent/404, got ${llms.status}`);
  assert(arLlms.status === 404, `Local /ar/llms.txt must be absent/404, got ${arLlms.status}`);
  assert(enSitemapResponse.status === 404, `Local EN sitemap must be absent/404, got ${enSitemapResponse.status}`);
  assert(arSitemapResponse.status === 404, `Local AR sitemap must be absent/404, got ${arSitemapResponse.status}`);
}

const parityFields = [
  'status',
  'contentType',
  'xRobotsTag',
  'bodyHash',
  'title',
  'description',
  'canonical',
  'robots',
  'lang',
  'dir',
  'alternates',
  'jsonLdHash',
  'mainHash',
  'hrefsHash',
  'answerIdsHash',
];
let crawlerRecords = 0;
let crawlerComparisons = 0;

for (const route of canonicalRoutes) {
  const baseline = canonicalBaseline.get(route);
  const baselineRecord = {
    status: baseline.status,
    contentType: baseline.contentType,
    xRobotsTag: baseline.xRobotsTag,
    bodyHash: baseline.bodyHash,
    ...baseline.page,
  };
  for (const [name, userAgent] of Object.entries(allUserAgents)) {
    const snap = snapshot(await fetchChain(route, userAgent));
    assertCanonicalSnapshot(route, snap);
    const record = {
      status: snap.status,
      contentType: snap.contentType,
      xRobotsTag: snap.xRobotsTag,
      bodyHash: snap.bodyHash,
      ...snap.page,
    };
    crawlerRecords += 1;
    for (const field of parityFields) {
      crawlerComparisons += 1;
      assert(
        JSON.stringify(record[field]) === JSON.stringify(baselineRecord[field]),
        `Crawler SSR parity mismatch: ${route} ${name} ${field}`,
      );
    }
  }
}

const summary = {
  status: 'PASS',
  mode,
  verifyBase,
  expectedOrigin,
  canonicalRoutes: canonicalRoutes.length,
  answerSourceTargets: answerSourceTargets.size,
  slashlessAliases200: alias200,
  slashlessAliasesRedirected: aliasRedirected,
  queryChecks,
  wrongCaseChecks,
  sitemapLastmodMode,
  crawlerUserAgents: Object.keys(allUserAgents),
  crawlerRecords,
  crawlerComparisons,
  networkChecksAreBuildGate: false,
};

console.log(JSON.stringify(summary, null, 2));

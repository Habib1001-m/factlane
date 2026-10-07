import {access, readFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const siteRoot = path.resolve(here, '..');
const buildRoot = path.resolve(siteRoot, process.env.FACTLANE_BUILD_DIR ?? 'build');
const mode = process.env.FACTLANE_SEO_MODE ?? 'public';
const configuredSiteUrl = process.env.FACTLANE_SITE_URL ?? 'https://factlane.local';
const origin = new URL(configuredSiteUrl).origin;
const authority = JSON.parse(
  await readFile(path.join(siteRoot, 'src', 'content', 'answerAuthority.json'), 'utf8'),
);

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

function visibleText(html) {
  return decodeHtml(
    html
      .replace(/<script\b[\s\S]*?<\/script>/gi, ' ')
      .replace(/<style\b[\s\S]*?<\/style>/gi, ' ')
      .replace(/<[^>]+>/g, ' ')
      .replace(/\s+/g, ' ')
      .trim(),
  );
}

function answerRoute(locale) {
  return locale === 'en' ? '/answers/' : '/ar/answers/';
}

function answerFile(locale) {
  return locale === 'en'
    ? path.join(buildRoot, 'answers', 'index.html')
    : path.join(buildRoot, 'ar', 'answers', 'index.html');
}

function localizedRoute(locale, route) {
  if (locale === 'en' || !route.startsWith('/')) return route;
  if (route === '/') return '/ar/';
  return `/ar${route}`;
}

function fileForRoute(route) {
  const relative = route.replace(/^\//, '').replace(/\/$/, '');
  return relative ? path.join(buildRoot, relative, 'index.html') : path.join(buildRoot, 'index.html');
}

async function exists(file) {
  try {
    await access(file);
    return true;
  } catch {
    return false;
  }
}

function parsePage(html) {
  const alternates = Object.fromEntries(
    [...html.matchAll(/<link[^>]+rel="alternate"[^>]+href="([^"]+)"[^>]+hreflang="([^"]+)"[^>]*>/g)]
      .map((match) => [match[2], decodeHtml(match[1])]),
  );
  const jsonLd = [...html.matchAll(/<script[^>]+type="application\/ld\+json"[^>]*>([\s\S]*?)<\/script>/g)]
    .map((match) => JSON.parse(match[1]));
  return {
    title: firstMatch(html, /<title[^>]*>([^<]+)<\/title>/),
    description: firstMatch(html, /<meta[^>]+name="description"[^>]+content="([^"]*)"[^>]*>/),
    robots: firstMatch(html, /<meta[^>]+name="robots"[^>]+content="([^"]*)"[^>]*>/),
    canonical: firstMatch(html, /<link[^>]+rel="canonical"[^>]+href="([^"]+)"[^>]*>/),
    ogUrl: firstMatch(html, /<meta[^>]+property="og:url"[^>]+content="([^"]+)"[^>]*>/),
    htmlLang: firstMatch(html, /<html[^>]+lang="([^"]+)"[^>]*>/),
    htmlDir: firstMatch(html, /<html[^>]+dir="([^"]+)"[^>]*>/),
    h1Count: [...html.matchAll(/<h1\b/g)].length,
    alternates,
    hrefs: [...html.matchAll(/<a[^>]+href="([^"]+)"[^>]*>/g)].map((match) => decodeHtml(match[1])),
    answerIds: new Set(
      [...html.matchAll(/data-answer-id="([^"]+)"/g)].map((match) => decodeHtml(match[1])),
    ),
    jsonLd,
    text: visibleText(html),
  };
}

for (const locale of ['en', 'ar']) {
  const copy = authority[locale];
  const html = await readFile(answerFile(locale), 'utf8');
  const page = parsePage(html);
  const route = answerRoute(locale);
  const expectedCanonical = `${origin}${route}`;
  const expectedAlternates = {
    en: `${origin}/answers/`,
    ar: `${origin}/ar/answers/`,
    'x-default': `${origin}/answers/`,
  };

  assert(page.title === `${copy.meta.title} | FactLane`, `Title mismatch: ${locale}`);
  assert(page.description === copy.meta.description, `Description mismatch: ${locale}`);
  assert(page.canonical === expectedCanonical, `Canonical mismatch: ${locale}`);
  assert(page.ogUrl === expectedCanonical, `og:url mismatch: ${locale}`);
  assert(Object.keys(page.alternates).length === 3, `Unexpected hreflang count: ${locale}`);
  for (const [hreflang, href] of Object.entries(expectedAlternates)) {
    assert(page.alternates[hreflang] === href, `hreflang mismatch ${locale}/${hreflang}`);
  }
  assert(page.htmlLang === locale, `HTML lang mismatch: ${locale}`);
  assert(page.htmlDir === (locale === 'ar' ? 'rtl' : 'ltr'), `HTML dir mismatch: ${locale}`);
  assert(page.h1Count === 1, `Expected one H1: ${locale}`);
  assert(page.jsonLd.length === 0, `Answer page must not carry decorative JSON-LD: ${locale}`);
  if (mode === 'public') {
    assert(!page.robots.toLowerCase().includes('noindex'), `Public answer route is noindex: ${locale}`);
  } else {
    assert(page.robots === 'noindex,nofollow,noarchive', `Local answer robots mismatch: ${locale}`);
  }

  assert(page.text.includes(copy.title), `SSR H1 text missing: ${locale}`);
  assert(page.text.includes(copy.sourceCue), `SSR provenance cue missing: ${locale}`);
  assert(page.answerIds.size === copy.answers.length, `Answer count mismatch: ${locale}`);

  for (const answer of copy.answers) {
    assert(page.answerIds.has(answer.id), `Missing answer ID ${locale}#${answer.id}`);
    assert(page.text.includes(answer.question), `SSR question missing ${locale}#${answer.id}`);
    assert(page.text.includes(answer.answer), `SSR answer missing ${locale}#${answer.id}`);
    for (const source of answer.sources) {
      const href = localizedRoute(locale, source.to);
      assert(page.hrefs.includes(href), `Missing canonical source link ${locale}: ${href}`);
      assert(await exists(fileForRoute(href)), `Canonical source target missing ${locale}: ${href}`);
    }
  }
}

const robots = await readFile(path.join(buildRoot, 'robots.txt'), 'utf8');
const rootLlms = path.join(buildRoot, 'llms.txt');
const arLlms = path.join(buildRoot, 'ar', 'llms.txt');

if (mode === 'public') {
  for (const token of ['OAI-SearchBot', 'Claude-SearchBot', 'Claude-User']) {
    assert(robots.includes(`User-agent: ${token}`), `Public robots missing ${token}`);
  }
  assert(robots.includes('User-agent: *\nAllow: /'), 'Public wildcard crawler access missing');
  assert(robots.includes(`Sitemap: ${origin}/sitemap.xml`), 'Public EN sitemap declaration missing');
  assert(robots.includes(`Sitemap: ${origin}/ar/sitemap.xml`), 'Public AR sitemap declaration missing');

  assert(await exists(rootLlms), 'Public root llms.txt missing');
  assert(!(await exists(arLlms)), 'Public localized /ar/llms.txt must be absent');
  const llms = await readFile(rootLlms, 'utf8');
  assert(llms.includes(`${origin}/answers/`), 'llms.txt missing EN canonical answer surface');
  assert(llms.includes(`${origin}/ar/answers/`), 'llms.txt missing AR canonical answer surface');
  assert(llms.includes(`${origin}/docs/TOOLS/`), 'llms.txt missing tool contract');
  assert(llms.includes(`${origin}/docs/ENVIRONMENT/`), 'llms.txt missing environment contract');
  assert(llms.includes(`${origin}/docs/QUICKSTART/`), 'llms.txt missing Quick Start');
  assert(llms.includes(`${origin}/ar/docs/CORE_CONCEPTS/`), 'llms.txt missing AR core concepts');
  assert(llms.includes(`${origin}/ar/docs/TOOLS/`), 'llms.txt missing AR tool contract');
  assert(llms.includes(`${origin}/ar/docs/ENVIRONMENT/`), 'llms.txt missing AR environment contract');
  assert(llms.includes(`${origin}/ar/docs/QUICKSTART/`), 'llms.txt missing AR Quick Start');
  assert(!llms.includes('/docs/PROJECT_HISTORY/'), 'llms.txt should not include project-history noise');
  assert(!llms.includes('/docs/RELEASE_OPERATIONS/'), 'llms.txt should not include release-operations noise');

  const enSitemap = await readFile(path.join(buildRoot, 'sitemap.xml'), 'utf8');
  const arSitemap = await readFile(path.join(buildRoot, 'ar', 'sitemap.xml'), 'utf8');
  assert(enSitemap.includes(`<loc>${origin}/answers/</loc>`), 'EN answer route missing from sitemap');
  assert(arSitemap.includes(`<loc>${origin}/ar/answers/</loc>`), 'AR answer route missing from sitemap');
} else {
  assert(robots.includes('User-agent: *\nDisallow: /'), 'Local robots must block crawling');
  assert(!(await exists(rootLlms)), 'Local root llms.txt must be absent');
  assert(!(await exists(arLlms)), 'Local /ar/llms.txt must be absent');
}

console.log(
  `Answer authority audit passed: mode=${mode}; locales=2; answers=18; rootLlms=${mode === 'public' ? 'present' : 'absent'}; localizedLlms=absent`,
);

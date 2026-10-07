import {readFile, writeFile} from 'node:fs/promises';
import path from 'node:path';

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

function assert(condition, message) {
  if (!condition) throw new Error(`SEO regression guard: ${message}`);
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
  const alternates = Object.fromEntries(
    [...html.matchAll(/<link[^>]+rel="alternate"[^>]+href="([^"]+)"[^>]+hreflang="([^"]+)"[^>]*>/g)].map(
      (match) => [match[2], decodeHtml(match[1])],
    ),
  );
  const jsonLd = [...html.matchAll(/<script[^>]+type="application\/ld\+json"[^>]*>([\s\S]*?)<\/script>/g)].map(
    (match) => JSON.parse(match[1]),
  );
  return {
    title: firstMatch(html, /<title[^>]*>([^<]+)<\/title>/),
    description: firstMatch(html, /<meta[^>]+name="description"[^>]+content="([^"]*)"[^>]*>/),
    robots: firstMatch(html, /<meta[^>]+name="robots"[^>]+content="([^"]*)"[^>]*>/),
    canonical: firstMatch(html, /<link[^>]+rel="canonical"[^>]+href="([^"]+)"[^>]*>/),
    ogUrl: firstMatch(html, /<meta[^>]+property="og:url"[^>]+content="([^"]*)"[^>]*>/),
    ogTitle: firstMatch(html, /<meta[^>]+property="og:title"[^>]+content="([^"]*)"[^>]*>/),
    ogDescription: firstMatch(html, /<meta[^>]+property="og:description"[^>]+content="([^"]*)"[^>]*>/),
    htmlLang: firstMatch(html, /<html[^>]+lang="([^"]+)"[^>]*>/),
    htmlDir: firstMatch(html, /<html[^>]+dir="([^"]+)"[^>]*>/),
    h1: allMatches(html, /<h1[^>]*>([\s\S]*?)<\/h1>/g).map((value) => value.replace(/<[^>]+>/g, '').trim()),
    hrefs: allMatches(html, /<a[^>]+href="([^"]+)"[^>]*>/g),
    ids: new Set(allMatches(html, /\sid="([^"]+)"/g)),
    alternates,
    jsonLd,
  };
}

async function sitemapUrls(file) {
  const xml = await readFile(file, 'utf8');
  return new Set([...xml.matchAll(/<loc>([^<]+)<\/loc>/g)].map((match) => decodeHtml(match[1])));
}

async function sitemapLastmods(file) {
  const xml = await readFile(file, 'utf8');
  return new Map(
    [...xml.matchAll(/<url>([\s\S]*?)<\/url>/g)].map((match) => {
      const block = match[1];
      const loc = firstMatch(block, /<loc>([^<]+)<\/loc>/);
      const lastmod = firstMatch(block, /<lastmod>([^<]+)<\/lastmod>/);
      return [loc, lastmod];
    }),
  );
}

async function fileExists(file) {
  try {
    await readFile(file);
    return true;
  } catch (error) {
    if (error?.code === 'ENOENT') return false;
    throw error;
  }
}

function routePath(locale, id) {
  const prefix = locale === 'en' ? '' : '/ar';
  return id === 'INTRO' ? `${prefix}/docs/` : `${prefix}/docs/${id}/`;
}

function landingRoute(locale) {
  return locale === 'en' ? '/' : '/ar/';
}

function answerRoute(locale) {
  return locale === 'en' ? '/answers/' : '/ar/answers/';
}

function errorRoute(locale) {
  return locale === 'en' ? '/404.html' : '/ar/404.html';
}

function fileForRoute(outDir, locale, route) {
  const base = locale === 'en' ? '/' : '/ar/';
  assert(route.startsWith(base), `route escaped locale base: ${route}`);
  const relative = route.slice(base.length);
  if (!relative) return path.join(outDir, 'index.html');
  if (relative === '404.html') return path.join(outDir, '404.html');
  return path.join(outDir, relative, 'index.html');
}

function expectedAlternates(origin, id) {
  return {
    en: `${origin}${routePath('en', id)}`,
    ar: `${origin}${routePath('ar', id)}`,
    'x-default': `${origin}${routePath('en', id)}`,
  };
}

function expectedLandingAlternates(origin) {
  return {
    en: `${origin}/`,
    ar: `${origin}/ar/`,
    'x-default': `${origin}/`,
  };
}

function expectedAnswerAlternates(origin) {
  return {
    en: `${origin}/answers/`,
    ar: `${origin}/ar/answers/`,
    'x-default': `${origin}/answers/`,
  };
}

function localizedRoute(locale, route) {
  if (locale === 'en' || !route.startsWith('/')) return route;
  if (route === '/') return '/ar/';
  return `/ar${route}`;
}

function llmsText(origin) {
  return `# FactLane

> FactLane is a free, local-first governed MCP memory layer for compatible AI agents. It keeps bounded reusable facts with scope, provenance, freshness, and trusted Candidate → Current verification.

FactLane is not a universal memory replacement or a general agent-safety system. Current user instructions, live project state, and verified live sources outrank remembered facts. The supported named release is v0.1.3 over command-launched stdio with a compatible MCP host.

## Canonical answers

- [FactLane product answers](${origin}/answers/): Concise, source-linked answers about fit, non-fit, memory boundaries, Candidate → Current, authority, tools, environment, and read-only setup.
- [إجابات FactLane المرجعية](${origin}/ar/answers/): الإجابات نفسها بالعربية مع روابط إلى الوثائق المرجعية.

## Product contract

- [Core concepts](${origin}/docs/CORE_CONCEPTS/): Scope, provenance, freshness, Candidate, Current, and authority boundaries.
- [Five MCP tools](${origin}/docs/TOOLS/): The exact public MCP surface and tool semantics.
- [Environment and compatibility](${origin}/docs/ENVIRONMENT/): Supported v0.1.3 runtime, storage, transport, and local Ollama profile.
- [Quick Start](${origin}/docs/QUICKSTART/): Establish a supported read-only connection before enabling Candidate writes.
- [Security](${origin}/docs/SECURITY/): Host identity, write authority, scope, and sensitive-memory boundaries.
- [Source repository](https://github.com/Habib1001-m/factlane): Apache-2.0 source for FactLane.

## Arabic product contract

- [المفاهيم الأساسية](${origin}/ar/docs/CORE_CONCEPTS/): النطاق والمصدر والحداثة ودورة Candidate → Current وحدود الصلاحيات.
- [أدوات MCP الخمس](${origin}/ar/docs/TOOLS/): الواجهة العامة الدقيقة للأدوات ودلالاتها.
- [البيئة والتوافق](${origin}/ar/docs/ENVIRONMENT/): حدود التشغيل والتخزين والنقل وملف تعريف Ollama المحلي المدعوم في v0.1.3.
- [البدء السريع](${origin}/ar/docs/QUICKSTART/): إنشاء اتصال مدعوم للقراءة فقط قبل تفعيل كتابة Candidate.
`;
}

function internalRoute(href) {
  if (!href.startsWith('/')) return null;
  return href.split('#', 1)[0].split('?', 1)[0];
}

export default function seoRegressionGuard(context) {
  const siteRoot = context.siteDir;
  return {
    name: 'factlane-seo-regression-guard',
    getClientModules() {
      return [path.join(siteRoot, 'src', 'routing', 'preserveSsrRouteBoundary.mjs')];
    },
    async postBuild({outDir, routesPaths, routesBuildMetadata, i18n, siteConfig}) {
      const locale = i18n.currentLocale;
      assert(['en', 'ar'].includes(locale), `unexpected locale ${locale}`);
      assert(siteConfig.trailingSlash === true, 'trailingSlash must remain true');

      const publicFlag = process.env.FACTLANE_PUBLIC_BUILD ?? '0';
      assert(['0', '1'].includes(publicFlag), 'FACTLANE_PUBLIC_BUILD must be exactly 0 or 1');
      const publicBuild = publicFlag === '1';
      const origin = new URL(siteConfig.url).origin;
      const canonicalRoutes = [
        landingRoute(locale),
        answerRoute(locale),
        ...docIds.map((id) => routePath(locale, id)),
      ].sort();
      const expectedRoutes = [...canonicalRoutes, errorRoute(locale)].sort();
      assert(
        JSON.stringify([...routesPaths].sort()) === JSON.stringify(expectedRoutes),
        `route set mismatch for ${locale}`,
      );
      assert(
        JSON.stringify(Object.keys(routesBuildMetadata).sort()) === JSON.stringify(expectedRoutes),
        `route metadata set mismatch for ${locale}`,
      );
      assert(routesBuildMetadata[errorRoute(locale)]?.noIndex === true, `404 route must be noindex for ${locale}`);
      if (publicBuild) {
        for (const route of canonicalRoutes) {
          assert(routesBuildMetadata[route]?.noIndex === false, `public route metadata became noindex: ${route}`);
        }
      }

      const docSeo = JSON.parse(await readFile(path.join(siteRoot, 'src', 'content', 'docSeo.json'), 'utf8'));
      const answerAuthority = JSON.parse(
        await readFile(path.join(siteRoot, 'src', 'content', 'answerAuthority.json'), 'utf8'),
      );
      const requiredAnswerIds = [
        'what-is-factlane',
        'who-is-it-for',
        'when-not-fit',
        'memory-difference',
        'candidate-current',
        'authority-boundary',
        'five-tools',
        'supported-environment',
        'read-only-start',
      ];
      for (const answerLocale of ['en', 'ar']) {
        const answers = answerAuthority[answerLocale]?.answers ?? [];
        assert(
          JSON.stringify(answers.map((answer) => answer.id)) === JSON.stringify(requiredAnswerIds),
          `answer authority ID set/order mismatch: ${answerLocale}`,
        );
      }
      for (let index = 0; index < requiredAnswerIds.length; index += 1) {
        const enSources = answerAuthority.en.answers[index].sources.map((source) => source.to);
        const arSources = answerAuthority.ar.answers[index].sources.map((source) => source.to);
        assert(
          JSON.stringify(enSources) === JSON.stringify(arSources),
          `answer authority source parity mismatch: ${requiredAnswerIds[index]}`,
        );
      }
      for (const answerLocale of ['en', 'ar']) {
        const byId = Object.fromEntries(
          answerAuthority[answerLocale].answers.map((answer) => [answer.id, answer.answer]),
        );
        for (const tool of ['memory_search', 'memory_get', 'memory_store', 'memory_update', 'memory_status']) {
          assert(byId['five-tools'].includes(tool), `answer authority missing ${tool}: ${answerLocale}`);
        }
        for (const token of ['v0.1.3', 'Python 3.11+', 'SQLite 3.42.0+', 'stdio', 'Ollama']) {
          assert(
            byId['supported-environment'].includes(token),
            `answer authority environment missing ${token}: ${answerLocale}`,
          );
        }
        assert(
          byId['read-only-start'].includes('memory_status'),
          `answer authority read-only start missing memory_status: ${answerLocale}`,
        );
      }
      const allIndexableRoutes = new Set([
        '/',
        '/ar/',
        '/answers/',
        '/ar/answers/',
        ...docIds.flatMap((id) => [routePath('en', id), routePath('ar', id)]),
      ]);
      const pages = new Map();
      const incoming = new Map(canonicalRoutes.map((route) => [route, 0]));
      const outgoing = new Map(canonicalRoutes.map((route) => [route, 0]));

      for (const route of canonicalRoutes) {
        const html = await readFile(fileForRoute(outDir, locale, route), 'utf8');
        const page = parsePage(html);
        pages.set(route, page);
        const expectedCanonical = `${origin}${route}`;
        assert(page.canonical === expectedCanonical, `canonical mismatch: ${route}`);
        assert(page.ogUrl === expectedCanonical, `og:url mismatch: ${route}`);
        assert(page.title, `missing title: ${route}`);
        assert(page.description, `missing description: ${route}`);
        assert(page.ogTitle === page.title, `og:title mismatch: ${route}`);
        assert(page.ogDescription === page.description, `og:description mismatch: ${route}`);
        assert(page.h1.length === 1, `expected one H1: ${route}`);
        assert(page.htmlLang === locale, `HTML lang mismatch: ${route}`);
        assert(page.htmlDir === (locale === 'ar' ? 'rtl' : 'ltr'), `HTML dir mismatch: ${route}`);
        if (publicBuild) {
          assert(!page.robots.toLowerCase().includes('noindex'), `public route became noindex: ${route}`);
        } else {
          assert(page.robots === 'noindex,nofollow,noarchive', `local robots mismatch: ${route}`);
        }

        const id = docIds.find((candidate) => route === routePath(locale, candidate));
        if (id) {
          const expectedSeo = docSeo[locale]?.[id];
          const otherLocale = locale === 'en' ? 'ar' : 'en';
          const otherSeo = docSeo[otherLocale]?.[id];
          assert(expectedSeo, `missing SEO manifest entry: ${locale}/${id}`);
          assert(otherSeo, `missing SEO manifest entry: ${otherLocale}/${id}`);
          assert(page.title === `${expectedSeo.title} | FactLane`, `title manifest mismatch: ${locale}/${id}`);
          assert(page.description === expectedSeo.description, `description manifest mismatch: ${locale}/${id}`);
          assert(page.title.length >= 10 && page.title.length <= 75, `title length invalid: ${locale}/${id}`);
          assert(page.description.length >= 80 && page.description.length <= 190, `description length invalid: ${locale}/${id}`);
          assert(/[.!?؟]$/u.test(page.description), `description is not a complete sentence: ${locale}/${id}`);
          assert(expectedSeo.title !== otherSeo.title, `cross-locale title fallback: ${id}`);
          assert(expectedSeo.description !== otherSeo.description, `cross-locale description fallback: ${id}`);
          if (locale === 'ar') {
            assert(/[\u0600-\u06ff]/u.test(page.title), `Arabic title fallback: ${id}`);
            assert(/[\u0600-\u06ff]/u.test(page.description), `Arabic description fallback: ${id}`);
            assert(/[\u0600-\u06ff]/u.test(page.h1[0]), `Arabic H1 fallback: ${id}`);
          }
          assert(
            JSON.stringify(page.alternates) === JSON.stringify(expectedAlternates(origin, id)),
            `hreflang mismatch: ${locale}/${id}`,
          );
          const breadcrumbs = page.jsonLd.filter((entry) => entry?.['@type'] === 'BreadcrumbList');
          assert(breadcrumbs.length === 1, `expected one BreadcrumbList: ${locale}/${id}`);
          assert(!page.jsonLd.some((entry) => entry?.['@type'] === 'SoftwareSourceCode'), `landing schema leaked into docs: ${locale}/${id}`);
          const items = breadcrumbs[0].itemListElement;
          assert(Array.isArray(items) && items.length >= 2, `invalid BreadcrumbList: ${locale}/${id}`);
          const expectedHome = locale === 'ar' ? `${origin}/ar/` : `${origin}/`;
          const expectedHomeName = locale === 'ar' ? 'الرئيسية' : 'Home page';
          assert(items[0]?.position === 1, `breadcrumb home position mismatch: ${locale}/${id}`);
          assert(items[0]?.name === expectedHomeName, `breadcrumb home name mismatch: ${locale}/${id}`);
          assert(items[0]?.item === expectedHome, `breadcrumb home URL mismatch: ${locale}/${id}`);
          assert(items.every((item, index) => item?.position === index + 1), `breadcrumb positions invalid: ${locale}/${id}`);
          assert(items.at(-1)?.item === expectedCanonical, `breadcrumb canonical mismatch: ${locale}/${id}`);
        } else if (route === answerRoute(locale)) {
          const expectedAnswer = answerAuthority[locale];
          assert(expectedAnswer, `missing answer authority content: ${locale}`);
          assert(
            page.title === `${expectedAnswer.meta.title} | FactLane`,
            `answer title mismatch: ${locale}`,
          );
          assert(page.description === expectedAnswer.meta.description, `answer description mismatch: ${locale}`);
          assert(page.title.length >= 10 && page.title.length <= 75, `answer title length invalid: ${locale}`);
          assert(
            page.description.length >= 80 && page.description.length <= 190,
            `answer description length invalid: ${locale}`,
          );
          assert(/[.!?؟]$/u.test(page.description), `answer description is not a complete sentence: ${locale}`);
          if (locale === 'ar') {
            assert(/[\u0600-\u06ff]/u.test(page.title), 'Arabic answer title fallback');
            assert(/[\u0600-\u06ff]/u.test(page.description), 'Arabic answer description fallback');
            assert(/[\u0600-\u06ff]/u.test(page.h1[0]), 'Arabic answer H1 fallback');
          }
          assert(
            JSON.stringify(page.alternates) === JSON.stringify(expectedAnswerAlternates(origin)),
            `answer hreflang mismatch: ${locale}`,
          );
          assert(page.jsonLd.length === 0, `answer route must not add decorative JSON-LD: ${locale}`);
          for (const answer of expectedAnswer.answers) {
            assert(page.ids.has(answer.id), `missing answer section ${locale}#${answer.id}`);
            for (const source of answer.sources) {
              const expectedHref = localizedRoute(locale, source.to);
              assert(page.hrefs.includes(expectedHref), `missing answer source link ${locale}: ${expectedHref}`);
            }
          }
        } else {
          assert(route === landingRoute(locale), `unexpected canonical route: ${route}`);
          assert(
            JSON.stringify(page.alternates) === JSON.stringify(expectedLandingAlternates(origin)),
            `landing hreflang mismatch: ${locale}`,
          );
          const software = page.jsonLd.filter((entry) => entry?.['@type'] === 'SoftwareSourceCode');
          assert(software.length === 1, `expected one SoftwareSourceCode: ${locale}`);
          assert(software[0]?.url === expectedCanonical, `landing schema URL mismatch: ${locale}`);
          assert(software[0]?.inLanguage === locale, `landing schema language mismatch: ${locale}`);
          assert(!page.jsonLd.some((entry) => entry?.['@type'] === 'BreadcrumbList'), `BreadcrumbList leaked into landing: ${locale}`);
        }
      }

      const titles = new Set();
      const descriptions = new Set();
      for (const [route, page] of pages) {
        assert(!titles.has(page.title), `duplicate title in ${locale}: ${route}`);
        assert(!descriptions.has(page.description), `duplicate description in ${locale}: ${route}`);
        titles.add(page.title);
        descriptions.add(page.description);
        for (const href of page.hrefs) {
          const targetRoute = internalRoute(href);
          if (!targetRoute || href === '#') continue;
          assert(allIndexableRoutes.has(targetRoute), `non-indexable internal href from ${route}: ${href}`);
          if (incoming.has(targetRoute)) {
            incoming.set(targetRoute, incoming.get(targetRoute) + 1);
            outgoing.set(route, outgoing.get(route) + 1);
          }
          const fragment = href.includes('#') ? decodeURIComponent(href.split('#', 2)[1]) : '';
          if (fragment && pages.has(targetRoute)) {
            assert(pages.get(targetRoute).ids.has(fragment), `broken internal fragment from ${route}: ${href}`);
          }
        }
      }

      for (const route of canonicalRoutes) {
        assert(outgoing.get(route) > 0, `dead-end canonical route: ${route}`);
        if (route !== landingRoute(locale)) {
          assert(incoming.get(route) > 0, `orphan canonical route: ${route}`);
        }
      }

      const robots = await readFile(path.join(outDir, 'robots.txt'), 'utf8');
      const sitemapPath = path.join(outDir, 'sitemap.xml');
      const llmsPath = path.join(outDir, 'llms.txt');
      if (publicBuild) {
        assert(robots.includes('User-agent: OAI-SearchBot'), 'public robots missing OAI-SearchBot policy');
        assert(robots.includes('User-agent: Claude-SearchBot'), 'public robots missing Claude-SearchBot policy');
        assert(robots.includes('User-agent: Claude-User'), 'public robots missing Claude-User policy');
        assert(robots.includes('Allow: /'), 'public robots.txt must allow crawling');
        assert(!robots.includes('Disallow: /'), 'public robots.txt must not block crawling');
        assert(robots.includes(`Sitemap: ${origin}/sitemap.xml`), 'public robots missing EN sitemap');
        assert(robots.includes(`Sitemap: ${origin}/ar/sitemap.xml`), 'public robots missing AR sitemap');
        if (locale === 'en') {
          const llms = llmsText(origin);
          await writeFile(llmsPath, llms, 'utf8');
          assert(llms.startsWith('# FactLane\n'), 'public llms.txt must identify FactLane');
          assert(llms.includes(`${origin}/answers/`), 'public llms.txt missing EN canonical answers');
          assert(llms.includes(`${origin}/ar/answers/`), 'public llms.txt missing AR canonical answers');
          assert(llms.includes(`${origin}/docs/CORE_CONCEPTS/`), 'public llms.txt missing core concepts');
          assert(llms.includes(`${origin}/docs/TOOLS/`), 'public llms.txt missing tool contract');
          assert(llms.includes(`${origin}/docs/ENVIRONMENT/`), 'public llms.txt missing environment contract');
          assert(llms.includes(`${origin}/docs/QUICKSTART/`), 'public llms.txt missing Quick Start');
          assert(llms.includes(`${origin}/ar/docs/CORE_CONCEPTS/`), 'public llms.txt missing AR core concepts');
          assert(llms.includes(`${origin}/ar/docs/TOOLS/`), 'public llms.txt missing AR tool contract');
          assert(llms.includes(`${origin}/ar/docs/ENVIRONMENT/`), 'public llms.txt missing AR environment contract');
          assert(llms.includes(`${origin}/ar/docs/QUICKSTART/`), 'public llms.txt missing AR Quick Start');
          assert(llms.includes('v0.1.3'), 'public llms.txt missing supported release boundary');
          assert(llms.includes('not a universal memory replacement'), 'public llms.txt missing product-scope boundary');
        } else {
          assert(!(await fileExists(llmsPath)), 'localized /ar/llms.txt must not be emitted');
        }
        const sitemap = await sitemapUrls(sitemapPath);
        const expectedSitemap = new Set(canonicalRoutes.map((route) => `${origin}${route}`));
        assert(
          JSON.stringify([...sitemap].sort()) === JSON.stringify([...expectedSitemap].sort()),
          `public sitemap route set mismatch for ${locale}`,
        );
        const sitemapFreshness = await sitemapLastmods(sitemapPath);
        const lastmods = [...sitemapFreshness.values()].filter(Boolean);
        assert(
          lastmods.length === 0 || lastmods.length === expectedSitemap.size,
          `public sitemap lastmod coverage must be complete or absent for ${locale}`,
        );
        for (const [url, lastmod] of sitemapFreshness) {
          if (!lastmod) continue;
          const timestamp = Date.parse(lastmod);
          assert(!Number.isNaN(timestamp), `invalid sitemap lastmod for ${url}: ${lastmod}`);
          assert(timestamp <= Date.now() + 5 * 60 * 1000, `future sitemap lastmod for ${url}: ${lastmod}`);
        }
      } else {
        assert(robots.includes('Disallow: /'), 'local robots.txt must block crawling');
        assert(!robots.includes('Sitemap:'), 'local robots.txt must not declare a sitemap');
        assert(!(await fileExists(sitemapPath)), `local sitemap must be absent for ${locale}`);
        assert(!(await fileExists(llmsPath)), `local llms.txt must be absent for ${locale}`);
      }

      const errorHtml = await readFile(fileForRoute(outDir, locale, errorRoute(locale)), 'utf8');
      const errorPage = parsePage(errorHtml);
      assert(errorPage.robots === 'noindex,nofollow,noarchive', `404 robots mismatch: ${locale}`);
      assert(/Not Found|غير موجودة/u.test(errorPage.title), `404 title mismatch: ${locale}`);

      console.log(
        `SEO regression guard passed: locale=${locale}; mode=${publicBuild ? 'public' : 'local'}; canonicalRoutes=${canonicalRoutes.length}`,
      );
    },
  };
}

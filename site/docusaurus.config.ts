import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';
import {isIP} from 'node:net';

const configuredSiteUrl = process.env.FACTLANE_SITE_URL ?? 'https://factlane.local';
const parsedSiteUrl = new URL(configuredSiteUrl);
const publicBuildFlag = process.env.FACTLANE_PUBLIC_BUILD ?? '0';

if (
  parsedSiteUrl.protocol !== 'https:' ||
  parsedSiteUrl.username ||
  parsedSiteUrl.password ||
  parsedSiteUrl.pathname !== '/' ||
  parsedSiteUrl.search ||
  parsedSiteUrl.hash
) {
  throw new Error(
    'FACTLANE_SITE_URL must be an HTTPS origin with no credentials, path, query, or fragment',
  );
}

if (!['0', '1'].includes(publicBuildFlag)) {
  throw new Error('FACTLANE_PUBLIC_BUILD must be exactly 0 or 1');
}

const siteUrl = parsedSiteUrl.origin;
const publicBuild = publicBuildFlag === '1';

const publicHostname = parsedSiteUrl.hostname.toLowerCase();
const reservedPublicHostname =
  publicHostname.endsWith('.') ||
  isIP(publicHostname) !== 0 ||
  !publicHostname.includes('.') ||
  publicHostname === 'localhost' ||
  publicHostname.endsWith('.localhost') ||
  publicHostname.endsWith('.local') ||
  publicHostname.endsWith('.test') ||
  publicHostname.endsWith('.invalid') ||
  publicHostname.endsWith('.example') ||
  ['example.com', 'example.net', 'example.org'].some(
    (domain) => publicHostname === domain || publicHostname.endsWith('.' + domain),
  );

if (publicBuild && reservedPublicHostname) {
  throw new Error(
    'FACTLANE_PUBLIC_BUILD=1 requires a non-local, non-reserved DNS hostname approved for publication',
  );
}

const config: Config = {
  title: 'FactLane',
  tagline: 'Share facts. Not context.',
  favicon: 'favicon/favicon-32.png',

  // A public deployment must provide its real canonical origin.
  // The local fallback remains deliberately non-indexable.
  url: siteUrl,
  baseUrl: '/',
  organizationName: 'Habib1001-m',
  projectName: 'factlane',

  i18n: {
    defaultLocale: 'en',
    locales: ['en', 'ar'],
    localeConfigs: {
      en: {
        label: 'English',
        htmlLang: 'en',
        direction: 'ltr',
        translate: false,
        baseUrl: '/',
      },
      ar: {
        label: 'العربية',
        htmlLang: 'ar',
        direction: 'rtl',
        translate: true,
        baseUrl: '/ar/',
      },
    },
  },

  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',
  staticDirectories: ['.generated-static'],
  headTags: [
    {
      tagName: 'meta',
      attributes: {name: 'theme-color', content: '#0a0f1a'},
    },
    {
      tagName: 'meta',
      attributes: {
        name: 'keywords',
        content:
          'FactLane, AI assistant memory, AI agent memory, MCP memory, local-first memory, governed memory, ذاكرة مساعد الذكاء الاصطناعي, ذاكرة MCP',
      },
    },
    {
      tagName: 'meta',
      attributes: {property: 'og:type', content: 'website'},
    },
    ...(!publicBuild
      ? [
          {
            tagName: 'meta',
            attributes: {
              name: 'robots',
              content: 'noindex,nofollow,noarchive',
            },
          },
        ]
      : []),
  ],

  presets: [
    [
      'classic',
      {
        docs: {
          path: '.generated-docs',
          routeBasePath: 'docs',
          sidebarPath: './sidebars.ts',
          editUrl: undefined,
          showLastUpdateAuthor: false,
          showLastUpdateTime: false,
        },
        blog: false,
        sitemap: publicBuild
          ? {
              changefreq: 'weekly',
              priority: 0.5,
            }
          : false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: 'social/factlane-github-social-preview.png',
    colorMode: {
      defaultMode: 'dark',
      disableSwitch: false,
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'FactLane',
      logo: {
        alt: 'FactLane',
        src: 'logo/factlane-mark.svg',
      },
      items: [
        {to: '/docs/USE_CASES', label: 'Use cases', position: 'left'},
        {to: '/docs/CORE_CONCEPTS', label: 'How it works', position: 'left'},
        {
          to: '/docs/',
          label: 'Docs',
          position: 'left',
          activeBaseRegex: '^/docs/',
        },
        {
          href: 'https://github.com/Habib1001-m/factlane',
          label: 'GitHub',
          position: 'right',
        },
        {
          type: 'localeDropdown',
          position: 'right',
        },
        {
          to: '/docs/QUICKSTART',
          label: 'Get started',
          position: 'right',
          className: 'navbar__item--cta',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Learn',
          items: [
            {label: 'Use cases', to: '/docs/USE_CASES'},
            {label: 'FAQ', to: '/docs/FAQ'},
            {label: 'Core concepts', to: '/docs/CORE_CONCEPTS'},
            {label: 'Quick Start', to: '/docs/QUICKSTART'},
          ],
        },
        {
          title: 'Reference',
          items: [
            {label: 'Tools', to: '/docs/TOOLS'},
            {label: 'Architecture', to: '/docs/ARCHITECTURE'},
            {label: 'Environment', to: '/docs/ENVIRONMENT'},
            {label: 'Release operations', to: '/docs/RELEASE_OPERATIONS'},
          ],
        },
        {
          title: 'Project',
          items: [
            {label: 'GitHub', href: 'https://github.com/Habib1001-m/factlane'},
            {
              label: 'Security',
              to: '/docs/SECURITY',
            },
            {
              label: 'Apache-2.0',
              href: 'https://github.com/Habib1001-m/factlane/blob/main/LICENSE',
            },
          ],
        },
      ],
      copyright:
        'FactLane v0.1.3 · Local-first governed memory for AI agents.',
    },
    prism: {
      additionalLanguages: ['bash', 'json', 'toml', 'yaml'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;

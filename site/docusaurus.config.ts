import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'FactLane',
  tagline: 'Share facts. Not context.',
  favicon: 'favicon/favicon-32.png',

  // Placeholder origin for a local prototype. Hosting/domain are deliberately deferred.
  url: 'https://factlane.local',
  baseUrl: '/',
  organizationName: 'Habib1001-m',
  projectName: 'factlane',

  onBrokenLinks: 'throw',
  onBrokenAnchors: 'warn',
  staticDirectories: ['static', '../docs/assets/brand'],

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
            {label: 'Quick Start', to: '/docs/QUICKSTART'},
            {label: 'Core concepts', to: '/docs/CORE_CONCEPTS'},
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
              href: 'https://github.com/Habib1001-m/factlane/blob/main/SECURITY.md',
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

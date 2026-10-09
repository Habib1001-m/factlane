import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docs: [
    'INTRO',
    {
      type: 'category',
      label: 'Start here',
      collapsed: false,
      items: ['USE_CASES', 'FAQ', 'CORE_CONCEPTS', 'QUICKSTART'],
    },
    {
      type: 'category',
      label: 'For developers',
      collapsed: true,
      items: ['TOOLS', 'USING_FACTLANE_SKILL'],
    },
    {
      type: 'category',
      label: 'Evaluate and operate',
      items: ['ARCHITECTURE', 'ENVIRONMENT', 'SECURITY', 'RELEASE_OPERATIONS'],
    },
    {
      type: 'category',
      label: 'Background',
      items: ['PROJECT_HISTORY'],
    },
  ],
};

export default sidebars;

import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docs: [
    'INTRO',
    {
      type: 'category',
      label: 'Start here',
      collapsed: false,
      items: ['USE_CASES', 'QUICKSTART'],
    },
    {
      type: 'category',
      label: 'Understand FactLane',
      collapsed: false,
      items: ['CORE_CONCEPTS', 'TOOLS'],
    },
    {
      type: 'category',
      label: 'Operate it',
      items: ['ARCHITECTURE', 'ENVIRONMENT', 'RELEASE_OPERATIONS'],
    },
    {
      type: 'category',
      label: 'Security',
      items: ['SECURITY', 'USING_FACTLANE_SKILL'],
    },
    {
      type: 'category',
      label: 'Background',
      items: ['PROJECT_HISTORY'],
    },
  ],
};

export default sidebars;

import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';
import publicDocs from './public-docs.json';

const ids = (group: string) =>
  publicDocs.filter((doc) => doc.group === group).map((doc) => doc.id);

const sidebars: SidebarsConfig = {
  docs: [
    ...ids('root'),
    {
      type: 'category',
      label: 'Start here',
      collapsed: false,
      items: ids('start'),
    },
    {
      type: 'category',
      label: 'For developers',
      collapsed: true,
      items: ids('developer'),
    },
    {
      type: 'category',
      label: 'Evaluate and operate',
      items: ids('operate'),
    },
    {
      type: 'category',
      label: 'Background',
      items: ids('background'),
    },
  ],
};

export default sidebars;

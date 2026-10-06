import React, {type ReactNode} from 'react';
import {translate} from '@docusaurus/Translate';
import {PageMetadata} from '@docusaurus/theme-common';
import Layout from '@theme/Layout';
import NotFoundContent from '@theme/NotFound/Content';

export default function NotFound(): ReactNode {
  const title = translate({
    id: 'theme.NotFound.title',
    message: 'Page Not Found',
  });

  return (
    <>
      <PageMetadata title={title}>
        <meta name="robots" content="noindex,nofollow,noarchive" />
      </PageMetadata>
      <Layout>
        <NotFoundContent />
      </Layout>
    </>
  );
}

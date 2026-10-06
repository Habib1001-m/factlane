import React, {type ReactNode} from 'react';
import Head from '@docusaurus/Head';
import useBaseUrl from '@docusaurus/useBaseUrl';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import {translate} from '@docusaurus/Translate';
import type {Props} from '@theme/DocBreadcrumbs/StructuredData';

export default function DocBreadcrumbsStructuredData({
  breadcrumbs,
}: Props): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  const homeHref = useBaseUrl('/');
  const linkedBreadcrumbs = breadcrumbs.filter((breadcrumb) => breadcrumb.href);

  const structuredData = {
    '@context': 'https://schema.org',
    '@type': 'BreadcrumbList',
    itemListElement: [
      {
        '@type': 'ListItem',
        position: 1,
        name: translate({
          id: 'theme.docs.breadcrumbs.home',
          message: 'Home page',
          description: 'The ARIA label for the home page in the breadcrumbs',
        }),
        item: `${siteConfig.url}${homeHref}`,
      },
      ...linkedBreadcrumbs.map((breadcrumb, index) => ({
        '@type': 'ListItem',
        position: index + 2,
        name: breadcrumb.label,
        item: `${siteConfig.url}${breadcrumb.href}`,
      })),
    ],
  };

  return (
    <Head>
      <script type="application/ld+json">
        {JSON.stringify(structuredData)}
      </script>
    </Head>
  );
}

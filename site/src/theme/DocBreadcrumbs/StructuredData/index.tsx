import React, {type ReactNode} from 'react';
import Head from '@docusaurus/Head';
import useBaseUrl from '@docusaurus/useBaseUrl';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import {translate} from '@docusaurus/Translate';
import {applyTrailingSlash} from '@docusaurus/utils-common';
import type {Props} from '@theme/DocBreadcrumbs/StructuredData';

export default function DocBreadcrumbsStructuredData({
  breadcrumbs,
}: Props): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  const homeHref = useBaseUrl('/');
  const trailingSlashOptions = {
    trailingSlash: siteConfig.trailingSlash,
    baseUrl: siteConfig.baseUrl,
  };
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
        item: `${siteConfig.url}${applyTrailingSlash(homeHref, trailingSlashOptions)}`,
      },
      ...linkedBreadcrumbs.map((breadcrumb, index) => {
        const itemHref = applyTrailingSlash(
          breadcrumb.href,
          trailingSlashOptions,
        );
        return {
          '@type': 'ListItem',
          position: index + 2,
          name: breadcrumb.label,
          item: `${siteConfig.url}${itemHref}`,
        };
      }),
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

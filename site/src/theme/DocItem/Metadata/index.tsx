import React, {type ReactNode} from 'react';
import {useDoc} from '@docusaurus/plugin-content-docs/client';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import {PageMetadata} from '@docusaurus/theme-common';
import {getDocSeo} from '@site/src/content/docSeo';

export default function DocItemMetadata(): ReactNode {
  const {metadata, frontMatter, assets} = useDoc();
  const {i18n} = useDocusaurusContext();
  const seo = getDocSeo(i18n.currentLocale, metadata.id);

  return (
    <PageMetadata
      title={seo.title}
      description={seo.description}
      keywords={frontMatter.keywords}
      image={assets.image ?? frontMatter.image}
    />
  );
}

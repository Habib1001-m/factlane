import docSeoData from './docSeo.json';

export type DocSeoLocale = 'en' | 'ar';

export type DocSeoId =
  | 'INTRO'
  | 'USE_CASES'
  | 'FAQ'
  | 'CORE_CONCEPTS'
  | 'QUICKSTART'
  | 'TOOLS'
  | 'USING_FACTLANE_SKILL'
  | 'ARCHITECTURE'
  | 'ENVIRONMENT'
  | 'SECURITY'
  | 'RELEASE_OPERATIONS'
  | 'PROJECT_HISTORY';

type DocSeoEntry = {
  title: string;
  description: string;
};

type DocSeoMap = Record<DocSeoLocale, Record<DocSeoId, DocSeoEntry>>;

export const docSeo: DocSeoMap = docSeoData;

export function getDocSeo(locale: string, id: string): DocSeoEntry {
  if (locale !== 'en' && locale !== 'ar') {
    throw new Error(`Unsupported documentation SEO locale: ${locale}`);
  }

  const entry = docSeo[locale][id as DocSeoId];
  if (!entry) {
    throw new Error(`Missing documentation SEO metadata: ${locale}/${id}`);
  }

  return entry;
}

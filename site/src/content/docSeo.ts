import docSeoData from './docSeo.json';
import publicDocs from '../../public-docs.json';

export type DocSeoLocale = 'en' | 'ar';
export type DocSeoId = (typeof publicDocs)[number]['id'];

type DocSeoEntry = {
  title: string;
  description: string;
};

type DocSeoMap = Record<DocSeoLocale, Record<DocSeoId, DocSeoEntry>>;

export const docSeo: DocSeoMap = docSeoData;
const publicDocIds = new Set(publicDocs.map((doc) => doc.id));

export function getDocSeo(locale: string, id: string): DocSeoEntry {
  if (locale !== 'en' && locale !== 'ar') {
    throw new Error(`Unsupported documentation SEO locale: ${locale}`);
  }
  if (!publicDocIds.has(id)) {
    throw new Error(`Unsupported public documentation id: ${id}`);
  }

  const entry = docSeo[locale][id as DocSeoId];
  if (!entry) {
    throw new Error(`Missing documentation SEO metadata: ${locale}/${id}`);
  }

  return entry;
}

import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const siteRoot = path.resolve(here, '..');
const generatedDocs = path.join(siteRoot, '.generated-docs');
const arabicDocs = path.join(
  siteRoot,
  'i18n',
  'ar',
  'docusaurus-plugin-content-docs',
  'current',
);

const requiredDocs = [
  'ARCHITECTURE.md',
  'CORE_CONCEPTS.md',
  'ENVIRONMENT.md',
  'FAQ.md',
  'INTRO.md',
  'PROJECT_HISTORY.md',
  'QUICKSTART.md',
  'RELEASE_OPERATIONS.md',
  'SECURITY.md',
  'TOOLS.md',
  'USE_CASES.md',
  'USING_FACTLANE_SKILL.md',
];

function fencedBlocks(markdown) {
  return markdown.match(/```[\s\S]*?```/g) ?? [];
}

function inlineCodeTokens(markdown) {
  const withoutFences = markdown.replace(/```[\s\S]*?```/g, '');
  return [...withoutFences.matchAll(/`([^`\n]+)`/g)].map((match) => match[1]);
}

function isSubsequence(expected, actual) {
  let cursor = 0;
  for (const value of actual) {
    if (value === expected[cursor]) {
      cursor += 1;
      if (cursor === expected.length) {
        return true;
      }
    }
  }
  return expected.length === 0;
}

function headingLevels(markdown) {
  const withoutFences = markdown.replace(/```[\s\S]*?```/g, '');
  return [...withoutFences.matchAll(/^(#{1,6})\s+/gm)].map((match) => match[1].length);
}

function markdownLinkTargets(markdown) {
  const withoutFences = markdown.replace(/```[\s\S]*?```/g, '');
  return [...withoutFences.matchAll(/(?<!!)\[[^\]]*\]\(([^)]+)\)/g)].map(
    (match) => match[1],
  );
}

for (const fileName of requiredDocs) {
  const sourcePath = path.join(generatedDocs, fileName);
  const translationPath = path.join(arabicDocs, fileName);
  const [source, translation] = await Promise.all([
    readFile(sourcePath, 'utf8'),
    readFile(translationPath, 'utf8'),
  ]);

  if (!translation.trim()) {
    throw new Error(`Arabic translation is empty: ${fileName}`);
  }
  if (translation.trim() === source.trim()) {
    throw new Error(`Arabic translation still matches English source: ${fileName}`);
  }
  if (!/[\u0600-\u06ff]/u.test(translation)) {
    throw new Error(`Arabic translation contains no Arabic script: ${fileName}`);
  }

  for (const block of fencedBlocks(source)) {
    if (!translation.includes(block)) {
      throw new Error(`Arabic translation changed or dropped a fenced code block: ${fileName}`);
    }
  }

  const sourceInlineCode = inlineCodeTokens(source);
  const translationInlineCode = inlineCodeTokens(translation);
  if (!isSubsequence(sourceInlineCode, translationInlineCode)) {
    throw new Error(
      `Arabic translation changed, reordered, or dropped source inline-code occurrences: ${fileName}`,
    );
  }

  const sourceHeadingLevels = headingLevels(source);
  const translationHeadingLevels = headingLevels(translation);
  if (JSON.stringify(sourceHeadingLevels) !== JSON.stringify(translationHeadingLevels)) {
    throw new Error(`Arabic translation changed heading structure: ${fileName}`);
  }

  const sourceLinkTargets = markdownLinkTargets(source);
  const translationLinkTargets = markdownLinkTargets(translation);
  if (JSON.stringify(sourceLinkTargets) !== JSON.stringify(translationLinkTargets)) {
    throw new Error(`Arabic translation changed Markdown link targets: ${fileName}`);
  }
}

const corpus = (
  await Promise.all(requiredDocs.map((name) => readFile(path.join(arabicDocs, name), 'utf8')))
).join('\n');

for (const invariant of [
  'v0.1.4',
  'memory_search',
  'memory_get',
  'memory_store',
  'memory_update',
  'memory_status',
  '2,000',
  'UTF-8',
  'Ollama',
  'SQLite',
  'stdio',
]) {
  if (!corpus.includes(invariant)) {
    throw new Error(`Arabic documentation corpus is missing required invariant: ${invariant}`);
  }
}

const uiTranslationFiles = [
  'i18n/ar/code.json',
  'i18n/ar/docusaurus-theme-classic/navbar.json',
  'i18n/ar/docusaurus-theme-classic/footer.json',
  'i18n/ar/docusaurus-plugin-content-docs/current.json',
];
const intentionalNonArabicMessages = new Set(['FactLane', 'GitHub', 'Apache-2.0']);

// Snapshot of the required translation keys emitted by
// `docusaurus write-translations --locale en` for this exact site/config.
// Public Arabic publication is fail-closed on this contract so Docusaurus
// cannot silently fall back to English when a required Arabic UI key is lost.
const requiredUiTranslationKeys = {
  'i18n/ar/code.json': [
    'theme.ErrorPageContent.title',
    'theme.BackToTopButton.buttonAriaLabel',
    'theme.blog.archive.title',
    'theme.blog.archive.description',
    'theme.blog.paginator.navAriaLabel',
    'theme.blog.paginator.newerEntries',
    'theme.blog.paginator.olderEntries',
    'theme.blog.post.paginator.navAriaLabel',
    'theme.blog.post.paginator.newerPost',
    'theme.blog.post.paginator.olderPost',
    'theme.tags.tagsPageLink',
    'theme.colorToggle.ariaLabel.mode.system',
    'theme.colorToggle.ariaLabel.mode.light',
    'theme.colorToggle.ariaLabel.mode.dark',
    'theme.colorToggle.ariaLabel',
    'theme.docs.breadcrumbs.navAriaLabel',
    'theme.docs.paginator.navAriaLabel',
    'theme.docs.paginator.previous',
    'theme.docs.paginator.next',
    'theme.docs.tagDocListPageTitle.nDocsTagged',
    'theme.docs.tagDocListPageTitle',
    'theme.docs.versionBadge.label',
    'theme.docs.versions.unreleasedVersionLabel',
    'theme.docs.versions.unmaintainedVersionLabel',
    'theme.docs.versions.latestVersionSuggestionLabel',
    'theme.docs.versions.latestVersionLinkLabel',
    'theme.common.editThisPage',
    'theme.common.headingLinkTitle',
    'theme.lastUpdated.atDate',
    'theme.lastUpdated.byUser',
    'theme.lastUpdated.lastUpdatedAtBy',
    'theme.navbar.mobileVersionsDropdown.label',
    'theme.NotFound.title',
    'theme.tags.tagsListLabel',
    'theme.admonition.caution',
    'theme.admonition.danger',
    'theme.admonition.info',
    'theme.admonition.note',
    'theme.admonition.tip',
    'theme.admonition.warning',
    'theme.AnnouncementBar.closeButtonAriaLabel',
    'theme.blog.sidebar.navAriaLabel',
    'theme.DocSidebarItem.expandCategoryAriaLabel',
    'theme.DocSidebarItem.collapseCategoryAriaLabel',
    'theme.IconExternalLink.ariaLabel',
    'theme.NavBar.navAriaLabel',
    'theme.navbar.mobileLanguageDropdown.label',
    'theme.NotFound.p1',
    'theme.NotFound.p2',
    'theme.TOCCollapsible.toggleButtonLabel',
    'theme.blog.post.readMore',
    'theme.blog.post.readMoreLabel',
    'theme.blog.post.readingTime.plurals',
    'theme.CodeBlock.copy',
    'theme.CodeBlock.copied',
    'theme.CodeBlock.copyButtonAriaLabel',
    'theme.CodeBlock.wordWrapToggle',
    'theme.docs.breadcrumbs.home',
    'theme.docs.sidebar.collapseButtonTitle',
    'theme.docs.sidebar.collapseButtonAriaLabel',
    'theme.docs.sidebar.navAriaLabel',
    'theme.docs.sidebar.closeSidebarButtonAriaLabel',
    'theme.navbar.mobileSidebarSecondaryMenu.backButtonLabel',
    'theme.docs.sidebar.toggleSidebarButtonAriaLabel',
    'theme.navbar.mobileDropdown.collapseButton.expandAriaLabel',
    'theme.navbar.mobileDropdown.collapseButton.collapseAriaLabel',
    'theme.docs.sidebar.expandButtonTitle',
    'theme.docs.sidebar.expandButtonAriaLabel',
    'theme.blog.post.plurals',
    'theme.blog.tagTitle',
    'theme.blog.author.pageTitle',
    'theme.blog.authorsList.pageTitle',
    'theme.blog.authorsList.viewAll',
    'theme.blog.author.noPosts',
    'theme.contentVisibility.unlistedBanner.title',
    'theme.contentVisibility.unlistedBanner.message',
    'theme.contentVisibility.draftBanner.title',
    'theme.contentVisibility.draftBanner.message',
    'theme.docs.DocCard.categoryDescription.plurals',
    'theme.ErrorPageContent.tryAgain',
    'theme.common.skipToMainContent',
    'theme.tags.tagsPageTitle',
  ],
  'i18n/ar/docusaurus-theme-classic/navbar.json': [
    'title',
    'logo.alt',
    'item.label.Use cases',
    'item.label.How it works',
    'item.label.Docs',
    'item.label.GitHub',
    'item.label.Get started',
  ],
  'i18n/ar/docusaurus-theme-classic/footer.json': [
    'link.title.Learn',
    'link.title.Reference',
    'link.title.Project',
    'link.item.label.Use cases',
    'link.item.label.FAQ',
    'link.item.label.Core concepts',
    'link.item.label.Quick Start',
    'link.item.label.Tools',
    'link.item.label.Architecture',
    'link.item.label.Environment',
    'link.item.label.Release operations',
    'link.item.label.GitHub',
    'link.item.label.Security',
    'link.item.label.Apache-2.0',
    'copyright',
  ],
  'i18n/ar/docusaurus-plugin-content-docs/current.json': [
    'version.label',
    'sidebar.docs.category.Start here',
    'sidebar.docs.category.For developers',
    'sidebar.docs.category.Evaluate and operate',
    'sidebar.docs.category.Background',
  ],
};

for (const relativePath of uiTranslationFiles) {
  const content = JSON.parse(await readFile(path.join(siteRoot, relativePath), 'utf8'));
  const expectedKeys = [...requiredUiTranslationKeys[relativePath]].sort();
  const actualKeys = Object.keys(content).sort();
  if (JSON.stringify(actualKeys) !== JSON.stringify(expectedKeys)) {
    const expectedSet = new Set(expectedKeys);
    const actualSet = new Set(actualKeys);
    const missing = expectedKeys.filter((key) => !actualSet.has(key));
    const unexpected = actualKeys.filter((key) => !expectedSet.has(key));
    throw new Error(
      `Arabic UI translation key contract changed: ${relativePath}; ` +
        `missing=${JSON.stringify(missing)}; unexpected=${JSON.stringify(unexpected)}`,
    );
  }
  for (const [key, value] of Object.entries(content)) {
    const message = value?.message;
    if (typeof message !== 'string' || message.trim() === '') {
      throw new Error(`Arabic UI translation has an empty message: ${relativePath} :: ${key}`);
    }
    if (
      !intentionalNonArabicMessages.has(message) &&
      !/[\u0600-\u06ff]/u.test(message)
    ) {
      throw new Error(
        `Arabic UI translation still has a non-Arabic message: ${relativePath} :: ${key}`,
      );
    }
  }
}

console.log(
  'Arabic documentation coverage, structure/link parity, technical-token occurrence/order integrity, and exact UI key/message checks passed',
);

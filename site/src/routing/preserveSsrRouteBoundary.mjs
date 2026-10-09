// Docusaurus's React Router matching is case-insensitive. On an HTTP 404, that
// can otherwise hydrate the static NotFound artifact into a similarly named
// document and remove its noindex boundary. Hydrate against the SSR canonical
// path first; Root restores the requested URL after the initial commit without
// notifying the router.
if (typeof window !== 'undefined') {
  const canonicalHref = document
    .querySelector('link[rel="canonical"]')
    ?.getAttribute('href');

  if (canonicalHref) {
    const canonicalPath = new URL(canonicalHref, window.location.href).pathname;
    if (canonicalPath !== window.location.pathname) {
      window.__factlaneSsrPathRestore =
        window.location.pathname + window.location.search + window.location.hash;
      window.history.replaceState(
        window.history.state,
        '',
        canonicalPath + window.location.search + window.location.hash,
      );
    }
  }
}

const COPY = {
  en: {
    dir: "ltr", skip: "Skip to content", ownerSurface: "Owner", navigation: "Navigation", globalSearch: "Search memory",
    localeToggle: "العربية", connect: "Connect", endSession: "End session",
    nav: { overview: "Overview", browse: "Memories", review: "Reviews", conflicts: "Conflicts", database: "Database", agents: "Agents", activity: "Activity", settings: "Settings" },
  },
  ar: {
    dir: "rtl", skip: "انتقل إلى المحتوى", ownerSurface: "المالك", navigation: "التنقل", globalSearch: "ابحث في الذاكرة",
    localeToggle: "EN", connect: "اتصال", endSession: "إنهاء الجلسة",
    nav: { overview: "نظرة عامة", browse: "الذكريات", review: "المراجعات", conflicts: "التعارضات", database: "قاعدة البيانات", agents: "الوكلاء", activity: "النشاط", settings: "الإعدادات" },
  },
};

const drawerMedia = window.matchMedia("(max-width: 1199px)");

function region(selector) {
  return document.querySelector(selector);
}

function appendBannerContent(target, title, message) {
  target.replaceChildren();
  if (title) {
    const strong = document.createElement("strong");
    strong.textContent = title;
    target.append(strong);
  }
  if (message) {
    const paragraph = document.createElement("p");
    paragraph.textContent = message;
    target.append(paragraph);
  }
}

export function currentLocale() {
  return document.documentElement.lang === "ar" ? "ar" : "en";
}

export function setLocale(locale) {
  if (!Object.hasOwn(COPY, locale)) {
    throw new RangeError(`Unsupported locale: ${locale}`);
  }
  const copy = COPY[locale];
  document.documentElement.lang = locale;
  document.documentElement.dir = copy.dir;

  for (const node of document.querySelectorAll("[data-i18n]")) {
    const key = node.dataset.i18n;
    if (copy[key]) node.textContent = copy[key];
  }
  for (const link of document.querySelectorAll("[data-nav-key]")) {
    const label = copy.nav[link.dataset.navKey];
    if (label) link.textContent = label;
  }
  const toggle = region("[data-locale-toggle]");
  if (toggle) toggle.textContent = copy.localeToggle;
}

export function setPage(state, title) {
  const titleNode = region("[data-page-title]");
  if (titleNode) titleNode.textContent = title;
  for (const link of document.querySelectorAll("[data-nav-key]")) {
    if (link.dataset.navKey === state) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

export function setHeaderState({ database = "", connection = "", refreshAge = "", authenticated = false } = {}) {
  const databaseNode = region("[data-database-label]");
  const connectionNode = region("[data-connection-state]");
  const refreshNode = region("[data-refresh-age]");
  const sessionButton = region("[data-session-control]");
  if (databaseNode) databaseNode.textContent = database || "Database unavailable";
  if (connectionNode) connectionNode.textContent = connection || "Disconnected";
  if (refreshNode) refreshNode.textContent = refreshAge || "Not refreshed";
  if (sessionButton) sessionButton.textContent = authenticated ? COPY[currentLocale()].endSession : COPY[currentLocale()].connect;
}

export function setNavigationDisabled(disabled, reason = "") {
  const nav = region("[data-navigation]");
  if (!nav) return;
  nav.inert = Boolean(disabled);
  if (disabled) {
    nav.setAttribute("aria-disabled", "true");
    if (reason) nav.setAttribute("aria-description", reason);
  } else {
    nav.removeAttribute("aria-disabled");
    nav.removeAttribute("aria-description");
  }
}

function syncDrawerAccessibility() {
  const drawer = region("[data-drawer]");
  if (!drawer) return;
  const closed = drawerMedia.matches && document.body.dataset.drawerOpen !== "true";
  drawer.inert = closed;
  if (closed) drawer.setAttribute("aria-hidden", "true");
  else drawer.removeAttribute("aria-hidden");
}

export function setDrawerOpen(open) {
  const expanded = Boolean(open);
  document.body.dataset.drawerOpen = String(expanded);
  const toggle = region("[data-drawer-toggle]");
  if (toggle) toggle.setAttribute("aria-expanded", String(expanded));
  const scrim = region("[data-drawer-scrim]");
  if (scrim) scrim.hidden = !expanded;
  syncDrawerAccessibility();
}

export function renderStatusBanner({ tone = "info", title = "", message = "", actionLabel = "", onAction } = {}) {
  const target = region("[data-status-banner]");
  if (!target) return;
  if (!title && !message) {
    target.hidden = true;
    target.replaceChildren();
    return;
  }
  target.dataset.tone = tone;
  appendBannerContent(target, title, message);
  if (actionLabel && typeof onAction === "function") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "banner-action";
    button.textContent = actionLabel;
    button.addEventListener("click", onAction);
    target.append(button);
  }
  target.hidden = false;
}

export function renderStaleBanner({ stale = false, title = "", message = "" } = {}) {
  const target = region("[data-stale-banner]");
  if (!target) return;
  if (!stale) {
    target.hidden = true;
    target.replaceChildren();
    return;
  }
  target.dataset.tone = "warning";
  appendBannerContent(target, title, message);
  target.hidden = false;
}

export function wireShell({ onLocaleChange, onGlobalSearch, onSessionControl } = {}) {
  const drawerToggle = region("[data-drawer-toggle]");
  const drawerScrim = region("[data-drawer-scrim]");
  const localeToggle = region("[data-locale-toggle]");
  const globalSearch = region("[data-global-search]");
  const sessionControl = region("[data-session-control]");

  drawerToggle?.addEventListener("click", () => {
    const opening = document.body.dataset.drawerOpen !== "true";
    setDrawerOpen(opening);
    if (opening) region("[data-navigation] a")?.focus();
  });
  drawerScrim?.addEventListener("click", () => setDrawerOpen(false));
  document.querySelector("[data-navigation]")?.addEventListener("click", (event) => {
    if (event.target.closest("a")) setDrawerOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.dataset.drawerOpen === "true") {
      setDrawerOpen(false);
      drawerToggle?.focus();
    }
  });
  localeToggle?.addEventListener("click", () => {
    const locale = currentLocale() === "en" ? "ar" : "en";
    setLocale(locale);
    onLocaleChange?.(locale);
  });
  globalSearch?.addEventListener("submit", (event) => {
    event.preventDefault();
    const input = region("[data-global-search-input]");
    onGlobalSearch?.(input?.value || "");
  });
  sessionControl?.addEventListener("click", () => onSessionControl?.());
  drawerMedia.addEventListener("change", syncDrawerAccessibility);
  syncDrawerAccessibility();
}

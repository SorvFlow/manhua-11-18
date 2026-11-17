/* UI-only localization. Keep model output, manuscript text and raw logs untouched. */
(() => {
  "use strict";

  const pack = window.InkstoneLocales["zh-CN"];
  const storageKey = "inkstone-language";
  let locale = "zh-CN";
  try {
    if (localStorage.getItem(storageKey) === "en") locale = "en";
  } catch (_) {}

  const textSources = new WeakMap();
  const attributeSources = new WeakMap();
  const attributes = ["placeholder", "title", "aria-label", "alt"];
  const excluded = "script, style, code, pre, textarea, [data-i18n-skip], .file-name";

  function translate(source) {
    if (locale === "en" || !source) return source;
    const key = source.trim().replace(/\s+/g, " ");
    let value = Object.hasOwn(pack.messages, key) ? pack.messages[key] : undefined;
    if (value === undefined) {
      for (const [pattern, render] of pack.patterns) {
        const match = key.match(pattern);
        if (match) {
          value = render(...match);
          break;
        }
      }
    }
    if (value === undefined) return source;
    return source.slice(0, source.search(/\S/)) + value + source.match(/\s*$/)[0];
  }

  function translateText(node) {
    if (!node.parentElement || node.parentElement.closest(excluded)) return;
    let state = textSources.get(node);
    if (!state || node.nodeValue !== state.rendered) state = { source: node.nodeValue };
    state.rendered = translate(state.source);
    textSources.set(node, state);
    if (node.nodeValue !== state.rendered) node.nodeValue = state.rendered;
  }

  function translateAttributes(element) {
    if (element.closest("[data-i18n-skip]")) return;
    let states = attributeSources.get(element);
    if (!states) {
      states = new Map();
      attributeSources.set(element, states);
    }
    for (const name of attributes) {
      if (!element.hasAttribute(name)) continue;
      const current = element.getAttribute(name);
      let state = states.get(name);
      if (!state || current !== state.rendered) state = { source: current };
      state.rendered = translate(state.source);
      states.set(name, state);
      if (current !== state.rendered) element.setAttribute(name, state.rendered);
    }
  }

  function translateTree(root) {
    if (root.nodeType === Node.TEXT_NODE) {
      translateText(root);
      return;
    }
    if (root.nodeType !== Node.ELEMENT_NODE) return;
    if (root.matches("script, style, [data-i18n-skip]")) return;
    translateAttributes(root);
    root.querySelectorAll("[placeholder], [title], [aria-label], [alt]")
      .forEach(translateAttributes);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) translateText(walker.currentNode);
  }

  const toggle = document.createElement("button");
  toggle.id = "languageToggle";
  toggle.type = "button";
  toggle.className = "theme-toggle";
  toggle.setAttribute("data-i18n-skip", "");
  toggle.style.cssText = "width:auto;min-width:56px;padding:0 10px;font:inherit;font-size:12px;white-space:nowrap";
  document.querySelector(".nav-extra").prepend(toggle);

  const originalTitle = document.title;
  function refresh() {
    document.documentElement.lang = locale;
    document.title = translate(originalTitle);
    toggle.textContent = locale === "zh-CN" ? "English" : "中文";
    toggle.setAttribute("aria-label", locale === "zh-CN" ? "Switch to English" : "切换为中文");
    toggle.title = toggle.getAttribute("aria-label");
    translateTree(document.body);
    // Reposition the existing navigation indicator after translated labels resize.
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  }

  function setLocale(next) {
    locale = next === "en" ? "en" : "zh-CN";
    try { localStorage.setItem(storageKey, locale); } catch (_) {}
    refresh();
  }

  toggle.addEventListener("click", () => setLocale(locale === "zh-CN" ? "en" : "zh-CN"));
  window.InkstoneI18n = { translate, setLocale, get locale() { return locale; } };
  refresh();

  // Watch only UI text/attributes. Translation writes are idempotent, and the
  // original values remain available for switching back without reloading.
  new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === "characterData") translateText(record.target);
      else if (record.type === "attributes") translateAttributes(record.target);
      else record.addedNodes.forEach(translateTree);
    }
  }).observe(document.body, {
    subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: attributes,
  });
})();

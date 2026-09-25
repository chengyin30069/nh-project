(function () {
  "use strict";

  const SCRIPT_PATH = new URL(document.currentScript?.src || "/_nh-local/assets/local.js", window.location.origin).pathname;
  const BASE_PATH = SCRIPT_PATH.replace(/\/_nh-local\/assets\/local\.js$/, "");
  const API = `${BASE_PATH}/_nh-local/api`;
  const GALLERY_PATH_RE = /^(?:\/downloads)?\/g\/([0-9]+)\/?$/;
  const TAXONOMY_PATH_RE = /^\/(tag|artist|character|parody|group|language|category)\/([^/]+)\/?$/;
  const DIRECTORY_PATH_RE = /^\/(tags|artists|characters|parodies|groups|languages|categories)\/?$/;
  const TAXONOMY_MODE_KEY = "nh-taxonomy-link-mode";
  const URL_CHECK_INTERVAL_MS = 250;
  const RENDER_RETRY_DELAYS_MS = [0, 300, 900, 1800];

  let currentUrl = "";
  let renderVersion = 0;
  let renderTimers = [];
  const badgeObservers = new Map();

  function parseGalleryIdFromUrl(value) {
    try {
      const path = new URL(value, window.location.origin).pathname;
      const routePath = BASE_PATH && path.startsWith(`${BASE_PATH}/`) ? path.slice(BASE_PATH.length) : path;
      return routePath.match(GALLERY_PATH_RE)?.[1] || null;
    } catch {
      return null;
    }
  }

  async function request(path, options) {
    const response = await fetch(`${API}${path}`, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body.error || `HTTP ${response.status}`);
    }
    return body;
  }

  function cleanText(value) {
    return (value || "").replace(/\s+/g, " ").trim();
  }

  function setButtonState(button, state, text, title = text) {
    button.dataset.state = state;
    button.textContent = text;
    button.title = title;
  }

  async function getStatuses(ids) {
    const uniqueIds = [...new Set(ids)];
    const body = await request("/galleries/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: uniqueIds }),
    });
    return body.galleries || {};
  }

  function getCurrentGalleryTitle(galleryId) {
    if (parseGalleryIdFromUrl(window.location.href) !== galleryId) {
      return "";
    }
    return cleanText(
      document.querySelector("#info h1")?.textContent ||
        document.querySelector(".title .pretty")?.textContent ||
        document.querySelector("h1")?.textContent ||
        document.title,
    );
  }

  function getCardTitle(card, overlayTarget) {
    return cleanText(
      card?.querySelector(".caption")?.textContent ||
        overlayTarget?.querySelector("img")?.getAttribute("alt") ||
        overlayTarget?.getAttribute("title") ||
        card?.textContent,
    );
  }

  function createDownloadButton(galleryId, className, label) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    button.title = `Download "${galleryId}"`;
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      queueDownload(galleryId, button);
    });
    return button;
  }

  function createDeleteButton(galleryId, title) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "nh-delete-button";
    button.textContent = "Delete";
    button.title = `Delete "${galleryId}"`;
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      showDeleteModal(galleryId, title);
    });
    return button;
  }

  function createDownloadedControls(galleryId, title) {
    const controls = document.createElement("div");
    controls.className = "nh-downloaded-controls";
    controls.appendChild(createDeleteButton(galleryId, title));
    return controls;
  }

  function hideDeleteModal() {
    document.getElementById("nh-delete-modal")?.remove();
  }

  function showDeleteModal(galleryId, title) {
    hideDeleteModal();
    const modal = document.createElement("div");
    modal.id = "nh-delete-modal";
    modal.className = "nh-delete-modal";
    modal.innerHTML = `
      <div class="nh-delete-dialog" role="dialog" aria-modal="true">
        <h2>Delete downloaded gallery?</h2>
        <p class="nh-delete-target"></p>
        <p class="nh-delete-error" hidden></p>
        <div class="nh-delete-actions">
          <button type="button" class="nh-delete-cancel">Cancel</button>
          <button type="button" class="nh-delete-confirm">Delete</button>
        </div>
      </div>`;
    modal.querySelector(".nh-delete-target").textContent = `ID ${galleryId} - ${cleanText(title) || `ID ${galleryId}`}`;
    modal.querySelector(".nh-delete-cancel").addEventListener("click", hideDeleteModal);
    modal.addEventListener("click", (event) => {
      if (event.target === modal) hideDeleteModal();
    });
    modal.querySelector(".nh-delete-confirm").addEventListener("click", async () => {
      const buttons = modal.querySelectorAll("button");
      buttons.forEach((item) => { item.disabled = true; });
      try {
        await request(`/galleries/${galleryId}`, { method: "DELETE" });
        hideDeleteModal();
        if (parseGalleryIdFromUrl(window.location.href)) {
          window.location.assign(`${BASE_PATH}/g/${galleryId}/`);
          return;
        }
        if (window.location.pathname.startsWith(`${BASE_PATH}/downloads/`)) {
          window.location.reload();
          return;
        }
        scheduleRenderForCurrentUrl();
      } catch (error) {
        const message = modal.querySelector(".nh-delete-error");
        message.textContent = error.message || "Delete failed";
        message.hidden = false;
        buttons.forEach((item) => { item.disabled = false; });
      }
    });
    document.body.appendChild(modal);
  }

  async function watchJob(job, button) {
    let current = job;
    while (current.status === "queued" || current.status === "running") {
      setButtonState(button, current.status, current.status === "running" ? "Running" : "Queued");
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      current = await request(`/jobs/${current.job_id}`);
    }
    if (current.status !== "succeeded") {
      throw new Error(current.error || "Download failed");
    }
  }

  async function queueDownload(galleryId, button) {
    button.disabled = true;
    setButtonState(button, "queued", "Queued");
    try {
      const job = await request("/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: galleryId }),
      });
      await watchJob(job, button);
      scheduleRenderForCurrentUrl();
    } catch (error) {
      button.disabled = false;
      setButtonState(button, "error", "Retry", error.message || "Request failed");
    }
  }

  function findNativeDownloadTarget() {
    const exactText = [...document.querySelectorAll("a, button")].find(
      (element) => element.textContent.trim().toLowerCase() === "download",
    );
    return exactText || document.querySelector('a[href*="/download"], #download, .buttons .btn');
  }

  async function addGalleryPageButton(version) {
    const galleryId = window.location.pathname.slice(BASE_PATH.length).match(GALLERY_PATH_RE)?.[1];
    if (!galleryId || document.getElementById("nh-downloader-button")) return;

    const button = createDownloadButton(galleryId, "nh-inline-download-button", "Download");
    button.id = "nh-downloader-button";
    const target = findNativeDownloadTarget();
    if (target?.parentElement) {
      for (const className of target.classList) button.classList.add(className);
      target.insertAdjacentElement("afterend", button);
    } else {
      button.classList.add("nh-page-download-button");
      document.body.appendChild(button);
    }

    try {
      const status = (await getStatuses([galleryId]))[galleryId] || {};
      if (version !== renderVersion) return;
      if (status.downloaded) {
        const del = createDeleteButton(galleryId, getCurrentGalleryTitle(galleryId));
        del.id = "nh-downloader-button";
        del.classList.add(button.classList.contains("nh-page-download-button") ? "nh-page-delete-button" : "nh-inline-delete-button");
        button.replaceWith(del);
      } else if ((status.status === "queued" || status.status === "running") && status.job_id) {
        button.disabled = true;
        watchJob(status, button).then(scheduleRenderForCurrentUrl).catch((error) => {
          button.disabled = false;
          setButtonState(button, "error", "Retry", error.message);
        });
      }
    } catch (error) {
      setButtonState(button, "error", "Retry", error.message || "Status unavailable");
    }
  }

  function findGalleryCards() {
    const cards = new Map();
    for (const link of document.querySelectorAll('a[href*="/g/"]')) {
      const galleryId = parseGalleryIdFromUrl(link.href);
      if (!galleryId) continue;
      if (GALLERY_PATH_RE.test(window.location.pathname.slice(BASE_PATH.length)) && !link.closest(".gallery, .thumb-container")) continue;
      const card = link.closest(".gallery") || link.closest(".thumb-container") || link.parentElement;
      if (!card || card === document.body || cards.has(card)) continue;
      if (link.closest(".nh-local-gallery-page, #info") || link.classList.contains("nh-content-thumbnail")) continue;
      const overlayTarget = link.querySelector("img") ? link : card;
      cards.set(card, { galleryId, card, overlayTarget });
    }
    return cards;
  }

  function prepareOverlayTarget(galleryId, card, overlayTarget) {
    card.classList.add("nh-downloader-card");
    overlayTarget.classList.add("nh-downloader-overlay-target");
    overlayTarget.dataset.nhGalleryId = galleryId;
  }

  async function renderThumbnailControls(version) {
    for (const [img, observer] of badgeObservers) {
      if (!img.isConnected) {
        observer.disconnect();
        badgeObservers.delete(img);
      }
    }
    const cards = findGalleryCards();
    if (cards.size === 0) return;
    for (const { galleryId, card, overlayTarget } of cards.values()) {
      prepareOverlayTarget(galleryId, card, overlayTarget);
      const titles = [card.dataset.nhTitles, getCardTitle(card, overlayTarget),
        overlayTarget.getAttribute("title"), overlayTarget.querySelector("img")?.getAttribute("alt")].join("\n");
      const marked = /decensored|uncensored|無碼|无码|無修正|モザイクなし/i.test(titles);
      let badge = overlayTarget.querySelector(".nh-decensored-marker");
      if (marked && !badge) {
        badge = document.createElement("span");
        badge.className = "nh-decensored-marker";
        badge.textContent = "Decensored";
        overlayTarget.appendChild(badge);
      } else if (!marked) {
        badge?.remove();
      }
      // Captions can share the cover anchor; anchor the badge to the image edge.
      const img = overlayTarget.querySelector("img");
      const positionBadge = () => {
        if (badge && img) badge.style.top = `${img.offsetTop + img.offsetHeight - badge.offsetHeight - 6}px`;
      };
      positionBadge();
      if (img && !badgeObservers.has(img)) {
        const observer = new ResizeObserver(() => {
          const marker = overlayTarget.querySelector(".nh-decensored-marker");
          if (marker) marker.style.top = `${img.offsetTop + img.offsetHeight - marker.offsetHeight - 6}px`;
        });
        observer.observe(img);
        badgeObservers.set(img, observer);
      }
    }
    let statuses = {};
    try {
      statuses = await getStatuses([...cards.values()].map(({ galleryId }) => galleryId));
    } catch {
      // Keep download controls usable even if the initial status check fails.
    }
    if (version !== renderVersion) return;

    for (const { galleryId, card, overlayTarget } of cards.values()) {
      prepareOverlayTarget(galleryId, card, overlayTarget);
      const status = statuses[galleryId] || {};
      if (status.downloaded) {
        overlayTarget.querySelector(".nh-thumb-download-button")?.remove();
        if (!overlayTarget.querySelector(".nh-downloaded-controls")) {
          overlayTarget.appendChild(createDownloadedControls(galleryId, getCardTitle(card, overlayTarget)));
        }
      } else if (!overlayTarget.querySelector(".nh-thumb-download-button, .nh-downloaded-controls")) {
        const button = createDownloadButton(galleryId, "nh-thumb-download-button", "DL");
        overlayTarget.appendChild(button);
        if ((status.status === "queued" || status.status === "running") && status.job_id) {
          button.disabled = true;
          watchJob(status, button).then(scheduleRenderForCurrentUrl).catch((error) => {
            button.disabled = false;
            setButtonState(button, "error", "Retry", error.message);
          });
        }
      }
    }
  }

  function cleanupLocalUi() {
    for (const observer of badgeObservers.values()) observer.disconnect();
    badgeObservers.clear();
    document.querySelectorAll("#nh-downloader-button, .nh-thumb-download-button, .nh-downloaded-controls, .nh-delete-button, .nh-decensored-marker, .nh-scope-toggle").forEach((element) => element.remove());
    document.querySelectorAll(".nh-downloader-card").forEach((element) => element.classList.remove("nh-downloader-card"));
    document.querySelectorAll(".nh-downloader-overlay-target").forEach((element) => {
      element.classList.remove("nh-downloader-overlay-target");
      delete element.dataset.nhGalleryId;
    });
  }

  function removeUnsupportedUi() {
    document.querySelectorAll("iframe,.advertisement,.adsbyexoclick,.ad-container").forEach((element) => element.remove());
    const accountLinks = ["login", "register", "favorites", "user/"]
      .map((path) => `a[href^="${BASE_PATH}/${path}"]`)
      .join(",");
    document.querySelectorAll(`${accountLinks},form[action*="/comments"],button[aria-label*="favorite" i],button[aria-label*="vote" i],button[aria-label*="suggest" i]`).forEach((element) => element.remove());
  }

  function applyTaxonomyMode(mode) {
    const local = mode === "local";
    document.querySelectorAll("[data-upstream-href][data-local-href]").forEach((link) => {
      link.setAttribute("href", local ? link.dataset.localHref : link.dataset.upstreamHref);
      const count = link.querySelector(".count, .nh-taxonomy-count");
      if (count) {
        const value = Number.parseInt(local ? count.dataset.localCount : count.dataset.upstreamCount, 10);
        count.textContent = Number.isFinite(value) && value >= 0 ? formatTaxonomyCount(value) : "…";
        count.title = Number.isFinite(value) && value >= 0
          ? `${value.toLocaleString()} local ${value === 1 ? "gallery" : "galleries"}`
          : "Count unavailable";
        if (!local && Number.isFinite(value) && value >= 0) {
          count.title = `${value.toLocaleString()} ${value === 1 ? "gallery" : "galleries"} on nhentai`;
        }
      }
    });
    document.querySelectorAll("[data-nh-taxonomy-toggle]").forEach((button) => {
      button.textContent = local ? "Local" : "nhentai";
      button.dataset.mode = local ? "local" : "upstream";
      button.title = local ? "Taxonomy links search downloaded galleries" : "Taxonomy links follow nhentai";
    });
  }

  function formatTaxonomyCount(value) {
    if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}m`;
    if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
    return String(value);
  }

  function exactCountFromElement(element) {
    const title = element?.getAttribute("title") || "";
    const match = title.match(/[0-9][0-9,]*/);
    return match ? Number.parseInt(match[0].replaceAll(",", ""), 10) : -1;
  }

  async function loadLocalTaxonomyCounts(items, storageKey, defaultMode) {
    if (!items.length) return;
    try {
      const body = await request("/taxonomies/counts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ taxonomies: items.map(({ type, slug }) => ({ type, slug })) }),
      });
      for (const item of items) {
        const value = body.counts?.[`${item.type}/${item.slug}`];
        if (Number.isInteger(value)) item.count.dataset.localCount = String(value);
      }
      const storedMode = window.sessionStorage.getItem(storageKey);
      applyTaxonomyMode(storedMode === "local" || storedMode === "upstream" ? storedMode : defaultMode);
    } catch {
      // Upstream counts already rendered by nhentai remain usable if the local database is busy.
    }
  }

  function setupTaxonomyToggle() {
    const routePath = window.location.pathname.slice(BASE_PATH.length);
    if (!routePath.match(GALLERY_PATH_RE)) return;
    const localGallery = document.body.dataset.nhDownloadedGallery === "true";
    const storageKey = `${TAXONOMY_MODE_KEY}:${localGallery ? "downloads" : "proxy"}`;
    const defaultMode = localGallery ? "local" : "upstream";
    const missingLocalCounts = [];
    for (const link of document.querySelectorAll("a[href]")) {
      let url;
      try { url = new URL(link.getAttribute("href"), window.location.origin); } catch { continue; }
      if (url.origin !== window.location.origin) continue;
      const taxonomyPath = BASE_PATH && url.pathname.startsWith(`${BASE_PATH}/`) ? url.pathname.slice(BASE_PATH.length) : url.pathname;
      const match = taxonomyPath.match(TAXONOMY_PATH_RE);
      if (!match) continue;
      link.dataset.upstreamHref = `${BASE_PATH}${taxonomyPath}${url.search}${url.hash}`;
      link.dataset.localHref = `${BASE_PATH}/downloads/${match[1]}/${match[2]}/`;
      link.classList.add("nh-taxonomy-link");
      const count = link.querySelector(".count, .nh-taxonomy-count");
      if (count) {
        if (count.dataset.upstreamCount === undefined) {
          count.dataset.upstreamCount = String(exactCountFromElement(count));
        }
        if (count.dataset.localCount === undefined && !count.dataset.nhLocalCountRequested) {
          count.dataset.nhLocalCountRequested = "true";
          missingLocalCounts.push({ type: match[1], slug: match[2], count });
        }
      }
    }
    let button = document.querySelector("[data-nh-taxonomy-toggle]");
    if (!button && document.querySelector("[data-upstream-href][data-local-href]")) {
      const wrapper = document.createElement("div");
      wrapper.className = "nh-taxonomy-mode";
      wrapper.innerHTML = '<span>Tag links:</span><button type="button" data-nh-taxonomy-toggle></button>';
      button = wrapper.querySelector("button");
      const target = document.querySelector("#tags") || document.querySelector("#info") || document.body;
      target.insertAdjacentElement("afterbegin", wrapper);
    }
    if (!button) return;
    if (!button.dataset.bound) {
      button.dataset.bound = "true";
      button.addEventListener("click", () => {
        const next = button.dataset.mode === "local" ? "upstream" : "local";
        window.sessionStorage.setItem(storageKey, next);
        applyTaxonomyMode(next);
      });
    }
    const storedMode = window.sessionStorage.getItem(storageKey);
    applyTaxonomyMode(storedMode === "local" || storedMode === "upstream" ? storedMode : defaultMode);
    loadLocalTaxonomyCounts(missingLocalCounts, storageKey, defaultMode);
  }

  function renderCurrentPage(version) {
    removeUnsupportedUi();
    setupCardPresentation();
    setupScopeToggle();
    setupTaxonomyToggle();
    addGalleryPageButton(version);
    renderThumbnailControls(version);
  }

  function setupScopeToggle() {
    const route = window.location.pathname.slice(BASE_PATH.length);
    const local = route.startsWith("/downloads/");
    const upstreamPath = local ? route.slice("/downloads".length) : route;
    if (!TAXONOMY_PATH_RE.test(upstreamPath) && !DIRECTORY_PATH_RE.test(upstreamPath) && !/^\/search\/?$/.test(upstreamPath)) return;
    let control = document.querySelector(".nh-scope-toggle");
    if (!control) {
      control = document.createElement("nav");
      control.className = "nh-scope-toggle";
      control.setAttribute("aria-label", "Browse scope");
      const target = document.querySelector(".nh-catalog-panel, #content, main") || document.body;
      target.prepend(control);
    }
    const destination = new URL(`${BASE_PATH}${local ? "" : "/downloads"}${upstreamPath.replace(/\/?$/, "/")}`, window.location.origin);
    if (/^\/search\/?$/.test(upstreamPath)) {
      destination.searchParams.set("q", new URLSearchParams(window.location.search).get("q") || "");
    }
    control.replaceChildren();
    control.append(document.createTextNode(local ? "Scope: Downloaded " : "Scope: All "));
    const link = document.createElement("a");
    link.href = destination.pathname + destination.search;
    link.textContent = local ? "Show all" : "Show downloaded";
    control.append(link);
  }

  function clearRenderTimers() {
    for (const timer of renderTimers) window.clearTimeout(timer);
    renderTimers = [];
  }

  function scheduleRenderForCurrentUrl() {
    clearRenderTimers();
    const version = ++renderVersion;
    cleanupLocalUi();
    renderTimers = RENDER_RETRY_DELAYS_MS.map((delay) => window.setTimeout(() => {
      if (version === renderVersion) renderCurrentPage(version);
    }, delay));
  }

  function localControlsAreMissing() {
    const scopePath = window.location.pathname.slice(BASE_PATH.length).replace(/^\/downloads\//, "/");
    if ((TAXONOMY_PATH_RE.test(scopePath) || DIRECTORY_PATH_RE.test(scopePath) || /^\/search\/?$/.test(scopePath)) && !document.querySelector(".nh-scope-toggle")) return true;
    if (window.location.pathname.slice(BASE_PATH.length).match(GALLERY_PATH_RE)) {
      return !document.getElementById("nh-downloader-button");
    }
    const cards = findGalleryCards();
    if (cards.size === 0) return false;
    return [...cards.values()].some(({ overlayTarget }) =>
      !overlayTarget.querySelector(".nh-thumb-download-button, .nh-downloaded-controls")
    );
  }

  function detectUrlChange() {
    if (currentUrl === window.location.href) {
      if (localControlsAreMissing()) renderCurrentPage(renderVersion);
      return;
    }
    currentUrl = window.location.href;
    scheduleRenderForCurrentUrl();
  }

  function setupCardPresentation() {
    for (const card of document.querySelectorAll(".gallery")) {
      const caption = card.querySelector(".caption");
      const cover = card.querySelector("a.cover");
      if (!caption || !cover) continue;
      if (!card.classList.contains("nh-full-title-card")) card.classList.add("nh-full-title-card");
      if (!card.parentElement?.classList.contains("nh-gallery-grid")) card.parentElement?.classList.add("nh-gallery-grid");
      // Work on text nodes only: preserve upstream markup and event handlers.
      const walker = document.createTreeWalker(caption, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const node = walker.currentNode;
        if (node.nodeValue.includes("🇨🇳")) node.nodeValue = node.nodeValue.replaceAll("🇨🇳", "🇹🇼");
      }
      const flags = [["lang-cn", "🇹🇼"], ["lang-gb", "🇬🇧"], ["lang-jp", "🇯🇵"]]
        .filter(([name]) => card.classList.contains(name)).map(([, flag]) => flag).join(" ");
      const prefix = /🇹🇼|🇬🇧|🇯🇵/.test(caption.textContent) ? "" : flags;
      if (caption.dataset.nhFlags !== prefix) caption.dataset.nhFlags = prefix;
    }
  }

  function setupBranding() {
    const logoPath = `${BASE_PATH}/logo.png`;
    for (const logo of document.querySelectorAll('a.logo img, .nh-catalog-logo img, img[src$="/logo.svg"]')) {
      if (logo.getAttribute("src") !== logoPath) logo.setAttribute("src", logoPath);
      logo.setAttribute("alt", "Local gallery");
    }
    for (const link of document.querySelectorAll('link[rel~="icon"], link[rel="apple-touch-icon"]')) {
      if (link.getAttribute("href") !== logoPath) link.setAttribute("href", logoPath);
    }
  }

  function setupReader() {
    const select = document.getElementById("nh-reader-mode");
    const image = document.getElementById("nh-reader-image");
    const stage = document.querySelector(".nh-reader-stage");
    const key = `nh-reader-mode:${BASE_PATH || "/"}`;
    const valid = (value) => value === "original" ? "original" : "fit";
    const fitImage = () => {
      if (!image || !image.naturalWidth || !stage) return;
      const scale = document.body.dataset.readerMode === "original" ? 1 : Math.min(
        1, Math.max(1, stage.clientWidth - 32) / image.naturalWidth,
        Math.max(1, stage.clientHeight - 32) / image.naturalHeight,
      );
      image.style.width = `${image.naturalWidth * scale}px`;
      image.style.height = `${image.naturalHeight * scale}px`;
    };
    const apply = (value) => {
      const mode = valid(value);
      document.body.dataset.readerMode = mode;
      select.value = mode;
      fitImage();
    };
    let stored;
    try { stored = window.localStorage.getItem(key); } catch { /* Storage can be unavailable. */ }
    apply(stored);
    select.addEventListener("change", () => {
      apply(select.value);
      try { window.localStorage.setItem(key, select.value); } catch { /* Keep the current page usable. */ }
    });
    window.addEventListener("storage", (event) => {
      if (event.key === key || event.key === null) apply(event.newValue);
    });
    image?.addEventListener("load", fitImage);
    image?.addEventListener("click", (event) => {
      if (event.button !== 0 || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      const bounds = image.getBoundingClientRect();
      const destination = event.clientX < bounds.left + bounds.width / 2 ? "nh-reader-prev" : "nh-reader-next";
      event.preventDefault();
      window.location.assign(document.getElementById(destination).href);
    });
    const jump = document.querySelector(".nh-reader-jump");
    jump.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!jump.reportValidity()) return;
      const input = jump.elements.namedItem("page");
      const number = input.valueAsNumber;
      if (input.disabled || !Number.isInteger(number) || number < 1 || number > Number(input.max)) return;
      const destination = new URL(jump.action);
      destination.pathname = `${destination.pathname.replace(/\/$/, "")}/${number}/`;
      window.location.assign(destination.href);
    });
    image?.addEventListener("error", () => {
      const message = document.createElement("p");
      message.className = "nh-reader-error";
      message.textContent = "Page image unavailable. Reload to retry.";
      image.replaceWith(message);
    }, { once: true });
    new ResizeObserver(fitImage).observe(stage);
    document.addEventListener("keydown", (event) => {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey ||
          event.target.closest("input, select, textarea, button, [contenteditable]:not([contenteditable='false'])")) return;
      const destination = event.key === "ArrowLeft" ? "nh-reader-prev" : event.key === "ArrowRight" ? "nh-reader-next" : null;
      if (destination) {
        event.preventDefault();
        window.location.assign(document.getElementById(destination).href);
      }
    });
  }

  function setupAssistant() {
    if (document.getElementById("nh-assistant")) return;
    const key = `nh-assistant:${BASE_PATH}`;
    let saved = {};
    try { saved = JSON.parse(sessionStorage.getItem(key) || "{}"); } catch { /* storage unavailable */ }
    const store = () => { try { sessionStorage.setItem(key, JSON.stringify(saved)); } catch { /* optional */ } };
    function node(tag, text, className) {
      const el = document.createElement(tag);
      if (text) el.textContent = text;
      if (className) el.className = className;
      return el;
    }
    const root = node("div", "", "nh-assistant-root");
    root.id = "nh-assistant";
    const toggle = node("button", "Assistant", "nh-assistant-toggle");
    toggle.type = "button";
    toggle.setAttribute("aria-controls", "nh-assistant-panel");
    const panel = node("aside", "", "nh-assistant-panel");
    panel.id = "nh-assistant-panel";
    panel.setAttribute("aria-label", "Library Assistant");
    panel.hidden = true;
    const header = node("header");
    const close = node("button", "Close");
    header.append(node("h2", "Library Assistant"), close);
    const status = node("p", "Open to check availability.", "nh-assistant-status");
    status.setAttribute("role", "status");
    const disclosure = node("p", "V1 uses metadata only. Queries and metadata are sent to NVIDIA when configured; page images are not uploaded.", "nh-assistant-note");
    const form = node("form");
    const label = node("label", "What would you like to read?");
    label.htmlFor = "nh-assistant-message";
    const input = node("textarea");
    input.id = "nh-assistant-message";
    input.required = true;
    input.maxLength = 4000;
    input.rows = 3;
    const submit = node("button", "Recommend");
    submit.type = "submit";
    const reset = node("button", "New search");
    reset.type = "button";
    const controls = node("div", "", "nh-assistant-controls");
    controls.append(submit, reset);
    form.append(label, input, controls);
    const admin = node("details");
    admin.append(node("summary", "Metadata index"));
    const indexButton = node("button", "Build / resume index");
    const retryButton = node("button", "Retry failed");
    admin.append(indexButton, retryButton);
    const output = node("div", "", "nh-assistant-results");
    output.setAttribute("aria-live", "polite");
    const diagnostics = node("div", "", "nh-assistant-diagnostics");
    const checkButton = node("button", "Check NIM connection");
    checkButton.type = "button";
    const checkStatus = node("p", "", "nh-assistant-note");
    panel.append(header, status, diagnostics, checkButton, checkStatus, disclosure, form, admin, output);
    root.append(toggle, panel);
    document.body.append(root);
    let timer = null;
    let busy = false;
    let checking = false;
    async function health() {
      if (panel.hidden) return;
      try {
        const data = await request("/assistant/health");
        const index = data.metadata_index || {};
        const available = data.available !== false && data.enabled;
        status.textContent = !data.enabled ? "Assistant is disabled (assistant.enabled is false or absent in the loaded configuration)." :
          !available ? "Assistant enabled, but initialization failed." :
          !data.api_key_configured ? "Assistant ready · API key missing · Local search only." :
          `NIM ${data.provider_state} · Index ${index.indexed || 0}/${index.total || 0}${data.scanning ? " · Scanning" : ""}${data.background_enabled === false ? " · Background paused" : ""}`;
        if (index.jobs) status.textContent += ` · Queued ${(index.jobs.queued || 0) + (index.jobs.retry_wait || 0)} · Failed ${index.jobs.failed || 0}`;
        diagnostics.replaceChildren(
          node("p", `Configuration: ${data.config_source || "server configuration"} · enabled: ${Boolean(data.enabled)}`),
          node("p", `${data.api_key_env || "NVIDIA_API_KEY"}: ${data.api_key_configured ? "loaded by server (value hidden)" : "NOT found in server environment"}`),
        );
        if (data.error) diagnostics.append(node("p", `${data.error.code}: ${data.error.message}`));
        const check = data.connection_check || {};
        if (!checking) checkStatus.textContent = check.message || "Key presence does not verify validity. Check NIM connection to test the embedding model.";
        if (!data.enabled || !available || !data.api_key_configured) {
          diagnostics.append(node("p", data.configuration_note || "Restart the server after changing configuration.", "nh-assistant-note"));
          if (!data.api_key_configured) diagnostics.append(node("p", "Docker: add NVIDIA_API_KEY to the project .env file, then run docker compose up -d --force-recreate nh-server.", "nh-assistant-note"));
        }
        submit.disabled = busy || !available;
        indexButton.disabled = retryButton.disabled = !available;
        checkButton.disabled = checking || !available;
      } catch (error) { status.textContent = error.message; }
    }
    checkButton.addEventListener("click", async () => {
      checking = true;
      checkButton.disabled = true;
      checkStatus.textContent = "Checking NIM connection…";
      try {
        const result = await request("/assistant/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        checkStatus.textContent = result.message;
      } catch (error) { checkStatus.textContent = error.message; }
      finally { checking = false; checkButton.disabled = false; }
    });
    function show(open) {
      panel.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      saved.open = open;
      store();
      clearInterval(timer);
      if (open) { health(); timer = setInterval(health, 5000); input.focus(); }
      else toggle.focus();
    }
    toggle.addEventListener("click", () => show(panel.hidden));
    close.addEventListener("click", () => show(false));
    panel.addEventListener("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); show(false); } });
    reset.addEventListener("click", () => { saved = { open: true }; store(); input.value = ""; output.replaceChildren(); input.focus(); });
    for (const [button, action] of [[indexButton, "metadata"], [retryButton, "retry-failed"]]) {
      button.addEventListener("click", async () => {
        button.disabled = true;
        try { await request(`/assistant/index/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); await health(); }
        catch (error) { status.textContent = error.message; }
        finally { button.disabled = false; }
      });
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busy || !input.value.trim()) return;
      busy = true;
      submit.disabled = true;
      output.replaceChildren(node("p", "Finding recommendations…"));
      try {
        const data = await request("/assistant/recommend", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: input.value, mode: "fast", limit: 5, previous_plan: saved.previous_plan || null }) });
        saved.previous_plan = data.plan;
        saved.last_result_ids = (data.results || []).map((r) => r.id);
        saved.request_id = data.request_id;
        store();
        output.replaceChildren(node("p", data.assistant_text));
        for (const warning of data.warnings || []) output.append(node("p", warning, "nh-assistant-note"));
        for (const term of data.unresolved_terms || []) output.append(node("p", `Unresolved ${term.kind}: ${term.value}`));
        for (const result of data.results || []) {
          if (!/^[1-9][0-9]*$/.test(result.id)) continue;
          const card = node("article", "", "nh-assistant-card");
          const cover = node("img");
          cover.src = `${BASE_PATH}/catalog-thumbnail/${result.id}`;
          cover.alt = "";
          cover.loading = "lazy";
          const details = node("div");
          details.append(node("h3", result.title), node("p", `${result.id} · ${result.pages || "Unknown"} pages`));
          for (const reason of (result.reasons || []).slice(0, 3)) details.append(node("p", reason));
          const link = node("a", "Open gallery");
          link.href = `${BASE_PATH}/g/${result.id}/`;
          const exclude = node("button", "Exclude from next search");
          exclude.addEventListener("click", () => {
            saved.previous_plan.excluded_gallery_ids = [...new Set([...(saved.previous_plan.excluded_gallery_ids || []), result.id])].slice(-100);
            store();
            exclude.disabled = true;
            exclude.textContent = "Excluded";
          });
          details.append(link, exclude);
          card.append(cover, details);
          output.append(card);
        }
      } catch (error) { output.replaceChildren(node("p", error.message)); }
      finally { busy = false; submit.disabled = false; }
    });
    toggle.setAttribute("aria-expanded", "false");
    if (saved.open && !document.body.classList.contains("nh-reader")) show(true);
  }

  setupAssistant();

  if (document.body.classList.contains("nh-reader")) {
    setupReader();
    return;
  }
  let presentationPending = false;
  new MutationObserver(() => {
    if (presentationPending) return;
    presentationPending = true;
    requestAnimationFrame(() => {
      presentationPending = false;
      setupBranding();
      setupCardPresentation();
    });
  }).observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: ["class", "src"],
  });
  document.querySelector(".nh-directory-filter select")?.addEventListener("change", (event) => event.target.form.requestSubmit());

  setupBranding();
  detectUrlChange();
  window.setInterval(detectUrlChange, URL_CHECK_INTERVAL_MS);
  window.addEventListener("pageshow", () => { currentUrl = ""; detectUrlChange(); });
  window.addEventListener("popstate", () => { currentUrl = ""; detectUrlChange(); });
}());

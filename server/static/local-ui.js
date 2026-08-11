(function () {
  "use strict";

  const API = "/_nh-local/api";
  const GALLERY_PATH_RE = /^(?:\/downloads)?\/g\/([0-9]+)\/?$/;
  const TAXONOMY_PATH_RE = /^\/(tag|artist|character|parody|group|language|category)\/([^/]+)\/?$/;
  const TAXONOMY_MODE_KEY = "nh-taxonomy-link-mode";
  const URL_CHECK_INTERVAL_MS = 250;
  const RENDER_RETRY_DELAYS_MS = [0, 300, 900, 1800];

  let currentUrl = "";
  let renderVersion = 0;
  let renderTimers = [];

  function parseGalleryIdFromUrl(value) {
    try {
      return new URL(value, window.location.origin).pathname.match(GALLERY_PATH_RE)?.[1] || null;
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
    const marker = document.createElement("div");
    marker.className = "nh-downloaded-marker";
    marker.textContent = "Downloaded";
    marker.title = `Already downloaded "${galleryId}"`;
    controls.append(marker, createDeleteButton(galleryId, title));
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
        if (window.location.pathname.startsWith("/downloads/g/")) {
          window.location.assign("/downloads/");
          return;
        }
        if (window.location.pathname.startsWith("/downloads/")) {
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
    const galleryId = window.location.pathname.match(GALLERY_PATH_RE)?.[1];
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
        button.disabled = true;
        setButtonState(button, "downloaded", "Downloaded");
        if (!button.parentElement?.querySelector(".nh-delete-button")) {
          button.insertAdjacentElement("afterend", createDeleteButton(galleryId, getCurrentGalleryTitle(galleryId)));
        }
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
      if (!galleryId || cards.has(galleryId)) continue;
      const card = link.closest(".gallery") || link.closest(".thumb-container") || link.parentElement;
      if (!card || card === document.body) continue;
      const overlayTarget = link.querySelector("img") ? link : card;
      cards.set(galleryId, { card, overlayTarget });
    }
    return cards;
  }

  function prepareOverlayTarget(galleryId, card, overlayTarget) {
    card.classList.add("nh-downloader-card");
    overlayTarget.classList.add("nh-downloader-overlay-target");
    overlayTarget.dataset.nhGalleryId = galleryId;
  }

  async function renderThumbnailControls(version) {
    const cards = findGalleryCards();
    if (cards.size === 0) return;
    let statuses = {};
    try {
      statuses = await getStatuses([...cards.keys()]);
    } catch {
      // Keep download controls usable even if the initial status check fails.
    }
    if (version !== renderVersion) return;

    for (const [galleryId, { card, overlayTarget }] of cards) {
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
    document.querySelectorAll("#nh-downloader-button, .nh-thumb-download-button, .nh-downloaded-controls, .nh-delete-button").forEach((element) => element.remove());
    document.querySelectorAll(".nh-downloader-card").forEach((element) => element.classList.remove("nh-downloader-card"));
    document.querySelectorAll(".nh-downloader-overlay-target").forEach((element) => {
      element.classList.remove("nh-downloader-overlay-target");
      delete element.dataset.nhGalleryId;
    });
  }

  function removeUnsupportedUi() {
    document.querySelectorAll("iframe,.advertisement,.adsbyexoclick,.ad-container").forEach((element) => element.remove());
    document.querySelectorAll('a[href^="/login"],a[href^="/register"],a[href^="/favorites"],a[href^="/user/"],form[action*="/comments"],button[aria-label*="favorite" i],button[aria-label*="vote" i],button[aria-label*="suggest" i]').forEach((element) => element.remove());
  }

  function applyTaxonomyMode(mode) {
    const local = mode === "local";
    document.querySelectorAll("[data-upstream-href][data-local-href]").forEach((link) => {
      link.setAttribute("href", local ? link.dataset.localHref : link.dataset.upstreamHref);
    });
    document.querySelectorAll("[data-nh-taxonomy-toggle]").forEach((button) => {
      button.textContent = local ? "Local" : "nhentai";
      button.dataset.mode = local ? "local" : "upstream";
      button.title = local ? "Taxonomy links search downloaded galleries" : "Taxonomy links follow nhentai";
    });
  }

  function setupTaxonomyToggle() {
    if (!window.location.pathname.match(GALLERY_PATH_RE)) return;
    const localGallery = window.location.pathname.startsWith("/downloads/g/");
    const storageKey = `${TAXONOMY_MODE_KEY}:${localGallery ? "downloads" : "proxy"}`;
    const defaultMode = localGallery ? "local" : "upstream";
    for (const link of document.querySelectorAll("a[href]")) {
      let url;
      try { url = new URL(link.getAttribute("href"), window.location.origin); } catch { continue; }
      if (url.origin !== window.location.origin) continue;
      const match = url.pathname.match(TAXONOMY_PATH_RE);
      if (!match) continue;
      link.dataset.upstreamHref = `${url.pathname}${url.search}${url.hash}`;
      link.dataset.localHref = `/downloads/${match[1]}/${match[2]}/`;
      link.classList.add("nh-taxonomy-link");
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
  }

  function renderCurrentPage(version) {
    removeUnsupportedUi();
    setupTaxonomyToggle();
    addGalleryPageButton(version);
    renderThumbnailControls(version);
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
    if (window.location.pathname.match(GALLERY_PATH_RE)) {
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

  detectUrlChange();
  window.setInterval(detectUrlChange, URL_CHECK_INTERVAL_MS);
  window.addEventListener("pageshow", () => { currentUrl = ""; detectUrlChange(); });
  window.addEventListener("popstate", () => { currentUrl = ""; detectUrlChange(); });
}());

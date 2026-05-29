const GALLERY_PATH_RE = /^\/g\/([0-9]+)\/?$/;
const URL_CHECK_INTERVAL_MS = 250;
const RENDER_RETRY_DELAYS_MS = [0, 300, 900, 1800];

let currentUrl = "";
let renderVersion = 0;
let renderTimers = [];

function parseGalleryIdFromUrl(value) {
  try {
    const url = new URL(value, window.location.origin);
    return url.pathname.match(GALLERY_PATH_RE)?.[1] || null;
  } catch {
    return null;
  }
}

function setButtonState(button, state, text, title = text) {
  button.dataset.state = state;
  button.textContent = text;
  button.title = title;
}

async function getStatuses(ids) {
  const uniqueIds = [...new Set(ids)];
  const result = await browser.runtime.sendMessage({ type: "statuses", ids: uniqueIds });
  return result.body.galleries || {};
}

async function queueDownload(galleryId, button) {
  button.disabled = true;
  setButtonState(button, "queued", `Queueing "${galleryId}"`);

  try {
    const result = await browser.runtime.sendMessage({ type: "download", id: galleryId });
    setButtonState(button, "done", `Queued "${galleryId}"`, `Queued on ${result.server}`);
  } catch (error) {
    button.disabled = false;
    setButtonState(button, "error", `Failed "${galleryId}"`, error.message || "Request failed");
    console.error("nh downloader request failed:", error);
  }
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

function createDownloadedMarker(galleryId) {
  const marker = document.createElement("div");
  marker.className = "nh-downloaded-marker";
  marker.textContent = "Downloaded";
  marker.title = `Already downloaded "${galleryId}"`;
  return marker;
}

function findNativeDownloadTarget() {
  const exactText = [...document.querySelectorAll("a, button")].find(
    (element) => element.textContent.trim().toLowerCase() === "download",
  );
  if (exactText) {
    return exactText;
  }

  return (
    document.querySelector('a[href*="/download"]') ||
    document.querySelector("#download") ||
    document.querySelector(".buttons .btn")
  );
}

async function addGalleryPageButton(version) {
  const galleryId = window.location.pathname.match(GALLERY_PATH_RE)?.[1];
  if (!galleryId || document.getElementById("nh-downloader-button")) {
    return;
  }

  const button = createDownloadButton(galleryId, "nh-inline-download-button", "Download v2");
  button.id = "nh-downloader-button";
  button.dataset.nhGalleryId = galleryId;

  const target = findNativeDownloadTarget();
  if (target?.parentElement) {
    for (const className of target.classList) {
      button.classList.add(className);
    }
    target.insertAdjacentElement("afterend", button);
  } else {
    button.classList.add("nh-page-download-button");
    document.body.appendChild(button);
  }

  try {
    const statuses = await getStatuses([galleryId]);
    if (version !== renderVersion) {
      return;
    }
    if (statuses[galleryId]?.downloaded) {
      button.disabled = true;
      setButtonState(button, "downloaded", "Downloaded", `Already downloaded "${galleryId}"`);
    }
  } catch (error) {
    console.error("nh downloader page status check failed:", error);
  }
}

function findGalleryCards() {
  const cards = new Map();
  const links = document.querySelectorAll('a[href*="/g/"]');

  for (const link of links) {
    const galleryId = parseGalleryIdFromUrl(link.href);
    if (!galleryId || cards.has(galleryId)) {
      continue;
    }

    const card = link.closest(".gallery") || link.closest(".thumb-container") || link.parentElement;
    if (!card || card === document.body) {
      continue;
    }

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
  const ids = [...cards.keys()];
  if (ids.length === 0) {
    return;
  }

  let statuses = {};
  try {
    statuses = await getStatuses(ids);
  } catch (error) {
    console.error("nh downloader status check failed:", error);
  }

  if (version !== renderVersion) {
    return;
  }

  for (const [galleryId, { card, overlayTarget }] of cards) {
    prepareOverlayTarget(galleryId, card, overlayTarget);

    if (statuses[galleryId]?.downloaded) {
      overlayTarget.querySelector(".nh-thumb-download-button")?.remove();
      if (!overlayTarget.querySelector(".nh-downloaded-marker")) {
        overlayTarget.appendChild(createDownloadedMarker(galleryId));
      }
      continue;
    }

    if (
      !overlayTarget.querySelector(".nh-thumb-download-button") &&
      !overlayTarget.querySelector(".nh-downloaded-marker")
    ) {
      overlayTarget.appendChild(createDownloadButton(galleryId, "nh-thumb-download-button", "DL"));
    }
  }
}

function cleanupPluginUi() {
  document.querySelectorAll("#nh-downloader-button").forEach((element) => element.remove());
  document.querySelectorAll(".nh-thumb-download-button, .nh-downloaded-marker").forEach((element) => element.remove());
  document.querySelectorAll(".nh-downloader-card").forEach((element) => {
    element.classList.remove("nh-downloader-card");
  });
  document.querySelectorAll(".nh-downloader-overlay-target").forEach((element) => {
    element.classList.remove("nh-downloader-overlay-target");
    delete element.dataset.nhGalleryId;
  });
}

function renderCurrentPage(version) {
  addGalleryPageButton(version);
  renderThumbnailControls(version);
}

function clearRenderTimers() {
  for (const timer of renderTimers) {
    window.clearTimeout(timer);
  }
  renderTimers = [];
}

function scheduleRenderForCurrentUrl() {
  clearRenderTimers();
  const version = ++renderVersion;
  cleanupPluginUi();

  renderTimers = RENDER_RETRY_DELAYS_MS.map((delay) =>
    window.setTimeout(() => {
      if (version === renderVersion) {
        renderCurrentPage(version);
      }
    }, delay),
  );
}

function detectUrlChange() {
  if (currentUrl === window.location.href) {
    return;
  }
  currentUrl = window.location.href;
  scheduleRenderForCurrentUrl();
}

detectUrlChange();
window.setInterval(detectUrlChange, URL_CHECK_INTERVAL_MS);
window.addEventListener("pageshow", () => {
  currentUrl = "";
  detectUrlChange();
});
window.addEventListener("popstate", () => {
  currentUrl = "";
  detectUrlChange();
});

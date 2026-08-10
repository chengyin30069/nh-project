const GALLERY_PATH_RE = /^\/g\/([0-9]+)\/?$/;
const URL_CHECK_INTERVAL_MS = 250;
const RENDER_RETRY_DELAYS_MS = [0, 300, 900, 1800];
const QUEUE_REFRESH_INTERVAL_MS = 1000;

let currentUrl = "";
let renderVersion = 0;
let renderTimers = [];
let queuePanel = null;

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

async function getQueue() {
  return browser.runtime.sendMessage({ type: "queue" });
}

async function deleteGallery(galleryId) {
  return browser.runtime.sendMessage({ type: "delete", id: galleryId });
}

async function queueDownload(galleryId, button) {
  button.disabled = true;
  setButtonState(button, "queued", `Queueing "${galleryId}"`);

  try {
    const result = await browser.runtime.sendMessage({ type: "download", id: galleryId });
    setButtonState(button, "done", `Queued "${galleryId}"`, `Queued on ${result.server}`);
    refreshQueuePanel();
  } catch (error) {
    button.disabled = false;
    setButtonState(button, "error", `Failed "${galleryId}"`, error.message || "Request failed");
    console.error("nh downloader request failed:", error);
  }
}

function cleanText(value) {
  return (value || "").replace(/\s+/g, " ").trim();
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

function ensureDeleteModal() {
  let modal = document.getElementById("nh-delete-modal");
  if (modal) {
    return modal;
  }

  modal = document.createElement("div");
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
    </div>
  `;
  modal.addEventListener("click", (event) => {
    if (event.target === modal) {
      hideDeleteModal();
    }
  });
  modal.querySelector(".nh-delete-cancel").addEventListener("click", hideDeleteModal);
  document.body.appendChild(modal);
  return modal;
}

function hideDeleteModal() {
  const modal = document.getElementById("nh-delete-modal");
  if (modal) {
    modal.remove();
  }
}

function showDeleteModal(galleryId, title) {
  const modal = ensureDeleteModal();
  const displayTitle = cleanText(title) || getCurrentGalleryTitle(galleryId) || `ID ${galleryId}`;
  const target = modal.querySelector(".nh-delete-target");
  const error = modal.querySelector(".nh-delete-error");
  const confirm = modal.querySelector(".nh-delete-confirm");
  const cancel = modal.querySelector(".nh-delete-cancel");

  target.textContent = `ID ${galleryId} - ${displayTitle}`;
  error.hidden = true;
  error.textContent = "";
  confirm.disabled = false;
  cancel.disabled = false;

  confirm.onclick = async () => {
    confirm.disabled = true;
    cancel.disabled = true;
    try {
      await deleteGallery(galleryId);
      hideDeleteModal();
      scheduleRenderForCurrentUrl();
      refreshQueuePanel();
    } catch (deleteError) {
      error.textContent = deleteError.message || "Delete failed";
      error.hidden = false;
      confirm.disabled = false;
      cancel.disabled = false;
      console.error("nh downloader delete failed:", deleteError);
    }
  };
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
      if (!button.parentElement?.querySelector(".nh-delete-button")) {
        button.insertAdjacentElement("afterend", createDeleteButton(galleryId, getCurrentGalleryTitle(galleryId)));
      }
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
      if (!overlayTarget.querySelector(".nh-downloaded-controls")) {
        overlayTarget.appendChild(createDownloadedControls(galleryId, getCardTitle(card, overlayTarget) || `ID ${galleryId}`));
      }
      continue;
    }

    if (
      !overlayTarget.querySelector(".nh-thumb-download-button") &&
      !overlayTarget.querySelector(".nh-downloaded-controls")
    ) {
      overlayTarget.appendChild(createDownloadButton(galleryId, "nh-thumb-download-button", "DL"));
    }
  }
}

function cleanupPluginUi() {
  document.querySelectorAll("#nh-downloader-button").forEach((element) => element.remove());
  document
    .querySelectorAll(".nh-thumb-download-button, .nh-downloaded-controls, .nh-delete-button")
    .forEach((element) => element.remove());
  document.querySelectorAll(".nh-downloader-card").forEach((element) => {
    element.classList.remove("nh-downloader-card");
  });
  document.querySelectorAll(".nh-downloader-overlay-target").forEach((element) => {
    element.classList.remove("nh-downloader-overlay-target");
    delete element.dataset.nhGalleryId;
  });
}

function formatQueueItem(job) {
  const label = job.id || job.gallery_id || "unknown";
  return `${label} · ${job.status || "queued"}`;
}

function renderQueuePanel(result) {
  if (!queuePanel) {
    return;
  }

  const body = result?.body || {};
  queuePanel.querySelector(".nh-queue-server").textContent = result?.server || "";

  const running = body.running || [];
  const queued = body.queued || [];
  const recent = (body.recent || []).slice(0, 5);
  const lines = [];

  if (running.length) {
    lines.push(`Running: ${running.map(formatQueueItem).join(", ")}`);
  }
  if (queued.length) {
    lines.push(`Queued: ${queued.map(formatQueueItem).join(", ")}`);
  }
  if (recent.length) {
    lines.push(`Recent: ${recent.map(formatQueueItem).join(", ")}`);
  }

  queuePanel.querySelector(".nh-queue-body").textContent = lines.join("\n") || "Queue is empty";
  queuePanel.dataset.state = "ok";
}

function renderQueueError(error) {
  if (!queuePanel) {
    return;
  }
  queuePanel.dataset.state = "error";
  queuePanel.querySelector(".nh-queue-server").textContent = "";
  queuePanel.querySelector(".nh-queue-body").textContent = error.message || "Queue unavailable";
}

async function refreshQueuePanel() {
  if (!queuePanel) {
    return;
  }
  try {
    renderQueuePanel(await getQueue());
  } catch (error) {
    renderQueueError(error);
  }
}

function ensureQueuePanel() {
  if (queuePanel) {
    return;
  }

  queuePanel = document.createElement("div");
  queuePanel.id = "nh-queue-panel";
  queuePanel.innerHTML = `
    <div class="nh-queue-header">
      <span>Download Queue</span>
      <span class="nh-queue-server"></span>
    </div>
    <pre class="nh-queue-body">Loading...</pre>
  `;
  document.body.appendChild(queuePanel);
  refreshQueuePanel();
  window.setInterval(refreshQueuePanel, QUEUE_REFRESH_INTERVAL_MS);
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

ensureQueuePanel();
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

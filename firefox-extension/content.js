const match = window.location.pathname.match(/^\/g\/([0-9]+)\/?$/);

if (match && !document.getElementById("nh-downloader-button")) {
  const galleryId = match[1];
  const button = document.createElement("button");
  button.id = "nh-downloader-button";
  button.type = "button";
  button.textContent = `Download "${galleryId}"`;
  button.title = `Download "${galleryId}"`;
  document.body.appendChild(button);

  button.addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();

    button.disabled = true;
    button.textContent = `Queueing "${galleryId}"`;

    try {
      const result = await browser.runtime.sendMessage({ type: "download", id: galleryId });
      button.textContent = `Queued "${galleryId}"`;
      button.title = `Queued on ${result.server}`;
      button.dataset.state = "done";
    } catch (error) {
      button.disabled = false;
      button.textContent = `Failed "${galleryId}"`;
      button.title = error.message || "Request failed";
      button.dataset.state = "error";
      console.error("nh downloader request failed:", error);
    }
  });
}

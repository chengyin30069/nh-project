const SERVERS = [
  "http://192.168.50.144:8765",
  "http://192.168.193.144:8765",
  "http://100.109.167.26:8765",
];

async function requestDownload(server, id) {
  const response = await fetch(`${server}/api/download`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id }),
  });

  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.error || `HTTP ${response.status}`);
  }
  return { server, body };
}

async function requestStatuses(server, ids) {
  const response = await fetch(`${server}/api/galleries/status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });

  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.error || `HTTP ${response.status}`);
  }
  return { server, body };
}

async function tryServers(callback) {
  let lastError = null;
  for (const server of SERVERS) {
    try {
      return await callback(server);
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error("No downloader server responded");
}

browser.runtime.onMessage.addListener((message) => {
  if (message.type === "download") {
    return tryServers((server) => requestDownload(server, message.id));
  }
  if (message.type === "statuses") {
    return tryServers((server) => requestStatuses(server, message.ids));
  }
  return false;
});

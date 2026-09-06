import { chromium } from "playwright";
import { assert, assertEquals } from "jsr:@std/assert";

const uiScript = await Deno.readTextFile(new URL("../../server/static/local-ui.js", import.meta.url));
const uiStyle = await Deno.readTextFile(new URL("../../server/static/local-ui.css", import.meta.url));

function fixtureHtml(pathname: string): string {
  const detail = /^(?:\/downloads)?\/g\//.test(pathname);
  const content = detail
    ? '<main id="info"><h1>Fixture Detail</h1><a class="btn" href="/download">Download</a><div id="tags"><a href="/artist/alice/">Alice <span class="count" title="123,456 galleries">123.5k</span></a></div></main>'
    : `<main><div class="gallery-grid">
        <div class="gallery"><a class="cover" href="/g/123456/"><img alt="Downloaded fixture"><div class="caption">Downloaded fixture</div></a></div>
        <div class="gallery"><a class="cover" href="/g/654321/"><img alt="Remote fixture [Uncensored]"><div class="caption">Remote fixture [Uncensored]</div></a></div>
      </div></main>`;
  const hydrated = detail
    ? '<h1>Fixture Detail</h1><a class="btn" href="/download">Download</a><div id="tags"><a href="/artist/alice/">Alice <span class="count" title="123,456 galleries">123.5k</span></a></div><button aria-label="Favorite gallery">Favorite</button>'
    : `<div class="gallery-grid">
        <div class="gallery"><a class="cover" href="/g/123456/"><img alt="Downloaded fixture"><div class="caption">Downloaded fixture</div></a></div>
        <div class="gallery"><a class="cover" href="/g/654321/"><img alt="Remote fixture [Uncensored]"><div class="caption">Remote fixture [Uncensored]</div></a></div>
      </div>`;
  return `<!doctype html><html><head><link rel="stylesheet" href="/_nh-local/assets/local.css">
    <script>document.addEventListener("click", (event) => {
      const interactive = event.target?.closest?.("button, input, select, textarea, [role='button'], #nh-delete-modal");
      if (interactive) return;
      const anchor = event.target?.closest?.("a[href]");
      if (!anchor) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      window.location.assign(anchor.href);
    }, true);</script><script defer src="/_nh-local/assets/local.js"></script></head>
    <body ${detail && pathname.includes("123456") ? 'data-nh-downloaded-gallery="true"' : ""}>${content}<script>setTimeout(() => { document.querySelector("${detail ? "#info" : "main"}").innerHTML = ${JSON.stringify(hydrated)}; }, 120);</script></body></html>`;
}

Deno.test("controls survive hydration and match extension behavior", async () => {
  const abort = new AbortController();
  const queuedDownloads: string[] = [];
  const server = Deno.serve({ hostname: "127.0.0.1", port: 0, signal: abort.signal, onListen() {} }, async (request) => {
    const url = new URL(request.url);
    if (url.pathname === "/_nh-local/assets/local.js") {
      return new Response(uiScript, { headers: { "Content-Type": "text/javascript" } });
    }
    if (url.pathname === "/_nh-local/assets/local.css") {
      return new Response(uiStyle, { headers: { "Content-Type": "text/css" } });
    }
    if (url.pathname === "/_nh-local/api/galleries/status") {
      const body = await request.json();
      const galleries = Object.fromEntries(body.ids.map((id: string) => [id, {
        id,
        downloaded: id === "123456",
        job_id: null,
        status: null,
      }]));
      return Response.json({ galleries });
    }
    if (url.pathname === "/_nh-local/api/taxonomies/counts") {
      return Response.json({ counts: { "artist/alice": 7 } });
    }
    if (url.pathname === "/_nh-local/api/download") {
      const body = await request.json();
      queuedDownloads.push(body.id);
      return Response.json({ id: body.id, job_id: "fixture-job", status: "succeeded" });
    }
    return new Response(fixtureHtml(url.pathname), { headers: { "Content-Type": "text/html" } });
  });
  const port = (server.addr as Deno.NetAddr).port;
  const browser = await chromium.launch({ headless: true, channel: "chromium" });
  try {
    const page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${port}/search?q=fixture`);
    await page.waitForTimeout(2100);

    assertEquals(await page.locator(".nh-downloaded-controls").count(), 1);
    assertEquals(await page.locator(".nh-thumb-download-button").count(), 1);
    assertEquals(await page.locator(".nh-thumb-download-button").textContent(), "DL");
    assertEquals(await page.locator(".nh-decensored-marker").count(), 1);
    assertEquals(await page.locator(".nh-scope-toggle a").getAttribute("href"), "/downloads/search/?q=fixture");
    const searchUrl = page.url();
    await page.locator(".nh-thumb-download-button").click();
    await page.waitForTimeout(100);
    assertEquals(page.url(), searchUrl);
    assertEquals(queuedDownloads, ["654321"]);
    await page.locator(".nh-delete-button").click();
    assert((await page.locator(".nh-delete-target").textContent())?.includes("123456"));

    await page.goto(`http://127.0.0.1:${port}/downloads/search/?q=fixture`);
    await page.waitForTimeout(2100);
    assertEquals(await page.locator(".nh-downloaded-marker").count(), 0);
    assertEquals(await page.locator(".nh-delete-button").count(), 1);
    assertEquals(await page.locator(".nh-decensored-marker").count(), 1);
    await page.evaluate(`history.pushState({}, '', '/downloads/artist/alice/?page=3');`);
    await page.locator('.nh-scope-toggle a[href="/artist/alice/"]').waitFor();
    assertEquals(await page.locator(".nh-decensored-marker").count(), 1);

    await page.goto(`http://127.0.0.1:${port}/g/654321/`);
    await page.waitForTimeout(2100);
    assertEquals(await page.locator("#nh-downloader-button").count(), 1);
    assertEquals(await page.locator("#nh-downloader-button").textContent(), "Download");
    assertEquals(await page.locator('button[aria-label*="Favorite" i]').count(), 0);
    const taxonomyToggle = page.locator("[data-nh-taxonomy-toggle]");
    assertEquals(await taxonomyToggle.textContent(), "nhentai");
    assertEquals(await page.locator("#tags a").getAttribute("href"), "/artist/alice/");
    assertEquals(await page.locator("#tags .count").textContent(), "123.5k");
    await taxonomyToggle.click();
    assertEquals(await page.locator("#tags a").getAttribute("href"), "/downloads/artist/alice/");
    assertEquals(await page.locator("#tags .count").textContent(), "7");
    await page.reload();
    await page.waitForTimeout(2100);
    assertEquals(await page.locator("[data-nh-taxonomy-toggle]").textContent(), "Local");
    assertEquals(await page.locator("#tags .count").textContent(), "7");
    await page.locator("[data-nh-taxonomy-toggle]").click();
    assertEquals(await page.locator("[data-nh-taxonomy-toggle]").textContent(), "nhentai");
    assertEquals(await page.locator("#tags .count").textContent(), "123.5k");

    await page.goto(`http://127.0.0.1:${port}/g/123456/`);
    await page.waitForTimeout(2100);
    assertEquals(await page.locator("[data-nh-taxonomy-toggle]").textContent(), "Local");
    assertEquals(await page.locator("#tags a").getAttribute("href"), "/downloads/artist/alice/");
    assertEquals(await page.locator("#tags .count").textContent(), "7");
    await page.locator("[data-nh-taxonomy-toggle]").click();
    await page.reload();
    await page.waitForTimeout(2100);
    assertEquals(await page.locator("[data-nh-taxonomy-toggle]").textContent(), "nhentai");

    const freshTab = await browser.newPage();
    await freshTab.goto(`http://127.0.0.1:${port}/g/654321/`);
    await freshTab.waitForTimeout(2100);
    assertEquals(await freshTab.locator("[data-nh-taxonomy-toggle]").textContent(), "nhentai");
    await freshTab.close();
  } finally {
    await browser.close();
    abort.abort();
    await server.finished.catch(() => undefined);
  }
});

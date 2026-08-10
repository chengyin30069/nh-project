import { chromium } from "playwright";
import { assert, assertEquals, assertMatch } from "jsr:@std/assert";

const enabled = Deno.env.get("NH_RUN_LIVE_E2E") === "1";
const baseUrl = (Deno.env.get("NH_E2E_BASE_URL") || "http://127.0.0.1:8766").replace(/\/$/, "");

Deno.test({
  name: "live gallery canonical navigation, assets, and controls",
  ignore: !enabled,
  fn: async () => {
    const browser = await chromium.launch({ headless: true, channel: "chromium" });
    try {
      const page = await browser.newPage();
      const brokenAssets: string[] = [];
      page.on("response", (response) => {
        if (response.url().includes("/_app/") && response.status() >= 400) {
          brokenAssets.push(`${response.status()} ${response.url()}`);
        }
      });

      await page.goto(`${baseUrl}/search/?q=test`, { waitUntil: "networkidle" });
      assertEquals(new URL(page.url()).pathname, "/search");
      assert((await page.locator(".gallery").count()) > 0);
      await page.locator(".gallery .nh-thumb-download-button, .gallery .nh-downloaded-controls").first().waitFor({ timeout: 10_000 });
      const previewHref = await page.locator(".gallery:has(.nh-thumb-download-button) a.cover").first().getAttribute("href");
      assertEquals(brokenAssets, []);

      await page.goto(`${baseUrl}/downloads/`, { waitUntil: "networkidle" });
      assertEquals(await page.locator(".nh-catalog-grid .gallery").count(), 25);
      assertEquals(await page.locator('a[href="/downloads/random/"]').count() > 0, true);

      await page.goto(`${baseUrl}/downloads/random/`, { waitUntil: "networkidle" });
      const firstRandom = await page.locator(".nh-catalog-grid .gallery a.cover").evaluateAll((items) => items.map((item) => item.getAttribute("href")));
      assertEquals(firstRandom.length, 5);
      await page.reload({ waitUntil: "networkidle" });
      const secondRandom = await page.locator(".nh-catalog-grid .gallery a.cover").evaluateAll((items) => items.map((item) => item.getAttribute("href")));
      assertEquals(secondRandom.length, 5);
      assert(JSON.stringify(firstRandom) !== JSON.stringify(secondRandom));

      if (previewHref) {
        await page.route("**/preview-media/**", (route) => route.abort());
        await page.goto(`${baseUrl}${previewHref}1/`, { waitUntil: "domcontentloaded" });
        assertEquals(await page.locator(".nh-preview-label").textContent(), "Temporary preview");
        assertMatch(await page.locator('a[aria-label="Next page"] img').getAttribute("src") || "", /^\/preview-media\/\d+\/1$/);
        await page.unroute("**/preview-media/**");
      }

      await page.goto(`${baseUrl}/tags/`, { waitUntil: "networkidle" });
      assertEquals(new URL(page.url()).pathname, "/tags");
      assertMatch(await page.title(), /Tags/i);
      assertEquals(brokenAssets, []);

      await page.goto(`${baseUrl}/random/`, { waitUntil: "networkidle" });
      assertMatch(new URL(page.url()).pathname, /^\/g\/\d+\/$/);
      await page.locator("#nh-downloader-button").waitFor({ timeout: 10_000 });
      assertEquals(brokenAssets, []);
    } finally {
      await browser.close();
    }
  },
});

import { chromium } from "playwright";
import { assert, assertEquals } from "jsr:@std/assert";

Deno.test("actual catalog routes, scope, badges, sorting, readers and deletion", async () => {
  const process = new Deno.Command("python3", {
    args: [new URL("./catalog_fixture.py", import.meta.url).pathname],
    stdout: "piped", stderr: "inherit",
  }).spawn();
  const lines = process.stdout.pipeThrough(new TextDecoderStream()).getReader();
  let output = "";
  while (!output.includes("\n")) {
    const next = await lines.read();
    if (next.done) throw new Error("Fixture did not start");
    output += next.value;
  }
  const base = `http://127.0.0.1:${Number(output.trim())}/nh`;
  const browser = await chromium.launch({ headless: true, channel: "chromium" });
  try {
    const page = await browser.newPage();
    const errors: string[] = [];
    const pending = new Set<string>();
    page.on("request", (request) => pending.add(request.url()));
    page.on("requestfinished", (request) => pending.delete(request.url()));
    page.on("requestfailed", (request) => pending.delete(request.url()));
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/fixture.png", (route) => route.fulfill({
      contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="280"/>',
    }));
    await page.goto(`${base}/downloads/`);
    await page.locator(".nh-decensored-marker").first().waitFor();
    const ids = () => page.locator(".gallery a.cover").evaluateAll((links) => links.map((link) => link.getAttribute("href")));
    assertEquals(await ids(), ["/nh/g/9/", "/nh/g/100/"]);
    assertEquals(await page.locator(".nh-decensored-marker").count(), 2);
    const imageBox = await page.locator(".gallery img").first().boundingBox();
    const badgeBox = await page.locator(".nh-decensored-marker").first().boundingBox();
    assert(imageBox && badgeBox);
    assert(Math.abs(badgeBox.y + badgeBox.height - (imageBox.y + imageBox.height - 6)) < 2);
    assert(Math.abs(badgeBox.x - imageBox.x - 6) < 2);
    await page.getByRole("link", { name: "ID ↓", exact: true }).click();
    await page.waitForURL("**/downloads/?sort=id");
    assertEquals(await ids(), ["/nh/g/100/", "/nh/g/9/"]);

    await page.goto(`${base}/downloads/search/?q=Alcie&sort=downloaded&page=5`);
    await page.locator(".nh-scope-toggle a").waitFor();
    assertEquals(await ids(), ["/nh/g/9/", "/nh/g/100/"]);
    assertEquals(await page.locator('.nh-page-jump input[name="sort"]').inputValue(), "downloaded");
    await page.locator(".nh-scope-toggle a").click();
    await page.waitForURL("**/nh/search/?q=Alcie");
    await page.locator(".nh-decensored-marker").waitFor();
    assertEquals(await page.locator(".nh-decensored-marker").count(), 1);
    await page.locator(".nh-scope-toggle a").click();
    await page.waitForURL("**/downloads/search/?q=Alcie");
    assertEquals(await ids(), ["/nh/g/100/", "/nh/g/9/"]);

    for (const type of ["tag", "artist", "character", "parody", "group", "language", "category"]) {
      await page.goto(`${base}/${type}/missing/?page=3`);
      await page.locator(".nh-scope-toggle a").click();
      await page.waitForURL(`**/downloads/${type}/missing/`);
      await page.locator(".nh-scope-toggle a").waitFor();
      assertEquals(await page.locator(".nh-catalog-empty").count(), 1);
      assertEquals(await page.locator(".nh-scope-toggle a").getAttribute("href"), `/nh/${type}/missing/`);
    }

    await page.goto(`${base}/downloads/g/9/`);
    assertEquals(new URL(page.url()).pathname, "/nh/g/9/");
    await page.locator("[data-nh-taxonomy-toggle]").waitFor();
    assertEquals(await page.locator("[data-nh-taxonomy-toggle]").textContent(), "Local");
    assertEquals(await page.locator(".nh-decensored-marker").count(), 0);
    for (const gallery of ["9", "999"]) {
      for (const action of ["next", "image", "keyboard"]) {
        try {
          await page.goto(`${base}/g/${gallery}/2/`, { waitUntil: "domcontentloaded" });
          if (action === "next") await page.locator("#nh-reader-next").click();
          if (action === "image") await page.locator('a[aria-label="Next page"] img').click();
          if (action === "keyboard") await page.keyboard.press("ArrowRight");
          await page.waitForURL(`**/nh/g/${gallery}/`, { waitUntil: "domcontentloaded", timeout: 10000 });
        } catch (error) {
          throw new Error(`Reader ${gallery}, ${action}; URL: ${page.url()}; pending: ${[...pending].join(", ")}`, { cause: error });
        }
      }
    }
    await page.goto(`${base}/g/9/`);
    await page.locator(".nh-delete-button").click();
    await page.locator(".nh-delete-confirm").click();
    await page.getByRole("heading", { name: "Remote Fixture", exact: true }).waitFor();
    assertEquals(new URL(page.url()).pathname, "/nh/g/9/");
    await page.locator("#nh-downloader-button").waitFor();
    assertEquals(await page.locator("#nh-downloader-button").textContent(), "Download");
    assertEquals(errors, []);
  } finally {
    await browser.close();
    process.kill("SIGTERM");
    await process.status;
    await lines.cancel();
  }
});

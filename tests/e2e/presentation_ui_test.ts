/// <reference lib="dom" />
import { chromium, type Page } from "playwright";
import { assert, assertEquals } from "jsr:@std/assert";

async function assertFits(page: Page) {
  await page.waitForFunction(() => {
    const image = document.querySelector<HTMLImageElement>("#nh-reader-image");
    const stage = document.querySelector(".nh-reader-stage");
    if (!image?.naturalWidth || !stage) return false;
    const a = image.getBoundingClientRect(), b = stage.getBoundingClientRect();
    return a.width > 0 && a.x >= b.x && a.y >= b.y && a.right <= b.right && a.bottom <= b.bottom;
  });
  const image = await page.locator("#nh-reader-image").boundingBox();
  const natural = await page.locator("#nh-reader-image").evaluate((img: HTMLImageElement) => [img.naturalWidth, img.naturalHeight]);
  assert(image);
  assert(Math.abs(image.width / image.height - natural[0] / natural[1]) < 0.01);
  assert(image.width <= natural[0] && image.height <= natural[1]);
}

async function assertRows(page: Page, columns: number) {
  await page.waitForFunction((count) => {
    const cards = [...document.querySelectorAll(".nh-full-title-card")];
    return cards.length === 12 && cards.filter((card) => Math.abs(card.getBoundingClientRect().y - cards[0].getBoundingClientRect().y) < 1).length === count;
  }, columns);
  const boxes = await page.locator(".nh-full-title-card").evaluateAll((cards) => cards.map((card) => {
    const caption = card.querySelector<HTMLElement>(".caption")!;
    const rect = card.getBoundingClientRect(), text = caption.getBoundingClientRect();
    return { y: rect.y, height: rect.height, bottom: rect.bottom, textBottom: text.bottom,
      client: caption.clientHeight, scroll: caption.scrollHeight, width: caption.clientWidth, scrollWidth: caption.scrollWidth };
  }));
  for (let row = 0; row < boxes.length; row += columns) {
    for (const box of boxes.slice(row, row + columns)) {
      assert(Math.abs(box.height - boxes[row].height) < 1);
      assert(box.textBottom <= box.bottom + 1);
      assert(box.scroll <= box.client + 1);
      assert(box.scrollWidth <= box.width + 1);
      if (boxes[row + columns]) assert(box.bottom < boxes[row + columns].y);
    }
  }
}

Deno.test("language flags, equal rows, local directories and persistent reader sizes", async () => {
  const process = new Deno.Command("python3", {
    args: [new URL("./catalog_fixture.py", import.meta.url).pathname], stdout: "piped", stderr: "inherit",
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
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/fixture.png", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 180));
      await route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="280"><rect width="200" height="280" fill="#708098"/></svg>' });
    });
    const artifacts = Deno.env.get("NH_E2E_ARTIFACT_DIR");
    if (artifacts) await Deno.mkdir(artifacts, { recursive: true });
    const screenshot = async (name: string) => {
      if (artifacts) await page.screenshot({ path: `${artifacts}/${name}.png`, fullPage: true });
    };

    await page.goto(`${base}/search/?q=layout`);
    await page.waitForTimeout(2700); // Includes a late upstream DOM replacement, after normal retry timers.
    await assertRows(page, 5);
    assertEquals(await page.locator(".caption").first().evaluate((el) => getComputedStyle(el).fontFamily), 'Georgia, serif');
    assertEquals(await page.locator(".nh-delete-button").count(), 2); // Same book appears twice.
    assertEquals(await page.locator(".nh-downloaded-marker").count(), 0);
    assert(!(await page.locator(".index-container").textContent())!.includes("🇨🇳"));
    assert((await page.locator(".caption").first().evaluate((el) => getComputedStyle(el, "::before").content)).includes("🇹🇼"));
    assertEquals(await page.locator(".caption").first().evaluate((el) => getComputedStyle(el, "::before").backgroundImage), "none");
    const priorHeight = await page.locator(".gallery").first().evaluate((el) => el.getBoundingClientRect().height);
    await page.locator(".caption").nth(1).hover();
    assertEquals(await page.locator(".gallery").first().evaluate((el) => el.getBoundingClientRect().height), priorHeight);
    await screenshot("catalog-desktop");
    await page.setViewportSize({ width: 390, height: 844 });
    await assertRows(page, 2);
    await screenshot("catalog-mobile");

    await page.goto(`${base}/downloads/`);
    assertEquals(await page.locator(".caption").first().evaluate((el) => getComputedStyle(el).fontWeight), "700");
    assertEquals(await page.locator(".nh-language-flags").allTextContents(), ["🇹🇼 🇬🇧", "🇹🇼 🇬🇧"]);
    await page.getByRole("navigation", { name: "Downloaded classifications" }).getByRole("link", { name: "Artists", exact: true }).click();
    await page.waitForURL("**/nh/downloads/artists/");
    assertEquals(await page.locator(".nh-taxonomy-directory .nh-taxonomy-count").textContent(), "2");
    await page.locator('.nh-directory-filter input[name="q"]').fill("ALICE");
    await page.locator('.nh-directory-filter select').selectOption("name");
    await page.waitForURL("**/artists/?q=ALICE&sort=name");
    assertEquals(await page.locator(".nh-taxonomy-directory a").count(), 1);
    assertEquals(await page.locator(".nh-scope-toggle a").getAttribute("href"), "/nh/artists/");
    await page.locator(".nh-taxonomy-directory a").click();
    await page.waitForURL("**/nh/downloads/artist/alice/");
    assertEquals(await page.locator(".gallery").count(), 2);
    for (const directory of ["tags", "characters", "parodies", "groups", "languages", "categories"]) {
      await page.goto(`${base}/downloads/${directory}/`);
      await page.locator(".nh-scope-toggle a").waitFor();
      assertEquals(await page.locator(".nh-scope-toggle a").getAttribute("href"), `/nh/${directory}/`);
    }
    await page.goto(`${base}/downloads/languages/`);
    await screenshot("classifications-mobile");
    await page.setViewportSize({ width: 1280, height: 900 });
    await screenshot("classifications-desktop");

    // Both CBZ and temporary readers split the image itself, and all Prev controls
    // on page one lead back to the gallery.
    for (const gallery of ["9", "999"]) {
      for (const action of ["image", "button", "keyboard"]) {
        await page.goto(`${base}/g/${gallery}/1/`);
        await assertFits(page);
        if (action === "image") {
          const box = await page.locator("#nh-reader-image").boundingBox();
          assert(box);
          await page.mouse.click(box.x + box.width * .25, box.y + box.height * .5);
        } else if (action === "button") await page.locator("#nh-reader-prev").click();
        else await page.keyboard.press("ArrowLeft");
        await page.waitForURL(`**/nh/g/${gallery}/`);
      }
      await page.goto(`${base}/g/${gallery}/1/`);
      await assertFits(page);
      let box = await page.locator("#nh-reader-image").boundingBox();
      assert(box);
      await page.mouse.click(box.x + box.width * .75, box.y + box.height * .5);
      await page.waitForURL(`**/nh/g/${gallery}/2/`);
      await assertFits(page);
      box = await page.locator("#nh-reader-image").boundingBox();
      assert(box);
      await page.mouse.click(box.x + box.width * .25, box.y + box.height * .5);
      await page.waitForURL(`**/nh/g/${gallery}/1/`);
      const jump = page.getByRole("spinbutton", { name: "Jump to page" });
      for (const invalid of ["0", "3", "1.5", ""]) {
        await jump.fill(invalid);
        await page.locator(".nh-reader-jump button").click();
        assertEquals(new URL(page.url()).pathname, `/nh/g/${gallery}/1/`);
        assertEquals(await jump.evaluate((el: HTMLInputElement) => el.validity.valid), false);
      }
      await jump.fill("2");
      await jump.press("Enter");
      await page.waitForURL(`**/nh/g/${gallery}/2/`);
      await page.getByRole("spinbutton", { name: "Jump to page" }).fill("1");
      await page.locator(".nh-reader-jump button").click();
      await page.waitForURL(`**/nh/g/${gallery}/1/`);
    }

    // Original-size landscape scrolled horizontally: use the image midpoint,
    // not the viewport midpoint, to decide which page to open.
    await page.goto(`${base}/g/9/2/`);
    await page.locator("#nh-reader-mode").selectOption("original");
    await page.locator(".nh-reader-stage").evaluate((el) => { el.scrollLeft = 600; });
    const scrolled = await page.locator("#nh-reader-image").boundingBox();
    const stage = await page.locator(".nh-reader-stage").boundingBox();
    assert(scrolled && stage);
    await page.mouse.click(scrolled.x + scrolled.width / 2 - 20, stage.y + 40);
    await page.waitForURL("**/nh/g/9/1/");
    await page.locator("#nh-reader-mode").selectOption("fit");

    await page.goto(`${base}/downloads/g/9/1/`);
    assertEquals(new URL(page.url()).pathname, "/nh/g/9/1/");
    assertEquals(await page.locator("#nh-reader-mode").inputValue(), "fit");
    await assertFits(page);
    await screenshot("reader-fit");
    await page.locator("#nh-reader-mode").selectOption("original");
    const natural = await page.locator("#nh-reader-image").boundingBox();
    assert(natural);
    assertEquals([natural.width, natural.height], [1200, 1800]);
    assert(await page.locator(".nh-reader-stage").evaluate((el) => el.scrollHeight > el.clientHeight));
    await page.locator("#nh-reader-mode").focus();
    await page.keyboard.press("ArrowRight");
    assertEquals(new URL(page.url()).pathname, "/nh/g/9/1/");
    await page.reload();
    assertEquals(await page.locator("#nh-reader-mode").inputValue(), "original");
    const tab = await context.newPage();
    await tab.goto(`${base}/g/999/2/`);
    assertEquals(await tab.locator("#nh-reader-mode").inputValue(), "original");
    await page.locator("#nh-reader-mode").selectOption("fit");
    await tab.waitForFunction(() => document.body.dataset.readerMode === "fit");
    await assertFits(tab);
    await tab.close();
    await page.setViewportSize({ width: 390, height: 844 });
    await assertFits(page);
    await screenshot("reader-mobile");
    await page.setViewportSize({ width: 844, height: 390 });
    await assertFits(page);
    await page.goto(`${base}/g/9/2/`);
    await assertFits(page);
    await page.goto(`${base}/g/100/1/`);
    await page.setViewportSize({ width: 1280, height: 900 });
    await assertFits(page);
    const small = await page.locator("#nh-reader-image").boundingBox();
    assert(small);
    assertEquals([small.width, small.height], [200, 280]);

    await page.locator("#nh-reader-mode").selectOption("original");
    const restored = await browser.newContext({ storageState: await context.storageState() });
    const restoredPage = await restored.newPage();
    await restoredPage.goto(`${base}/g/9/1/`);
    assertEquals(await restoredPage.locator("#nh-reader-mode").inputValue(), "original");
    await restoredPage.evaluate(() => localStorage.setItem("nh-reader-mode:/nh", "invalid"));
    await restoredPage.reload();
    assertEquals(await restoredPage.locator("#nh-reader-mode").inputValue(), "fit");
    await restored.close();

    const restricted = await browser.newContext();
    await restricted.addInitScript(() => {
      Storage.prototype.getItem = () => { throw new Error("Storage unavailable"); };
      Storage.prototype.setItem = () => { throw new Error("Storage unavailable"); };
    });
    const restrictedPage = await restricted.newPage();
    restrictedPage.on("pageerror", (error) => errors.push(error.message));
    await restrictedPage.goto(`${base}/g/9/1/`);
    await assertFits(restrictedPage);
    await restrictedPage.locator("#nh-reader-mode").selectOption("original");
    assertEquals(await restrictedPage.locator("body").getAttribute("data-reader-mode"), "original");
    await restricted.close();

    await page.goto(`${base}/g/9/`);
    await page.locator("#nh-downloader-button.nh-delete-button").waitFor();
    assertEquals(await page.getByRole("button", { name: "Downloaded", exact: true }).count(), 0);
    await page.locator(".nh-delete-button").click();
    await page.locator(".nh-delete-confirm").click();
    await page.getByRole("heading", { name: "Remote Fixture", exact: true }).waitFor();
    await page.goto(`${base}/downloads/artists/`);
    assertEquals(await page.locator(".nh-taxonomy-directory .nh-taxonomy-count").textContent(), "1");
    assertEquals(errors, []);
    await context.close();
  } finally {
    await browser.close();
    process.kill("SIGTERM");
    await process.status;
    await lines.cancel();
  }
});

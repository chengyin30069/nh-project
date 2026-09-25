/// <reference lib="dom" />
import { chromium } from "playwright";
import { assert, assertEquals } from "jsr:@std/assert";

Deno.test("assistant actual API, follow-up, safe cards, hydration and reader", async () => {
  const process = new Deno.Command("python3", { args: [new URL("./catalog_fixture.py", import.meta.url).pathname],
    env: { NH_ASSISTANT_FIXTURE: "1" }, stdout: "piped", stderr: "inherit" }).spawn();
  const lines = process.stdout.pipeThrough(new TextDecoderStream()).getReader();
  let output = "";
  while (!output.includes("\n")) { const next = await lines.read(); if (next.done) throw new Error("Fixture did not start"); output += next.value; }
  const base = `http://127.0.0.1:${Number(output.trim())}/nh`;
  const browser = await chromium.launch({ headless: true, channel: "chromium" });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const requests: string[] = [];
    page.on("request", (request) => { if (request.url().includes("/assistant/")) requests.push(request.url()); });
    await page.goto(`${base}/downloads/`);
    assertEquals(requests.length, 0);
    await page.locator(".nh-assistant-toggle").click();
    await page.waitForFunction(() => document.querySelector(".nh-assistant-status")?.textContent?.includes("NIM ok"));
    await page.getByRole("button", { name: "Check NIM connection", exact: true }).click();
    await page.getByText("NIM accepted the key and the configured embedding model responded.", { exact: true }).waitFor();
    assert((await page.locator(".nh-assistant-diagnostics").textContent())?.includes("loaded by server"));
    await page.locator("#nh-assistant-message").fill("Fixture");
    await page.getByRole("button", { name: "Recommend", exact: true }).click();
    await page.locator(".nh-assistant-card").first().waitFor();
    assertEquals(await page.locator(".nh-assistant-card").count(), 2);
    const href = await page.locator(".nh-assistant-card a").first().getAttribute("href");
    assert(href?.match(/^\/nh\/g\/[0-9]+\/$/));
    await page.locator(".nh-assistant-card button").first().click();
    const pending = page.waitForRequest((req) => req.url().endsWith("/assistant/recommend"));
    await page.getByRole("button", { name: "Recommend", exact: true }).click();
    assertEquals((await pending).postDataJSON().previous_plan.excluded_gallery_ids.length, 1);
    await page.locator(".nh-assistant-card").first().waitFor();
    await page.getByRole("button", { name: "Close", exact: true }).click();
    await page.goto(`${base}/search/?q=layout`);
    await page.waitForTimeout(2700);
    assertEquals(await page.locator("#nh-assistant").count(), 1);
    await page.setViewportSize({ width: 375, height: 700 });
    await page.locator(".nh-assistant-toggle").click();
    const box = await page.locator(".nh-assistant-panel").boundingBox();
    assert(box && box.x >= 0 && box.x + box.width <= 375);
    // Reader never opens or fetches assistant health simply because prior page was open.
    const before = requests.length;
    await page.goto(`${base}/g/9/1/`);
    assert(await page.locator(".nh-assistant-panel").isHidden());
    assertEquals(requests.length, before);
    const original = await page.locator(".nh-reader-stage").boundingBox();
    await page.locator(".nh-assistant-toggle").click();
    assertEquals(await page.locator(".nh-reader-stage").boundingBox(), original);
    await page.getByRole("button", { name: "Close", exact: true }).click();
    // Treat all returned prose as text, even if a response contains markup.
    await page.route("**/assistant/recommend", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({
      plan: {}, results: [{ id: "9", title: '<img src=x onerror="window.bad=1">', reasons: ["<script>bad()</script>"] }], warnings: [], assistant_text: "<b>unsafe</b>" }) }));
    await page.locator(".nh-assistant-toggle").click();
    await page.locator("#nh-assistant-message").fill("Fixture");
    await page.getByRole("button", { name: "Recommend", exact: true }).click();
    await page.locator(".nh-assistant-card").waitFor();
    assertEquals(await page.locator(".nh-assistant-results script, .nh-assistant-results b").count(), 0);
    assert((await page.locator(".nh-assistant-card h3").textContent())?.startsWith("<img"));
    assertEquals(errors, []);
  } finally {
    await browser.close(); process.kill("SIGTERM"); await process.status; await lines.cancel();
  }
});

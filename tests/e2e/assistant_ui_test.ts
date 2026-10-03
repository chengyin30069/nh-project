/// <reference lib="dom" />
import { chromium } from "playwright";
import { assert, assertEquals } from "jsr:@std/assert";

Deno.test("dedicated assistant page, async search, resume, mobile, safe cards and reader", async () => {
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
    assertEquals(await page.locator(".nh-assistant-panel").count(), 0);
    await page.getByRole("link", { name: "Library Assistant ↗" }).click();
    assertEquals(new URL(page.url()).pathname, "/nh/AI_assistant");
    await page.waitForFunction(() => document.querySelector(".nh-assistant-status")?.textContent?.includes("NIM ok"));
    const box = await page.locator(".nh-assistant-workspace").boundingBox();
    assert(box && box.width > 1000);
    await page.getByText("Connection & indexes", { exact: true }).click();
    assert(await page.getByRole("button", { name: "Index selected IDs" }).isDisabled());
    assert(await page.getByRole("button", { name: "Index whole library" }).isDisabled());
    await page.getByRole("button", { name: "Check NIM connection", exact: true }).click();
    await page.getByText("NIM accepted the key and the configured embedding model responded.", { exact: true }).waitFor();
    assert((await page.locator(".nh-assistant-diagnostics").textContent())?.includes("loaded by server"));
    await page.locator("#nh-assistant-message").fill("Fixture");
    const response = page.waitForResponse((r) => r.url().endsWith("/assistant/recommend"));
    await page.getByRole("button", { name: "Recommend", exact: true }).click();
    assertEquals((await response).status(), 202);
    await page.locator(".nh-assistant-card").first().waitFor();
    assertEquals(await page.locator(".nh-assistant-card").count(), 2);
    const href = await page.locator(".nh-assistant-card a").first().getAttribute("href");
    assert(href?.match(/^\/nh\/g\/[0-9]+\/$/));
    await page.locator(".nh-assistant-card button").first().click();
    await page.locator("#nh-assistant-message").fill("Fixture shorter");
    const pending = page.waitForRequest((req) => req.url().endsWith("/assistant/recommend"));
    // Hold the first poll in processing so reload deterministically tests resumption.
    let firstPoll = true;
    await page.route("**/assistant/jobs/*", (route) => {
      if (firstPoll) { firstPoll = false; return route.fulfill({ contentType: "application/json", body: '{"status":"processing","stage":"semantic"}' }); }
      return route.continue();
    });
    await page.getByRole("button", { name: "Recommend", exact: true }).click();
    assertEquals((await pending).postDataJSON().previous_plan.excluded_gallery_ids.length, 1);
    await page.waitForFunction(() => Boolean(JSON.parse(sessionStorage.getItem("nh-assistant:/nh") || "{}").job_id));
    await page.reload();
    await page.waitForFunction(() => document.querySelectorAll(".nh-assistant-turn").length === 2);
    await page.setViewportSize({ width: 375, height: 700 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    const artifact = Deno.env.get("NH_E2E_ARTIFACT_DIR");
    if (artifact) { await Deno.mkdir(artifact, { recursive: true }); await page.screenshot({ path: `${artifact}/assistant-mobile.png`, fullPage: true }); }
    await page.goto(`${base}/search/?q=layout`);
    await page.waitForTimeout(2700);
    assertEquals(await page.locator(".nh-assistant-link").count(), 1);
    const before = requests.length;
    await page.goto(`${base}/g/9/1/`);
    assertEquals(await page.locator(".nh-assistant-panel").count(), 0);
    assertEquals(requests.length, before);
    assert(await page.locator("#nh-reader-image").isVisible());
    await page.getByRole("link", { name: "Library Assistant ↗" }).click();
    await page.getByRole("button", { name: "New search", exact: true }).click();
    await page.route("**/assistant/recommend", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({
      status: "ready", plan: {}, results: [{ id: "9", title: '<img src=x onerror="window.bad=1">', reasons: ["<script>bad()</script>"] }], warnings: [], assistant_text: "<b>unsafe</b>" }) }));
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

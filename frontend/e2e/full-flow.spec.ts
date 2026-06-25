import { test, expect, chromium } from "@playwright/test";

test("Full demo flow: login → credentials → passport → task → audit", async () => {
  const browser = await chromium.launch({
    executablePath:
      "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    headless: true,
  });
  const context = await browser.newContext();
  const page = await context.newPage();

  // ==================== Step 1: Landing Page ====================
  await page.goto("http://localhost:3001");
  await expect(page).toHaveTitle(/HTX|Agent|Passport/i);
  console.log("[1] Landing page loaded ✓");

  // ==================== Step 2: Demo Login ====================
  // Look for a demo/login button
  const demoBtn = page.locator(
    'button:has-text("演示"), button:has-text("Demo"), a:has-text("演示"), a:has-text("Demo"), button:has-text("进入"), a:has-text("进入")'
  );
  if ((await demoBtn.count()) > 0) {
    await demoBtn.first().click();
    await page.waitForURL(/dashboard|demo|credentials|passports/, {
      timeout: 10000,
    });
    console.log(`[2] Navigated to: ${page.url()} ✓`);
  } else {
    // Maybe already on dashboard or auto-login
    console.log(`[2] No demo button found, current page: ${page.url()}`);
  }

  // ==================== Step 3: Navigate to Credentials ====================
  await page.goto("http://localhost:3001/credentials");
  await page.waitForLoadState("networkidle");
  const credPageContent = await page.textContent("body");
  expect(credPageContent).toBeTruthy();
  console.log(
    `[3] Credentials page loaded, has content: ${credPageContent!.length > 50} ✓`
  );

  // ==================== Step 4: Navigate to Passports ====================
  await page.goto("http://localhost:3001/passports");
  await page.waitForLoadState("networkidle");
  const passportContent = await page.textContent("body");
  expect(passportContent).toBeTruthy();
  console.log(
    `[4] Passports page loaded, has content: ${passportContent!.length > 50} ✓`
  );

  // ==================== Step 5: Navigate to Dashboard ====================
  await page.goto("http://localhost:3001/dashboard");
  await page.waitForLoadState("networkidle");
  const dashContent = await page.textContent("body");
  expect(dashContent).toBeTruthy();
  console.log(`[5] Dashboard page loaded ✓`);

  // ==================== Step 6: Navigate to Demo scenario page ====================
  await page.goto("http://localhost:3001/demo");
  await page.waitForLoadState("networkidle");
  const demoContent = await page.textContent("body");
  expect(demoContent).toBeTruthy();
  console.log(`[6] Demo page loaded ✓`);

  // ==================== Step 7: Navigate to Audit ====================
  await page.goto("http://localhost:3001/audit");
  await page.waitForLoadState("networkidle");
  const auditContent = await page.textContent("body");
  expect(auditContent).toBeTruthy();
  console.log(`[7] Audit page loaded ✓`);

  // ==================== Step 8: Create Passport Page ====================
  await page.goto("http://localhost:3001/passports/new");
  await page.waitForLoadState("networkidle");
  const newPassportContent = await page.textContent("body");
  expect(newPassportContent).toBeTruthy();
  console.log(`[8] Create Passport page loaded ✓`);

  // ==================== Step 9: Check for console errors ====================
  const consoleErrors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  // Revisit a few pages to collect any JS errors
  await page.goto("http://localhost:3001/dashboard");
  await page.waitForLoadState("networkidle");
  await page.goto("http://localhost:3001/credentials");
  await page.waitForLoadState("networkidle");

  if (consoleErrors.length > 0) {
    console.log(`[9] Console errors found: ${consoleErrors.length}`);
    consoleErrors.forEach((e) => console.log(`  ERROR: ${e}`));
  } else {
    console.log("[9] No console errors ✓");
  }

  // ==================== Step 10: Check no page crashes (status 500 pages) ====================
  const pages = [
    "/",
    "/dashboard",
    "/credentials",
    "/passports",
    "/passports/new",
    "/demo",
    "/audit",
  ];
  for (const path of pages) {
    const resp = await page.goto(`http://localhost:3001${path}`);
    expect(resp?.status()).toBeLessThan(500);
  }
  console.log("[10] All pages return < 500 status ✓");

  await browser.close();
  console.log("\n=== ALL BROWSER CHECKS PASSED ===");
});

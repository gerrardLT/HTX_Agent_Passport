import { chromium } from 'playwright';

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

await page.goto('http://localhost:8080/variant-a-swiss-terminal.html');
await page.waitForTimeout(2000);

// Scroll to bottom in steps to trigger IntersectionObserver
const height = await page.evaluate(() => document.body.scrollHeight);
const step = 400;
for (let y = 0; y <= height; y += step) {
  await page.evaluate((scrollY) => window.scrollTo(0, scrollY), y);
  await page.waitForTimeout(300);
}

// Scroll back to top
await page.evaluate(() => window.scrollTo(0, 0));
await page.waitForTimeout(1000);

await page.screenshot({ path: 'variant-a-final.png', fullPage: true });
await browser.close();
console.log('Done!');

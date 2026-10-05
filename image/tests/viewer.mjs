// Open the agos KasmVNC web client as a human would and screenshot it.
import { chromium } from 'playwright';
const [url, user, pass, out] = process.argv.slice(2);
const browser = await chromium.launch();
const ctx = await browser.newContext({ httpCredentials: { username: user, password: pass },
                                       ignoreHTTPSErrors: true, viewport: { width: 1400, height: 900 } });
const page = await ctx.newPage();
const ws = [];
page.on('websocket', w => ws.push(w.url()));
await page.goto(url, { waitUntil: 'load' });
await page.waitForTimeout(10000);
await page.screenshot({ path: out });
console.log(JSON.stringify({ title: await page.title(), websockets: ws }));
await browser.close();

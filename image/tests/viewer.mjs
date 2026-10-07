// Open the agos KasmVNC web client as a human would and screenshot it.
import { chromium } from 'playwright';
// Optional 5th argument: base64 SHA-256 of the viewer certificate's public key.
// With it, Chromium trusts exactly that key; without it (throwaway boot-test
// VMs on loopback) certificate errors are ignored.
const [url, user, pass, out, spki] = process.argv.slice(2);
const browser = await chromium.launch(spki ? { args: [`--ignore-certificate-errors-spki-list=${spki}`] } : {});
const ctx = await browser.newContext({ httpCredentials: { username: user, password: pass },
                                       ignoreHTTPSErrors: !spki, viewport: { width: 1400, height: 900 } });
const page = await ctx.newPage();
const ws = [];
page.on('websocket', w => ws.push(w.url()));
await page.goto(url, { waitUntil: 'load' });
await page.waitForTimeout(10000);
await page.screenshot({ path: out });
console.log(JSON.stringify({ title: await page.title(), websockets: ws }));
await browser.close();

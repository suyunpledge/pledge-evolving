// Render the actual webview HTML/CSS/JS in an installed headless browser.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const http = require('node:http');
const Module = require('node:module');
const { chromium } = require('@playwright/test');

test('webview is responsive, safe for model text, cancellable and IME-aware', { timeout: 60000 }, async () => {
  const root = path.resolve(__dirname, '..');
  // VS Code normally injects these theme variables into every webview.
  const theme = `:root { --vscode-font-family: 'Segoe UI', sans-serif; --vscode-font-size: 13px;
    --vscode-foreground: #ddd; --vscode-sideBar-background: #181b22; --vscode-panel-border: #353a48;
    --vscode-descriptionForeground: #a2a9b8; --vscode-testing-iconPassed: #61c699;
    --vscode-progressBar-background: #759aff; --vscode-button-border: #3f4657;
    --vscode-button-secondaryBackground: #272c38; --vscode-button-secondaryForeground: #ddd;
    --vscode-button-secondaryHoverBackground: #343b4c; --vscode-focusBorder: #7e9efc;
    --vscode-input-foreground: #eee; --vscode-input-background: #222733; --vscode-input-border: #3b4455;
    --vscode-dropdown-foreground: #ddd; --vscode-dropdown-background: #272c38; --vscode-dropdown-border: #414958;
    --vscode-button-background: #5368d9; --vscode-button-foreground: #fff; --vscode-button-hoverBackground: #6278eb;
    --vscode-textLink-foreground: #9eb2ff; --vscode-editorWarning-foreground: #ddb668; }
    body.vscode-light { --vscode-foreground: #333; --vscode-sideBar-background: #f8f8f8;
      --vscode-input-foreground: #222; --vscode-input-background: #fff; --vscode-descriptionForeground: #606060;
      --vscode-button-secondaryBackground: #eee; --vscode-button-secondaryForeground: #333;
      --vscode-dropdown-foreground: #222; --vscode-dropdown-background: #fff; }
    body.vscode-high-contrast { --vscode-foreground: #fff; --vscode-sideBar-background: #000; --vscode-panel-border: #fff; }
  `;
  let html;
  const server = http.createServer(async (req, res) => {
    if (req.url === '/') { res.writeHead(200, { 'Content-Type': 'text/html' }); res.end(html); return; }
    const asset = req.url === '/media/chat.css' ? 'chat.css' : req.url === '/media/chat.js' ? 'chat.js' : '';
    if (!asset) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { 'Content-Type': asset.endsWith('.css') ? 'text/css' : 'text/javascript' });
    const data = await fs.readFile(path.join(root, 'media', asset));
    res.end(asset.endsWith('.css') ? theme + data.toString() : data);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  const load = Module._load;
  try {
    Module._load = function (name, ...args) {
      if (name === 'vscode') { return { Uri: { joinPath: (_, ...segments) => '/' + segments.join('/') } }; }
      return load.call(this, name, ...args);
    };
    html = require('../out/view').viewHtml({ cspSource: url, asWebviewUri: resource => url + resource }, '/');
  } finally { Module._load = load; }
  const browser = await chromium.launch(process.env.FORGE_TEST_BROWSER
    ? { executablePath: process.env.FORGE_TEST_BROWSER, headless: true }
    : process.platform === 'win32' ? { channel: 'msedge', headless: true } : { headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 320, height: 800 }, deviceScaleFactor: 1.5 });
    const page = await context.newPage();
    await page.addInitScript(() => {
      window.sent = [];
      window.acquireVsCodeApi = () => ({ postMessage: message => window.sent.push(message) });
    });
    await page.goto(url);
    const state = { type: 'state', language: 'zh-cn', trusted: true, busy: false, status: 'ready', activity: '',
      workspace: 'test', model: '', mode: 'read-only', attachments: [], models: [], transcript: [] };
    const update = async () => { await page.evaluate(value => window.postMessage(value, '*'), state); await page.waitForFunction(() => document.getElementById('status').textContent.length > 0); };
    await update();
    assert.equal(await page.locator('#welcome').textContent(), '在代码旁边，与 Forge 协作');
    state.transcript = [{ role: 'assistant', text: '<img src=x onerror="window.injected=true"> 😀 长内容'.repeat(20) }];
    await update();
    assert.equal(await page.locator('.message-body img').count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    assert((await page.locator('.message-body').textContent()).endsWith('长内容'));
    for (const width of [220, 320, 480, 800]) {
      await page.setViewportSize({ width, height: 800 });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `horizontal overflow at ${width}px`);
    }
    for (const scale of [1, 1.25, 1.75, 2]) {
      const scaled = await browser.newContext({ viewport: { width: 320, height: 800 }, deviceScaleFactor: scale });
      const scaledPage = await scaled.newPage();
      await scaledPage.addInitScript(() => { window.acquireVsCodeApi = () => ({ postMessage() {} }); });
      await scaledPage.goto(url);
      await scaledPage.evaluate(value => window.postMessage(value, '*'), state);
      await scaledPage.waitForSelector('.message-body');
      assert(await scaledPage.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `overflow at DPI ${scale}`);
      assert(await scaledPage.locator('.message-body').evaluate(node => node.scrollHeight <= node.clientHeight + 1), `vertical clipping at DPI ${scale}`);
      await scaled.close();
    }
    for (const themeClass of ['vscode-light', 'vscode-high-contrast', 'vscode-dark']) {
      await page.evaluate(value => { document.body.className = value; }, themeClass);
      await page.setViewportSize({ width: 320, height: 800 });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
      assert(await page.locator('#prompt').isVisible());
    }
    await page.locator('#prompt').fill('解释这段代码');
    await page.locator('#prompt').evaluate(element => element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, isComposing: true, bubbles: true })));
    assert.equal(await page.evaluate(() => window.sent.filter(m => m.type === 'send').length), 0);
    await page.locator('#prompt').press('Control+Enter');
    assert.equal(await page.evaluate(() => window.sent.filter(m => m.type === 'send').length), 1);
    state.busy = true; await update();
    await page.waitForFunction(() => !document.getElementById('stop').hidden);
    assert(await page.locator('#send').isDisabled());
    await page.locator('#stop').click();
    assert.equal(await page.evaluate(() => window.sent.at(-1).type), 'stop');
    state.busy = false; state.trusted = false; state.status = 'offline'; state.language = 'en'; await update();
    await page.waitForFunction(() => document.getElementById('status').textContent === 'Disconnected');
    assert(await page.locator('#connect').isDisabled()); assert(await page.locator('#send').isDisabled());
    state.trusted = true; state.language = 'zh-cn'; state.transcript = []; await update();
    await page.waitForFunction(() => document.getElementById('welcome').textContent === '在代码旁边，与 Forge 协作');
    await page.screenshot({ path: path.join(osTemp(), 'forge-vscode-webview.png') });
    await context.close();
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
});
function osTemp() { return require('node:os').tmpdir(); }

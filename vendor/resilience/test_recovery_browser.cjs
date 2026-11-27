const assert = require('node:assert/strict');
const { once } = require('node:events');
const { spawn } = require('node:child_process');
const path = require('node:path');
const { chromium, devices } = require(process.env.DEX_TEST_PLAYWRIGHT || "playwright-core");

async function check(browser, options, label) {
  const remote = spawn('/usr/bin/python3', ['-B', 'browser_fixture.py'], {
    cwd: __dirname, stdio: ['ignore', 'pipe', 'inherit'],
    env: { ...process.env, PYTHONPATH: process.env.PYTHONPATH || "/usr/local/lib/dex-rescue" }
  });
  const context = await browser.newContext(options);
  try {
    const [chunk] = await once(remote.stdout, 'data');
    const base = chunk.toString().trim();
    const page = await context.newPage();
    assert.equal((await page.goto(base + '/__dex_recovery/')).status(), 403);
    await context.addCookies([{ name: 'fixture', value: 'authorized', url: base }]);
    assert.equal((await page.goto(base)).status(), 200);
    assert.ok(page.url().endsWith('/__dex_recovery/'));
    const button = page.getByRole('button', { name: 'Restaurar Dex' });
    await page.getByText(/não está aceitando mensagens/).waitFor();
    const [response] = await Promise.all([
      page.waitForResponse(r => r.url().endsWith('/restore') && r.request().method() === 'POST'),
      button.click()
    ]);
    assert.equal(response.status(), 202);
    await page.getByText(/Codex CLI está trabalhando/).waitFor({ timeout: 10000 });
    assert.ok(await button.isDisabled());
    await page.getByText(/Dex está aceitando mensagens/).waitFor({ timeout: 18000 });
    const returned = page.waitForResponse(r => r.url() === base + '/' && r.request().isNavigationRequest(), { timeout: 8000 });
    await page.getByRole('link', { name: 'Voltar ao Dex' }).click();
    const document = await returned;
    assert.equal(document.status(), 200);
    assert.match(await document.text(), /<h1>Dex de teste<\/h1>/);
    await page.waitForURL(base + '/', { timeout: 5000 });
    await page.getByRole('heading', { name: 'Dex de teste' }).waitFor({ timeout: 5000 });
    console.log(`${label}: authenticated fallback, start, progress and return passed`);
  } finally {
    await context.close();
    remote.kill();
  }
}

(async () => {
  const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome' });
  try {
    await Promise.all([
      check(browser, { viewport: { width: 1440, height: 900 } }, 'desktop'),
      check(browser, devices['Pixel 7'], 'mobile emulation')
    ]);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

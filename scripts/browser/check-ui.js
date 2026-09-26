/**
 * Drive the PWA in a real browser and screenshot it.
 *
 * Closes the one verification gap that unit tests cannot: whether the bundle
 * actually renders. Everything else in this repo is checked structurally --
 * assets resolve, endpoints return the right shapes -- which says nothing
 * about what the user sees.
 *
 * Runs the real login flow, waits for live MQTT data to arrive over the
 * WebSocket, and captures desktop and phone viewports. Console errors and
 * failed requests are collected and reported, because a page can screenshot
 * perfectly while quietly throwing.
 *
 *   npm --prefix scripts/browser install     # once
 *   node scripts/browser/check-ui.js         # desktop + phone
 *   node scripts/browser/check-ui.js --out shots/x
 *   node scripts/browser/check-ui.js --wait 90    # longer wait for live data
 *
 * Exits non-zero if the live feed does not come up, so it can gate CI.
 */

const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '../..');
const { chromium } = require('playwright');

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const BASE = arg('base', 'http://127.0.0.1:8000');
const OUT = path.resolve(ROOT, arg('out', 'shots'));
const WAIT = Number(arg('wait', '45'));

/** Pull the admin password out of .env without printing it. */
function password() {
  const env = fs.readFileSync(path.join(ROOT, '.env'), 'utf8');
  const m = env.match(/^API_ADMIN_PASSWORD=(.*)$/m);
  if (!m) throw new Error('API_ADMIN_PASSWORD not found in .env');
  return m[1].trim();
}

const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 1000 },
  { name: 'phone', width: 390, height: 844, mobile: true },
];

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const password_ = password();
  const browser = await chromium.launch({ headless: true });
  const problems = [];
  const results = {};

  for (const vp of VIEWPORTS) {
    const context = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 2,
      isMobile: !!vp.mobile,
      hasTouch: !!vp.mobile,
    });
    const page = await context.newPage();

    page.on('console', (m) => {
      if (m.type() === 'error') problems.push(`[${vp.name}] console: ${m.text()}`);
    });
    page.on('pageerror', (e) => problems.push(`[${vp.name}] pageerror: ${e.message}`));
    page.on('requestfailed', (r) => {
      // WebSocket closes on navigation are expected, not a defect.
      if (r.url().startsWith('ws')) return;
      problems.push(`[${vp.name}] request failed: ${r.url()} ${r.failure()?.errorText}`);
    });
    page.on('response', (r) => {
      if (r.status() >= 400 && !r.url().includes('/api/auth/login')) {
        problems.push(`[${vp.name}] HTTP ${r.status()} ${r.url()}`);
      }
    });

    console.log(`\n=== ${vp.name} (${vp.width}x${vp.height}) ===`);
    await page.goto(BASE, { waitUntil: 'domcontentloaded' });

    // The app is a login form until authenticated; the token lives in memory
    // only, so every run goes through the real flow.
    await page.waitForSelector('input[type=password]', { timeout: 20000 });
    await page.screenshot({ path: path.join(OUT, `${vp.name}-1-login.png`) });
    console.log('  captured login screen');

    await page.fill('input[autocomplete=username]', 'admin');
    await page.fill('input[type=password]', password_);
    await page.click('button[type=submit]');

    // Fail fast on a login error rather than timing out on a selector. The API
    // rate-limits logins (10 per 5 minutes by default), and a run that trips it
    // is otherwise indistinguishable from a broken dashboard.
    const loginError = await page
      .waitForSelector('.error', { timeout: 12000 })
      .then((el) => el.textContent().trim())
      .catch(() => null);
    if (loginError) {
      throw new Error(
        `login rejected: "${loginError}"\n` +
          '  The API rate-limits logins. If this is 429, wait ~5 minutes, or set ' +
          'LOGIN_RATE_LIMIT higher for repeated checks.'
      );
    }

    // Wait for the dashboard shell, then for live data to actually land in the
    // tiles. Rendering the shell is not evidence that telemetry arrived.
    //
    // Selected by class, not by text: the label is CSS-uppercased for display
    // and its wording is free to change, and a text selector turned out to be
    // flaky across viewports for no benefit.
    await page.waitForSelector('.tile-accent', { timeout: 30000 });
    console.log('  dashboard shell rendered');

    // Wait for real data, not merely for a rendered shell. Two conditions: the
    // MQTT feed must report itself live, and a tile must hold a non-zero
    // number. Comparing the tile text against "0.0 kW" is not enough -- the
    // pre-data value is "0 W", which is not that string, so such a check exits
    // on the very first poll and captures an empty dashboard.
    const deadline = Date.now() + WAIT * 1000;
    let seen = null;
    let live = false;
    while (Date.now() < deadline) {
      live = (await page.textContent('.banner-label').catch(() => null))?.trim() === 'Live';
      const tile = (await page.textContent('.tile-accent .tile-value').catch(() => null))?.trim();
      const watts = tile ? parseFloat(tile) : NaN;
      if (live && Number.isFinite(watts) && watts > 0) {
        seen = tile;
        break;
      }
      await page.waitForTimeout(1000);
    }
    console.log(`  live banner    : ${live ? 'Live' : 'NOT live'}`);
    console.log(`  site power tile: ${seen ?? 'never became non-zero'}`);
    const summary = await page.evaluate(() => {
      const text = (sel) => document.querySelector(sel)?.textContent?.trim() ?? null;
      const tiles = [...document.querySelectorAll('.tile')].map((t) => ({
        label: t.querySelector('.tile-label')?.textContent?.trim(),
        value: t.querySelector('.tile-value')?.textContent?.trim(),
      }));
      return {
        banner: text('.banner-label'),
        bannerDetail: text('.banner-detail'),
        tiles,
        inverterCards: document.querySelectorAll('.card').length,
        heatCells: document.querySelectorAll('.heat-cell').length,
        chartPaths: document.querySelectorAll('.chart path[stroke]').length,
        rules: document.querySelectorAll('.rule').length,
        events: document.querySelectorAll('.event').length,
      };
    });
    console.log('  live banner   :', summary.banner, '-', summary.bannerDetail);
    console.log('  tiles         :', summary.tiles.map((t) => `${t.label}=${t.value}`).join('  '));
    console.log('  inverter cards:', summary.inverterCards);
    console.log('  chart series  :', summary.chartPaths, 'paths');
    console.log('  string cells  :', summary.heatCells);
    console.log('  rules listed  :', summary.rules);

    results[vp.name] = {
      live,
      power: Boolean(seen),
      cards: summary.inverterCards,
      series: summary.chartPaths,
      rules: summary.rules,
    };

    // Full page, and a viewport-sized shot for a realistic first impression.
    await page.screenshot({ path: path.join(OUT, `${vp.name}-2-dashboard.png`), fullPage: true });
    await page.screenshot({ path: path.join(OUT, `${vp.name}-3-viewport.png`) });
    console.log(`  screenshots written to ${OUT}`);

    await context.close();
  }

  await browser.close();

  console.log('\n=== console / network ===');
  if (problems.length === 0) {
    console.log('  no errors, no failed requests');
  } else {
    for (const p of [...new Set(problems)]) console.log('  !', p);
  }

  // Gate on the outcome, not merely on the script completing. Every other check
  // in this repo is structural -- assets resolve, endpoints return the right
  // shape, 208 tests pass -- and all of them were green while the live feed
  // was throwing `mqttModule.connect is not a function` in the browser. The
  // dashboard looked perfect and was completely dead. This is the only check
  // that can see that class of failure, so it has to fail loudly.
  const failures = [];
  for (const [name, s] of Object.entries(results)) {
    if (!s.live) failures.push(`${name}: MQTT feed never reported Live`);
    if (!s.power) failures.push(`${name}: site power tile never became non-zero`);
    if (s.cards === 0) failures.push(`${name}: no inverter cards rendered`);
    if (s.series === 0) failures.push(`${name}: chart drew no series`);
    if (s.rules === 0) failures.push(`${name}: no alert rules listed`);
  }
  for (const p of new Set(problems)) failures.push(p);

  console.log(`\nwrote ${fs.readdirSync(OUT).length} files to ${OUT}`);
  if (failures.length) {
    console.log('\nFAILED:');
    for (const f of failures) console.log('  -', f);
    process.exit(1);
  }
  console.log('\nPWA renders and the live feed is up.');
})().catch((e) => {
  console.error('FAILED:', e.message);
  process.exit(1);
});

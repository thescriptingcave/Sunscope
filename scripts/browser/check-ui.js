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

// The site this simulator models: 36.2 N, 115.1 W, near Mojave, Nevada.
// Kept in step with topology.py's Site(). Hardcoding it here rather than importing
// means this check has no Python dependency, which is what lets it run in the
// browser-check step without pulling in the sim environment.
const SITE = { latitude: 36.2, longitude: -115.1 };

const DEG = Math.PI / 180;

/**
 * Sunrise and sunset for the site today, in minutes after midnight UTC.
 *
 * WHY THIS IS HERE
 * The check below used to require the site-power tile to become non-zero. That is
 * only true while the sun is up: the simulator publishes nothing after sunset, so
 * from dusk until dawn the dashboard correctly shows 0 W -- and the check failed
 * every single night, on correct behaviour. Neither the API nor the PWA exposes a
 * `sun_up` flag, and the database cannot answer it either, because "the sun is
 * down" and "the feed is dead" look identical in the data. So the check computes
 * daylight itself.
 *
 * The formulae are the standard NOAA sunrise/sunset approximation, accurate to
 * about a minute for this latitude, which is far tighter than the ~20 minutes of
 * dawn and dusk twilight the assertion is skipped across anyway.
 *
 * Returns null when the sun does not rise or set on this date, which cannot happen
 * at 36 degrees north but is handled rather than assumed.
 */
function daylightWindowUtc(date = new Date()) {
  const startOfYear = Date.UTC(date.getUTCFullYear(), 0, 1);
  const dayOfYear = Math.floor((date - startOfYear) / 86400000) + 1;

  // Fractional year, in radians.
  const gamma = ((2 * Math.PI) / 365) * (dayOfYear - 1 + (date.getUTCHours() - 12) / 24);

  // Equation of time, in minutes.
  const eqtime =
    229.18 *
    (0.000075 +
      0.001868 * Math.cos(gamma) -
      0.032077 * Math.sin(gamma) -
      0.014615 * Math.cos(2 * gamma) -
      0.040849 * Math.sin(2 * gamma));

  // Solar declination, in radians.
  const decl =
    0.006918 -
    0.399912 * Math.cos(gamma) +
    0.070257 * Math.sin(gamma) -
    0.006758 * Math.cos(2 * gamma) +
    0.000907 * Math.sin(2 * gamma) -
    0.002697 * Math.cos(3 * gamma) +
    0.00148 * Math.sin(3 * gamma);

  // Hour angle at sunrise, including the standard -0.833 deg for refraction and
  // the solar disc radius, so this is the moment the sun is actually visible.
  const cosHa =
    Math.cos(90.833 * DEG) / (Math.cos(SITE.latitude * DEG) * Math.cos(decl)) -
    Math.tan(SITE.latitude * DEG) * Math.tan(decl);
  if (cosHa > 1 || cosHa < -1) return null;
  const ha = Math.acos(cosHa) / DEG;

  // The hour angle enters sunrise and sunset with opposite signs. Getting this backwards
  // is easy and produces a window that is the right *width* and the right *endpoints*,
  // merely labelled inside out -- so `isDaylight` returns true at midnight and the
  // non-zero-power assertion runs all night, which is the bug this whole function exists
  // to fix. Validated against pvlib's sun_rise_set_transit_spa below.
  let sunrise = 720 - 4 * (SITE.longitude + ha) - eqtime;
  let sunset = 720 - 4 * (SITE.longitude - ha) - eqtime;
  // Sunset usually crosses midnight UTC, so the window is [sunrise, sunset + 1440).
  // Normalising both keeps the comparison below a single interval test.
  if (sunset < sunrise) sunset += 1440;
  return { sunrise, sunset };
}

/** True when the sun is up at the site right now. */
function isDaylight(date = new Date()) {
  const window = daylightWindowUtc(date);
  if (window === null) return false;
  const minutes = date.getUTCHours() * 60 + date.getUTCMinutes();
  // Before dawn the clock has not reached `sunrise`; after dusk it has passed
  // `sunset`, which is now in tomorrow's range.
  const elapsed = minutes >= window.sunrise ? minutes : minutes + 1440;
  return elapsed < window.sunset;
}

function fmtMinutes(mins) {
  const h = Math.floor(mins / 60) % 24;
  const m = Math.floor(mins % 60);
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}Z`;
}

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

    // Wait for *either* outcome in a single race, rather than checking for an
    // error and then waiting for the dashboard. The sequential version could
    // miss an error that appeared and was replaced, and reported a rate-limited
    // run as "timed out waiting for .tile-accent", which is indistinguishable
    // from a broken dashboard.
    const outcome = await Promise.race([
      page
        .waitForSelector('.tile-accent', { timeout: 45000 })
        .then(() => ({ ok: true })),
      // `textContent()` is async on an ElementHandle. Calling `.trim()` on its
      // return value throws a TypeError, which masked the real message with
      // "textContent(...).trim is not a function" -- an error path that fails
      // silently is worse than no error path.
      page
        .waitForSelector('.error', { timeout: 45000 })
        .then(async (el) => ({ ok: false, error: ((await el.textContent()) ?? '').trim() })),
    ]);

    if (!outcome.ok) {
      throw new Error(
        `login rejected: "${outcome.error}"\n` +
          '  The API rate-limits logins (10 per 5 minutes by default). Repeated ' +
          'local runs will trip it; wait, or raise LOGIN_RATE_LIMIT.'
      );
    }
    console.log('  dashboard shell rendered');

    // The role badge. It is the only part of the header driven by something other than
    // site config, so it is the part most likely to regress silently -- a missing role
    // leaves the header looking perfectly fine.
    const role = (await page.textContent('.role-badge').catch(() => null))?.trim();
    if (role !== 'admin') {
      throw new Error(
        `expected the role badge to read "admin", got ${JSON.stringify(role)}.\n` +
          '  The login response carries a role and useAuth stores it. If the badge is ' +
          '  missing entirely, either the role never arrived from /api/auth/login or ' +
          '  Header stopped rendering it.'
      );
    }
    console.log(`  role badge     : ${role}`);

    // Wait for real data, not merely for a rendered shell. The MQTT feed must report
    // itself live, and the tile must hold a number that is plausible for the time of day.
    //
    // "Non-zero" is only the right requirement while the sun is up. The simulator
    // publishes nothing after sunset, so from dusk until dawn the dashboard correctly
    // shows 0 W -- and requiring non-zero made this check fail every night, on correct
    // behaviour. `isDaylight()` decides which requirement applies.
    //
    // In daylight the requirement stays strict, and stays strict about *when* it is
    // applied: comparing the tile text against "0.0 kW" is not enough, because the
    // pre-data value is "0 W", which is not that string, so such a check exits on the
    // very first poll and captures an empty dashboard.
    const daylight = isDaylight();
    const window = daylightWindowUtc();
    console.log(
      `  site daylight  : ${daylight ? 'yes' : 'no'} ` +
        `(sun ${fmtMinutes(window.sunrise)}-${fmtMinutes(window.sunset)})`,
    );
    const wattsRequired = daylight;

    const deadline = Date.now() + WAIT * 1000;
    let seen = null;
    let live = false;
    while (Date.now() < deadline) {
      live = (await page.textContent('.banner-label').catch(() => null))?.trim() === 'Live';
      const tile = (await page.textContent('.tile-accent .tile-value').catch(() => null))?.trim();
      const watts = tile ? parseFloat(tile) : NaN;
      // Out of daylight, a finite non-negative number is the pass condition -- including
      // the "0 W" that is the honest answer at 23:00. A NaN, or a negative number, is a
      // real failure either way and is caught by `wattsAccepted` below.
      const wattsAccepted = Number.isFinite(watts) && watts >= 0 && (!wattsRequired || watts > 0);
      if (live && wattsAccepted) {
        seen = tile;
        break;
      }
      await page.waitForTimeout(1000);
    }
    console.log(`  live banner    : ${live ? 'Live' : 'NOT live'}`);
    console.log(
      `  site power tile: ${seen ?? (wattsRequired ? 'never became non-zero' : 'never became a number')}`,
    );
    if (!daylight) {
      console.log('  note           : after dark, so 0 W is the correct value and is not treated as a dead feed');
    }
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
  // shape, hundreds of tests pass -- and all of them were green while the live feed
  // was throwing `mqttModule.connect is not a function` in the browser. The
  // dashboard looked perfect and was completely dead. This is the only check
  // that can see that class of failure, so it has to fail loudly.
  // Re-evaluated here, at the point of reporting, so the message names the requirement
  // that actually applied. `results` was collected per viewport, and daylight is a
  // property of the clock rather than of the viewport, so one decision covers them all.
  const daylightNow = isDaylight();
  const powerRequirement = daylightNow
    ? 'site power tile never became non-zero'
    : 'site power tile never became a number';

  const failures = [];
  for (const [name, s] of Object.entries(results)) {
    if (!s.live) failures.push(`${name}: MQTT feed never reported Live`);
    if (!s.power) failures.push(`${name}: ${powerRequirement}`);
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

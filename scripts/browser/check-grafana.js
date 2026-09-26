#!/usr/bin/env node
/**
 * Headless render check for the provisioned Grafana dashboards.
 *
 * WHY THIS EXISTS
 *
 * Every panel query can execute and still leave a dashboard that renders empty
 * or shows a red "Query error" badge, because Grafana's own rendering path does
 * more than /api/ds/query: it resolves template variables, applies the time
 * picker, and renders frames into panel types. A green query API is necessary
 * but not sufficient, so this asserts on the rendered DOM.
 *
 * It also checks the template variable resolves, which the query API cannot
 * tell you about -- a variable that fails to load degrades the panel query to
 * an unfiltered one rather than erroring.
 *
 * Usage:  node check-grafana.js [--keep-shots]
 * Exits non-zero on any failure, so it can gate CI like check-ui.js does.
 */

const { chromium } = require('playwright')
const fs = require('fs')
const path = require('path')

const ROOT = path.resolve(__dirname, '..', '..')
const SHOTS = path.join(ROOT, 'shots')
const BASE = process.env.GRAFANA_URL || 'http://127.0.0.1:3000'

const DASHBOARDS = [
  { slug: 'solar-overview', title: 'Sunscope — Overview', expect: 8 },
  { slug: 'solar-analysis', title: 'Sunscope — Analysis', expect: 4 },
]

/**
 * Grafana 12 puts `data-testid="data-testid Panel header <title>"` on the panel
 * <section>, and the title is recoverable from the attribute. Matching on a
 * fixed string is brittle; matching the prefix is not.
 */
const PANEL = 'section[data-testid^="data-testid Panel header"]'

/** The variable control's testid also carries the label, so match the prefix. */
const VAR_LABEL = '[data-testid^="data-testid Dashboard template variables submenu Label"]'

function envValue(key) {
  const env = path.join(ROOT, '.env')
  if (!fs.existsSync(env)) return null
  for (const line of fs.readFileSync(env, 'utf8').split('\n')) {
    if (line.startsWith(`${key}=`)) return line.slice(key.length + 1).split(' #')[0].trim()
  }
  return null
}

async function main() {
  const password = envValue('GRAFANA_ADMIN_PASSWORD')
  if (!password) {
    console.error('  GRAFANA_ADMIN_PASSWORD not set in .env')
    process.exit(1)
  }
  fs.mkdirSync(SHOTS, { recursive: true })

  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } })
  const failures = []

  // Grafana logs query failures into the panel DOM, not the console, so the
  // panel-level check below is what actually catches them.
  page.on('pageerror', (e) => console.log('  page error:', e.message.split('\n')[0]))

  try {
    await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded', timeout: 30000 })
    await page.fill('input[name="user"]', 'admin')
    await page.fill('input[name="password"]', password)
    // Grafana lands on "/?orgId=1&..." after login, not /home, so match on
    // having left /login rather than on a particular destination.
    await Promise.all([
      page.waitForURL((u) => !u.pathname.includes('/login'), { timeout: 30000 }),
      page.click('button[type="submit"]'),
    ])
    console.log('  logged in')

    for (const dash of DASHBOARDS) {
      await page.goto(`${BASE}/d/${dash.slug}`, { waitUntil: 'networkidle', timeout: 60000 })
      await page.waitForSelector(PANEL, { timeout: 30000 })
      // Grafana re-runs queries on load; give the panels time to settle.
      await page.waitForTimeout(9000)

      const panels = await page.$$(PANEL)
      const heading = await page.title()

      // Grafana renders query failures and empty results as text inside the
      // panel, so read each panel's own section. A green /api/ds/query does not
      // rule out either of these: the datasource can return frames that Grafana
      // then fails to plot.
      //
      // Read the section's own textContent. There is no
      // [data-testid="panel content"] wrapper in Grafana 12, and reaching for one
      // yields an empty string -- which made the error and no-data assertions
      // pass vacuously, i.e. they asserted nothing at all.
      const inspected = await page.$$eval(PANEL, (nodes) =>
        nodes.map((n) => {
          const body = (n.textContent || '').trim()
          const text = body
          return {
            title: (n.getAttribute('data-testid') || '').replace('data-testid Panel header ', '').trim(),
            error: /query error|error loading data|failed to load/i.test(text),
            noData: /\bno data\b/i.test(text),
            body,
            // Grafana 12 tables are role="grid"/"row", not <table>/<tr>, so a
            // tbody selector silently reports zero marks for every table. The
            // canvas catches time series, whose legend is not always painted
            // by the time this samples -- without it a healthy chart reports 0.
            marks: n.querySelectorAll(
              '[role="grid"] [role="row"], [data-testid="data-testid Bar gauge value"], [data-testid^="data-testid VizLegend series"], canvas',
            ).length,
          }
        }),
      )

      await page.screenshot({ path: path.join(SHOTS, `grafana-${dash.slug}.png`), fullPage: true })

      const problems = []
      if (panels.length < dash.expect) problems.push(`only ${panels.length} panels, expected ${dash.expect}`)
      inspected
        .filter((p) => p.error)
        .forEach((p) => problems.push(`"${p.title}" shows a query error`))
      inspected
        .filter((p) => p.noData)
        .forEach((p) => problems.push(`"${p.title}" is empty`))
      if (!heading.includes(dash.title)) problems.push(`title is "${heading}"`)

      // The template variable must genuinely resolve, not merely render.
      //
      // Asserting on the dropdown's option list was the obvious approach and the
      // wrong one: Grafana renders that menu in a portal that does not open
      // reliably under automation, so the assertion tested Grafana's menu
      // internals rather than this dashboard. Asserting on the effect is both
      // stabler and stronger -- if `$__all` really expanded, the fleet-ranking
      // panel has to name all four inverters. A variable that silently failed
      // leaves a filter comparing against a literal, which either errors or
      // matches nothing, and both are caught below.
      if (dash.slug === 'solar-analysis') {
        const label = await page.$$eval(VAR_LABEL, (n) => n.map((x) => x.textContent.trim()).join(' '))
        if (!/inverter/i.test(label)) {
          problems.push(`template variable label not found (saw: ${label || 'nothing'})`)
        } else {
          const fleet = inspected.find((p) => /ranking/i.test(p.title))
          const body = fleet ? fleet.body : ''
          const named = ['INV-01', 'INV-02', 'INV-03', 'INV-04'].filter((id) => body.includes(id))
          if (named.length !== 4) {
            problems.push(
              `template variable did not expand to the full fleet — panels name only ${named.length}/4 inverters`,
            )
          } else {
            console.log(`  variable "${label}" expanded to all 4 inverters`)
          }
        }
      }

      if (problems.length) {
        failures.push(`${dash.title}: ${problems.join('; ')}`)
        console.log(`  FAIL  ${dash.title}`)
        problems.forEach((p) => console.log(`          ${p}`))
      } else {
        console.log(`  ok    ${dash.title} — ${panels.length} panels rendered`)
        inspected.forEach((p) => {
          // A stat panel renders a number, not a canvas or a grid, so "0 marks"
          // is expected there rather than a rendering failure. Show the value
          // instead, and only call a chart/table blank if it truly drew nothing.
          // The panel header contributes the title to textContent; drop it so
          // the reported value is the value.
          const value = p.body.replace(p.title, '').replace(/\s+/g, ' ').trim().slice(0, 28)
          const state = p.marks ? 'drawn' : p.body ? `value ${value || '(empty)'}` : 'NOTHING DRAWN'
          console.log(`          · ${p.title.slice(0, 50).padEnd(52)} ${state}`)
        })
      }
    }
  } catch (err) {
    failures.push(err.message)
    console.log('  FAIL  ', err.message.split('\n')[0])
  } finally {
    if (!process.argv.includes('--keep-shots')) await browser.close()
  }

  console.log(`\n  screenshots in ${path.relative(ROOT, SHOTS)}/`)
  if (failures.length) {
    console.log(`\n  ${failures.length} dashboard(s) failed`)
    process.exit(1)
  }
  console.log('  all dashboards render')
}

main()

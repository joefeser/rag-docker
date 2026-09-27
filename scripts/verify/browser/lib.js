// Shared browser-test helpers.
const puppeteer = require('puppeteer-core');

const sleep = ms => new Promise(r => setTimeout(r, ms));

function makeReporter() {
  let pass = 0, fail = 0, skip = 0;
  const failed = [];
  return {
    check(name, ok, detail = '') {
      if (ok) { pass++; console.log(`    PASS  ${name}`); }
      else { fail++; failed.push(name); console.log(`    FAIL  ${name}${detail ? '  — ' + detail : ''}`); }
    },
    skip(name, why = '') { skip++; console.log(`    SKIP  ${name}${why ? '  — ' + why : ''}`); },
    section(t) { console.log(`\n  ${t}`); },
    summary() {
      console.log(`\n  ${pass} passed, ${fail} failed, ${skip} skipped`);
      if (fail) { console.log('  failed:'); failed.forEach(f => console.log(`    - ${f}`)); }
      return fail === 0;
    },
  };
}

async function launch() {
  return puppeteer.launch({
    executablePath: '/usr/bin/chromium-browser', headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
}

// A fresh browser context per role, with console errors and API calls recorded.
// Console errors matter: a React component that throws unmounts the whole app,
// which once turned every page blank after visiting /health.
async function session(browser, base, role) {
  const ctx = await browser.createBrowserContext();
  const page = await ctx.newPage();
  const errors = [], api = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console.error: ' + m.text()); });
  page.on('request', r => {
    const u = r.url();
    if (u.includes('/api/')) api.push({ method: r.method(), url: u.replace(/^https?:\/\/[^/]+/, ''), at: Date.now() });
  });
  await page.goto(base + '/', { waitUntil: 'networkidle2' });
  if (role) await page.evaluate(r => sessionStorage.setItem('rag_role', JSON.stringify({ role: r })), role);
  return { ctx, page, errors, api };
}

const bodyText = page => page.evaluate(
  () => (document.querySelector('#root')?.innerText || '').replace(/\s+/g, ' ').trim());

// React tracks input state internally, so assigning .value is ignored. Use the
// native setter and dispatch the event React listens for.
const setValue = (page, selectorFn, value) => page.evaluate((fn, v) => {
  const el = eval(fn)();
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
              : el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
              : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
}, selectorFn, value);

const clickByText = (page, text) => page.evaluate(t => {
  const el = [...document.querySelectorAll('a,button')].find(e => (e.textContent || '').trim() === t);
  if (!el) return false; el.click(); return true;
}, text);

module.exports = { sleep, makeReporter, launch, session, bodyText, setValue, clickByText };

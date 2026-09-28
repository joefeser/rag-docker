// SPECIFICATIONS.md §10.4 — Web UI, plus the Transfer and help pages.
//
// Selectors here are deliberately precise. Loose ones have produced false
// results in both directions: a collections dropdown once matched as the
// chunking-strategy selector, `input[type=text]` missed an input with no type
// attribute, and a row's delete link matched a class test meant for the modal's
// confirm button.
const { sleep, makeReporter, launch, session, bodyText, clickByText } = require('./lib');

const BASE = process.env.RAG_UI_BASE || 'http://proxy';
const STRATEGIES = ['fixed', 'overlap', 'language', 'context_aware', 'semantic'];

(async () => {
  const browser = await launch();
  const r = makeReporter();

  // ── role persistence ───────────────────────────────────────────────────────
  r.section('§10.4 role selection');
  {
    const s = await session(browser, BASE, null);
    await s.page.goto(BASE + '/', { waitUntil: 'networkidle2' });
    await sleep(1200);
    const picked = await s.page.evaluate(() => {
      const b = [...document.querySelectorAll('button')].find(x => /Engineer/i.test(x.textContent));
      if (!b) return false; b.click(); return true;
    });
    r.check('a role can be chosen on the landing page', picked);
    const stored = await s.page.evaluate(() => sessionStorage.getItem('rag_role'));
    r.check('the choice is persisted', !!stored, String(stored));
    for (const p of ['/qa', '/collections', '/health', '/qa']) {
      await s.page.goto(BASE + p, { waitUntil: 'networkidle2' }); await sleep(700);
    }
    const after = await s.page.evaluate(() => sessionStorage.getItem('rag_role'));
    const navPresent = await s.page.evaluate(() => document.querySelectorAll('nav a').length > 0);
    r.check('the role survives navigation', after === stored && navPresent);
    await s.ctx.close();
  }

  // ── role gating ────────────────────────────────────────────────────────────
  r.section('§10.4 role gating');
  {
    const s = await session(browser, BASE, 'end_user');
    await s.page.goto(BASE + '/qa', { waitUntil: 'networkidle2' }); await sleep(1500);
    const links = await s.page.evaluate(() => [...document.querySelectorAll('nav a')].map(a => a.textContent.trim()));
    r.check('End User sees only Q&A in the nav', links.length === 1 && /Q&A/.test(links[0]), JSON.stringify(links));
    await s.page.goto(BASE + '/collections', { waitUntil: 'networkidle2' }); await sleep(1200);
    const landed = await s.page.evaluate(() => location.pathname);
    r.check('End User cannot reach a gated route directly', landed === '/qa', `landed on ${landed}`);
    await s.ctx.close();
  }
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.goto(BASE + '/qa', { waitUntil: 'networkidle2' }); await sleep(1500);
    const links = await s.page.evaluate(() => [...document.querySelectorAll('nav a')].map(a => a.textContent.trim()));
    for (const want of ['Q&A', 'Import', 'Chunking', 'Retrieval', 'Gold Standard', 'Transfer', 'Collections', 'Health']) {
      r.check(`Engineer nav includes ${want}`, links.includes(want), JSON.stringify(links));
    }
    await s.ctx.close();
  }

  // ── chunking explainer ─────────────────────────────────────────────────────
  r.section('§10.4 chunking explainer');
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.goto(BASE + '/chunking', { waitUntil: 'networkidle2' }); await sleep(2200);
    let reloads = 0; s.page.on('framenavigated', () => reloads++);
    const options = await s.page.evaluate(K => {
      const sel = [...document.querySelectorAll('select')]
        .find(x => { const v = [...x.options].map(o => o.value); return K.every(k => v.includes(k)); });
      return sel ? [...sel.options].map(o => o.value) : [];
    }, STRATEGIES);
    r.check('the strategy selector offers every strategy', options.length === STRATEGIES.length, JSON.stringify(options));
    const seen = [];
    for (const v of options) {
      await s.page.evaluate(val => {
        const sel = [...document.querySelectorAll('select')].find(x => [...x.options].some(o => o.value === val));
        Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(sel, val);
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      }, v);
      await sleep(500);
      seen.push(await bodyText(s.page));
    }
    r.check('each strategy renders a distinct explanation',
            options.length > 1 && new Set(seen).size === options.length, `${new Set(seen).size} distinct`);
    r.check('no page reload occurs', reloads === 0, `${reloads} navigations`);
    await s.ctx.close();
  }

  // ── delete confirmation ────────────────────────────────────────────────────
  r.section('§10.4 delete confirmation');
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.goto(BASE + '/collections', { waitUntil: 'networkidle2' }); await sleep(2200);
    const opened = await s.page.evaluate(() => {
      const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'Delete');
      if (!b) return false; b.click(); return true;
    });
    if (!opened) {
      r.skip('delete confirmation', 'no collection present to delete');
    } else {
      await sleep(700);
      const state = await s.page.evaluate(() => {
        const inputs = [...document.querySelectorAll('input')].filter(i => !i.type || i.type === 'text');
        // the modal's confirm button is the solid red one; the row link is not
        const btn = [...document.querySelectorAll('button')]
          .find(b => b.textContent.trim() === 'Delete' && b.className.includes('bg-red-600'));
        return { inputs: inputs.length, disabled: btn ? btn.disabled : null };
      });
      r.check('the modal asks for the name to be typed', state.inputs > 0, JSON.stringify(state));
      r.check('confirm is disabled until it matches', state.disabled === true, JSON.stringify(state));
      const before = s.api.filter(x => x.method === 'DELETE').length;
      await s.page.evaluate(() => {
        const el = [...document.querySelectorAll('input')].find(i => !i.type || i.type === 'text');
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, 'not-the-name');
        el.dispatchEvent(new Event('input', { bubbles: true }));
        const btn = [...document.querySelectorAll('button')]
          .find(b => b.textContent.trim() === 'Delete' && b.className.includes('bg-red-600'));
        if (btn && !btn.disabled) btn.click();
      });
      await sleep(900);
      r.check('a wrong name sends no DELETE',
              s.api.filter(x => x.method === 'DELETE').length === before);
    }
    await s.ctx.close();
  }

  // ── upload size limit ──────────────────────────────────────────────────────
  // The Import page refuses a selection over the proxy's limit before sending.
  // The file is sparse: it reports 513 MB but occupies no disk, and the check
  // must stop it before the browser ever reads it. See issue #21.
  r.section('upload size limit');
  {
    const fs = require('fs');
    const big = '/tmp/vfy-oversize-upload.txt';
    fs.closeSync(fs.openSync(big, 'w'));
    fs.truncateSync(big, 513 * 1024 * 1024);
    const s = await session(browser, BASE, 'developer');
    await s.page.goto(BASE + '/import', { waitUntil: 'networkidle2' }); await sleep(1500);
    const hint = await bodyText(s.page);
    r.check('the drop zone states the upload limit', hint.includes('up to 512 MB per upload'));
    const input = await s.page.$('#file-input');
    await input.uploadFile(big);
    await sleep(500);
    const posts = () => s.api.filter(x => x.method === 'POST' && x.url.includes('/ingest/upload')).length;
    const before = posts();
    const clicked = await clickByText(s.page, 'Start Ingest');
    await sleep(900);
    const text = await bodyText(s.page);
    r.check('an oversize selection is refused with the limit named',
            clicked && text.includes('one upload can be at most 512 MB'),
            clicked ? text.slice(0, 160) : 'Start Ingest button not found');
    r.check('an oversize selection sends no upload', posts() === before);
    r.check('no console errors on the import page', s.errors.length === 0, s.errors.slice(0, 2).join(' | '));
    fs.unlinkSync(big);
    await s.ctx.close();
  }

  // ── health dashboard ───────────────────────────────────────────────────────
  r.section('§10.4 health dashboard');
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.goto(BASE + '/health', { waitUntil: 'networkidle2' }); await sleep(2500);
    const body = await bodyText(s.page);
    const latencies = body.match(/\d+\s*ms/g) || [];
    r.check('per-service latency is shown', latencies.length >= 3, latencies.slice(0, 5).join(' '));
    // Services are labelled by role and model, not by the word "Ollama".
    for (const want of ['Weaviate', 'LLM', 'Embed']) {
      r.check(`the dashboard names ${want}`, new RegExp(want, 'i').test(body));
    }
    if (process.env.RAG_SKIP_SLOW === '1') {
      r.skip('30s auto-refresh', 'needs a 70s observation window');
    } else {
      const t0 = Date.now(); s.api.length = 0;
      await sleep(70000);
      const hits = s.api.filter(x => x.url.includes('/health')).map(x => Math.round((x.at - t0) / 1000));
      const gaps = hits.slice(1).map((v, i) => v - hits[i]);
      r.check('the dashboard refreshes on its own', hits.length >= 2, `polled at t+${hits.join('s, t+')}s`);
      r.check('the interval is about 30s', gaps.length > 0 && gaps.every(g => g >= 25 && g <= 35), `gaps: ${gaps.join(', ')}s`);
    }
    r.check('no console errors on the health page', s.errors.length === 0, s.errors.slice(0, 2).join(' | '));
    await s.ctx.close();
  }

  // Imported identities are exposed through the existing visible job notes.
  r.section('imported session lookup IDs');
  {
    const s = await session(browser, BASE, 'engineer');
    const filename = 'ragpkg-owned-session.tar.gz';
    const sourceId = 'gs_460abcde', localId = 'gs_460abcdf';
    const submissions = [];
    await s.page.setRequestInterception(true);
    s.page.on('request', request => {
      const path = new URL(request.url()).pathname;
      let body;
      if (path === '/api/packages') body = { packages: [{ filename, size_bytes: 100, collection: 'OwnedOriginal', chunk_count: 1, fidelity: 'chunks-only', created_at: '2026-09-28T00:00:00Z', readable: true }] };
      else if (path === '/api/import' && request.method() === 'POST') {
        submissions.push(JSON.parse(request.postData()));
        return request.respond({ status: 202, contentType: 'application/json', body: JSON.stringify({ job_id: 'owned-identity-job', status: 'queued', filename }) });
      } else if (path === '/api/import/job/owned-identity-job') body = { job_id: 'owned-identity-job', status: 'completed', filename, on_conflict: 'rename', collection: 'OwnedImported', original_collection: 'OwnedOriginal', chunks_written: 1, fidelity: 'chunks-only', renamed: true, notes: ["evaluation session '" + sourceId + "' restored as local '" + localId + "' for 'OwnedImported'"], restored_sessions: [{ source_session_id: sourceId, session_id: localId, collection: 'OwnedImported' }], error: null, error_code: null, error_detail: null };
      if (body) return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
      return request.continue();
    });
    try {
      await s.page.goto(BASE + '/transfer', { waitUntil: 'networkidle2' }); await sleep(250);
      await s.page.evaluate(value => {
        const selector = [...document.querySelectorAll('select')].find(el => [...el.options].some(option => option.value === value));
        Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(selector, value);
        selector.dispatchEvent(new Event('change', { bubbles: true }));
        document.querySelector('input[name="conflict"][value="rename"]').click();
      }, filename);
      await clickByText(s.page, 'Import'); await sleep(600);
      const text = await bodyText(s.page);
      r.check('rename import submits selected package and explicit policy', submissions.length === 1 && submissions[0].filename === filename && submissions[0].on_conflict === 'rename');
      r.check('completed import displays original and allocated session IDs for lookup', text.includes(sourceId) && text.includes(localId) && text.includes('OwnedImported'));
      r.check('import identity notes do not cause React page errors', !s.errors.some(error => error.startsWith('pageerror:')));
    } finally { await s.ctx.close(); }
  }

  // ── transfer help page ─────────────────────────────────────────────────────
  r.section('transfer help page');
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.goto(BASE + '/help/transfer', { waitUntil: 'networkidle2' }); await sleep(2500);
    const info = await s.page.evaluate(() => {
      const h1 = document.querySelector('h1');
      const p = document.querySelector('article p');
      return {
        chars: (document.querySelector('#root')?.innerText || '').length,
        headings: [...document.querySelectorAll('h2')].map(e => e.textContent.trim()),
        tables: document.querySelectorAll('table').length,
        h1Size: h1 ? parseFloat(getComputedStyle(h1).fontSize) : 0,
        pSize: p ? parseFloat(getComputedStyle(p).fontSize) : 0,
      };
    });
    r.check('the help page renders substantive content', info.chars > 3000, `${info.chars} chars`);
    r.check('markdown tables render', info.tables >= 3, `${info.tables} tables`);
    r.check('headings are styled (typography plugin present)', info.h1Size > info.pSize,
            `h1=${info.h1Size}px p=${info.pSize}px`);
    for (const want of ['Where packages live', 'Naming', 'What a package contains',
                        'Fidelity', 'embedding model rule', 'name collision', 'Tuning after import']) {
      r.check(`§10 topic covered: ${want}`,
              info.headings.some(h => h.toLowerCase().includes(want.toLowerCase())),
              info.headings.join(' | ').slice(0, 80));
    }
    r.check('no console errors on the help page', s.errors.length === 0, s.errors.slice(0, 2).join(' | '));
    await s.ctx.close();
  }

  await browser.close();
  process.exit(r.summary() ? 0 : 1);
})().catch(e => { console.log('  HARNESS FAILURE: ' + e.message); process.exit(2); });

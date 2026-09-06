// jsdom-driven runtime checks for llemon_video/video.html's Segmind
// start-image picker + data-handling-warning consent UI
// (see specs/mediagen-video-user-interface-spec.md, "Segmind image-to-video
// integration"). Invoked as
// `node video_dom_test.js <path-to-rendered-html>` by
// ../test_llemon_video_dom.py, which renders the page against a fixture
// set of Segmind models covering every scenario this file exercises (see
// that file's model fixtures for the exact ids referenced below).
//
// Mirrors edit_images_dom_test.js's structure and the two runtime
// properties documented there: a runtime exception thrown inside an event
// listener does not propagate out of dispatchEvent(), so every step checks
// for newly accumulated window errors after it runs; and this page's
// submit handler is an `async function` invoked from a synchronous
// dispatchEvent(), so submitting waits one macrotask tick for the pending
// fetch/json microtasks to drain before the caller inspects state.
//
// Exits 0 with one "OK <name>" line per passing step, or exits 1 with at
// least one "FAIL <name> -> ..." line naming what broke.

const fs = require('fs');
const { JSDOM } = require('jsdom');

const htmlPath = process.argv[2];
if (!htmlPath) {
  console.error('usage: node video_dom_test.js <path-to-rendered-html>');
  process.exit(2);
}
const html = fs.readFileSync(htmlPath, 'utf-8');

function defaultFetchMock() {
  return Promise.resolve({
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve({
      ok: true, file: 'out.mp4', files: ['out.mp4'],
      url: '/file/out.mp4', gallery_url: '/gallery/', meta: {}, summary: [],
    }),
    status: 200,
  });
}

function makeDom(url) {
  const windowErrors = [];
  const state = { lastFetch: null, fetchImpl: defaultFetchMock };
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    resources: 'usable',
    url: url || 'http://localhost/llemon/media/video-creator/',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.fetch = function (url, opts) {
        state.lastFetch = { url, opts };
        return state.fetchImpl(url, opts);
      };
      window.onerror = function (msg, src, line, col) {
        windowErrors.push('onerror: ' + String(msg) + (line ? ' @' + line + ':' + (col || '?') : ''));
      };
      window.addEventListener('unhandledrejection', function (event) {
        const reason = event && event.reason;
        const text = reason && (reason.stack || reason.message) ? (reason.stack || reason.message) : reason;
        windowErrors.push('unhandledrejection: ' + text);
      });
    },
  });
  return { dom, windowErrors, state };
}

function sleep(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
}

let failures = 0;

async function step(name, fn, ctx) {
  let stepError = null;
  try {
    await fn();
  } catch (e) {
    stepError = e;
  }
  const newErrors = ctx.windowErrors.slice(ctx.checkedErrorsUpTo || 0);
  ctx.checkedErrorsUpTo = ctx.windowErrors.length;
  if (stepError) {
    failures += 1;
    console.log('FAIL', name, '->', (stepError && stepError.stack) || stepError);
    return;
  }
  if (newErrors.length) {
    failures += 1;
    console.log('FAIL', name, '-> runtime error(s) surfaced during this step:', newErrors.join(' | '));
    return;
  }
  console.log('OK  ', name);
}

function fire(win, el, type) {
  el.dispatchEvent(new win.Event(type, { bubbles: true }));
}

// Checks the resolved (stylesheet + inline) display, not just the inline
// style attribute -- a handler that clears its own inline override (e.g.
// style.display = '') can silently fall back to a stylesheet default of
// display:none, which an inline-only check would miss entirely.
function isHidden(win, el) {
  return win.getComputedStyle(el).display === 'none';
}

async function main() {
  const { dom, windowErrors, state } = makeDom();
  const { window } = dom;
  const doc = window.document;
  const ctx = { windowErrors, checkedErrorsUpTo: 0 };

  async function selectModel(modelId) {
    // Unlike image.html's edit-model-sel (a plain synchronous change
    // handler), video's model selection funnels through the shared async
    // createMediaRefreshController (media_creator.js): its cached path
    // still resolves via an async function's microtask, and an
    // as-yet-uncached model additionally awaits controller.load() -- both
    // apply their UI effects (updateImageChoiceVisibility(),
    // refreshSegmindConsent(), loadNote()'s own async fetch) after the
    // synchronous 'change' dispatch returns, not during it. A caller that
    // inspects DOM state immediately after firing 'change' would see the
    // *previous* model's state, and worse, an in-flight loadNote() fetch
    // from a still-resolving earlier switch can race with and clobber
    // window.fetch's single recorded call -- exactly the bug this helper
    // exists to avoid.
    const sel = doc.getElementById('model');
    sel.value = modelId;
    fire(window, sel, 'change');
    await sleep(10);
  }

  function pickerThumbs() {
    return Array.from(doc.querySelectorAll('#image-picker-grid .image-thumb-wrap'));
  }

  function pickStartImage(index) {
    fire(window, doc.getElementById('start-image-btn'), 'click');
    fire(window, pickerThumbs()[index], 'click');
  }

  async function submitGenerate(prompt) {
    state.lastFetch = null;
    doc.getElementById('prompt').value = prompt || 'a video';
    fire(window, doc.getElementById('videogen-form'), 'submit');
    // See the file header: let the still-pending fetch/json microtask
    // chain drain before returning control to the caller.
    await sleep(0);
  }

  await sleep(50); // let the page's own <script> blocks finish setting up

  await step('page loads and runs with no window errors', function () {}, ctx);

  await step('warned+available model: picker enabled, consent row hidden until an image is picked', async function () {
    await selectModel('wan-warned');
    if (doc.getElementById('start-image-row').style.display === 'none') {
      throw new Error('start-image-row should be visible');
    }
    if (doc.getElementById('start-image-btn').disabled) {
      throw new Error('start-image-btn should not be disabled for an available transport');
    }
    if (doc.getElementById('start-image-disabled-note').style.display !== 'none') {
      throw new Error('disabled-note should be hidden when the picker is enabled');
    }
    if (doc.getElementById('segmind-consent-row').style.display !== 'none') {
      throw new Error('consent row should be hidden before any image is picked');
    }
  }, ctx);

  await step('a model with a known caveat shows the caveat notice verbatim', function () {
    const notice = doc.getElementById('model-caveat-notice');
    if (isHidden(window, notice)) throw new Error('caveat notice should be visible for wan-warned');
    if (notice.textContent !== (
      'This model has been observed to ignore the requested aspect '
      + 'ratio in image-to-video mode and return square (1:1) output '
      + 'instead. This is Segmind provider behavior, not a Grove bug.'
    )) {
      throw new Error('unexpected caveat text: ' + notice.textContent);
    }
  }, ctx);

  await step('picking a gallery image shows the consent row with the verbatim warning', function () {
    pickStartImage(0);
    const row = doc.getElementById('segmind-consent-row');
    if (row.style.display === 'none') throw new Error('consent row should now be visible');
    const msg = doc.getElementById('segmind-consent-message').textContent;
    if (msg !== 'Uploads your image to Segmind for hosting.') {
      throw new Error('unexpected warning text: ' + msg);
    }
    if (doc.getElementById('segmind-consent-checkbox').checked) {
      throw new Error('checkbox should start unchecked');
    }
    if (!doc.getElementById('generate-btn').disabled) {
      throw new Error('generate button should be disabled while warned-and-unchecked');
    }
  }, ctx);

  await step('checking the consent box enables the generate button', function () {
    const cb = doc.getElementById('segmind-consent-checkbox');
    cb.checked = true;
    fire(window, cb, 'change');
    if (doc.getElementById('generate-btn').disabled) {
      throw new Error('generate button should be enabled once consent is checked');
    }
  }, ctx);

  await step('submitting sends the picked image and consent flag', async function () {
    await submitGenerate('go');
    if (!state.lastFetch) throw new Error('fetch was not called');
    const body = JSON.parse(state.lastFetch.opts.body);
    if (!body.image_url) throw new Error('image_url missing from submitted body');
    if (body.accept_data_handling_warnings !== true) {
      throw new Error('accept_data_handling_warnings should be true, got: ' + body.accept_data_handling_warnings);
    }
  }, ctx);

  await step(
    'toggling consent mid-request does not re-enable Generate before the request finishes',
    async function () {
      // Regression test: updateGenerateButtonAvailability() used to derive
      // the button's disabled state from consent alone, so an in-flight
      // request's own btn.disabled = true got clobbered the moment consent
      // was (re-)satisfied while waiting on the response -- allowing a
      // duplicate submission before the first one completed.
      const btn = doc.getElementById('generate-btn');
      const cb = doc.getElementById('segmind-consent-checkbox');
      let resolveFetch;
      state.fetchImpl = function () {
        return new Promise(function (resolve) { resolveFetch = resolve; });
      };
      doc.getElementById('prompt').value = 'go slowly';
      fire(window, doc.getElementById('videogen-form'), 'submit');
      await sleep(0);
      if (!btn.disabled) throw new Error('button should be disabled once generation starts');
      cb.checked = false;
      fire(window, cb, 'change');
      cb.checked = true;
      fire(window, cb, 'change');
      if (!btn.disabled) {
        throw new Error(
          'button should stay disabled while a request is in flight, even if '
          + 'consent toggles back to satisfied in the meantime',
        );
      }
      resolveFetch({
        headers: { get: () => 'application/json' },
        json: () => Promise.resolve({
          ok: true, file: 'out2.mp4', files: ['out2.mp4'],
          url: '/file/out2.mp4', gallery_url: '/gallery/', meta: {}, summary: [],
        }),
        status: 200,
      });
      await sleep(0);
      if (btn.disabled) throw new Error('button should re-enable once the request finishes');
      state.fetchImpl = defaultFetchMock;
    },
    ctx,
  );

  await step('clearing the start image hides the consent row and resets the checkbox', function () {
    fire(window, doc.getElementById('start-image-clear-btn'), 'click');
    if (doc.getElementById('segmind-consent-row').style.display !== 'none') {
      throw new Error('consent row should hide once the start image is cleared');
    }
    if (doc.getElementById('segmind-consent-checkbox').checked) {
      throw new Error('checkbox should reset once the start image is cleared');
    }
    if (doc.getElementById('generate-btn').disabled) {
      throw new Error('generate button should be enabled again with no image selected');
    }
  }, ctx);

  await step('an unwarned model needs no consent even with an image picked', async function () {
    await selectModel('wan-unwarned');
    if (!isHidden(window, doc.getElementById('model-caveat-notice'))) {
      throw new Error('caveat notice should be hidden for a model with no known_caveat');
    }
    pickStartImage(1);
    if (doc.getElementById('segmind-consent-row').style.display !== 'none') {
      throw new Error('consent row should stay hidden for an unwarned model');
    }
    if (doc.getElementById('generate-btn').disabled) {
      throw new Error('generate button should stay enabled for an unwarned model');
    }
    await submitGenerate('go again');
    if (!state.lastFetch) throw new Error('fetch was not called');
    const body = JSON.parse(state.lastFetch.opts.body);
    if (body.accept_data_handling_warnings !== false) {
      throw new Error('accept_data_handling_warnings should be sent as false, got: '
        + body.accept_data_handling_warnings);
    }
  }, ctx);

  await step('switching models resets a previously-checked consent checkbox', async function () {
    await selectModel('wan-warned');
    if (isHidden(window, doc.getElementById('model-caveat-notice'))) {
      throw new Error('caveat notice should reappear when switching back to wan-warned');
    }
    pickStartImage(0);
    const cb = doc.getElementById('segmind-consent-checkbox');
    cb.checked = true;
    fire(window, cb, 'change');
    if (!cb.checked) throw new Error('setup: checkbox should be checked before the switch');
    await selectModel('wan-warned'); // reselecting the same model must not itself reset
    if (!cb.checked) throw new Error('reselecting the same model must not clear consent');
  }, ctx);

  await step('a declared-but-unavailable transport disables the picker with a reason', async function () {
    await selectModel('wan-unavailable');
    if (doc.getElementById('start-image-row').style.display === 'none') {
      throw new Error('start-image-row should still be visible (allows_start_image is true)');
    }
    if (!doc.getElementById('start-image-btn').disabled) {
      throw new Error('start-image-btn should be disabled when the transport is unavailable');
    }
    const note = doc.getElementById('start-image-disabled-note');
    if (note.style.display === 'none' || !note.textContent) {
      throw new Error('a disabled picker must show an explanatory reason');
    }
  }, ctx);

  await step(
    'missing transport metadata entirely fails closed (disabled, not enabled)',
    async function () {
      // The fail-closed requirement from the design review: a model whose
      // presentation carries no required_backend_transports/
      // available_backend_transports keys at all -- not merely empty ones
      // -- must render exactly like a declared-unavailable transport, never
      // default to an enabled picker just because nothing said "no".
      await selectModel('wan-missing-metadata');
      if (!doc.getElementById('start-image-btn').disabled) {
        throw new Error('picker must be disabled when transport metadata is entirely absent');
      }
      const note = doc.getElementById('start-image-disabled-note');
      if (note.style.display === 'none' || !note.textContent) {
        throw new Error('a disabled picker must show an explanatory reason even with no metadata');
      }
    },
    ctx,
  );

  await step('a model with no start-image support hides the row entirely', async function () {
    await selectModel('t2v-only');
    if (doc.getElementById('start-image-row').style.display !== 'none') {
      throw new Error('start-image-row should be hidden when allows_start_image is false');
    }
    if (doc.getElementById('start-image-disabled-note').style.display !== 'none') {
      throw new Error('disabled-note should not show when the row itself is hidden');
    }
    if (!isHidden(window, doc.getElementById('model-caveat-notice'))) {
      throw new Error('caveat notice should be hidden for a model with no known_caveat');
    }
  }, ctx);

  await step(
    'submitting while warned-and-unchecked is blocked client-side with no request sent',
    async function () {
      await selectModel('wan-warned');
      pickStartImage(0);
      state.lastFetch = null;
      await submitGenerate('should not go');
      if (state.lastFetch) throw new Error('fetch should not have been called while unchecked');
      if (!/data-handling warning/.test(doc.getElementById('status').textContent)) {
        throw new Error('expected an inline warning about the unchecked consent, got: '
          + doc.getElementById('status').textContent);
      }
    },
    ctx,
  );

  // -- URL-prefill: a fresh page load with ?image_url=... must reach the
  // shared branch (Segmind is no longer special-cased) and trigger the
  // same consent refresh as an interactive pick. Needs its own JSDOM
  // instance since the query string is fixed at construction time.
  const prefillCtx = { windowErrors: [], checkedErrorsUpTo: 0 };
  const prefillFixture = makeDom(
    'http://localhost/llemon/media/video-creator/?image_url=%2Fimg%2Fcat.png',
  );
  prefillCtx.windowErrors = prefillFixture.windowErrors;
  await sleep(50);
  await step('URL-prefilled image_url populates the picker and triggers consent refresh', function () {
    const pdoc = prefillFixture.dom.window.document;
    const label = pdoc.getElementById('start-image-label').textContent;
    if (!label) throw new Error('start image label was not populated from ?image_url=');
    if (pdoc.getElementById('segmind-consent-row').style.display === 'none') {
      throw new Error('consent row should appear for the prefilled image on this warned model');
    }
  }, prefillCtx);

  console.log(failures ? 'DONE (' + failures + ' failure(s))' : 'DONE');
  process.exitCode = failures ? 1 : 0;
}

main().catch(function (err) {
  console.error('harness crashed:', (err && err.stack) || err);
  process.exitCode = 1;
});

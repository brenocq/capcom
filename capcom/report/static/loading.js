// Progressive loading: fetching a reply that the server computes as a job (202
// with progress and the reply so far, then 200; capcom/report/API.md,
// "Progressive replies"), and the number tween the pages use while it fills.
// No DOM here except in `tweenText`, so node can test the helpers.

export const POLL_MS = 400;
export const TWEEN_MS = 400;

export const easeOut = (t) => 1 - Math.pow(1 - Math.min(1, Math.max(0, t)), 3);

// The numbers capcom prints: "$1,189", "$568.80", "≥ $3.5k", "62%", "1.2M", "172".
// Parsed into a prefix, the value, its decimals and a suffix, so a tween can
// print the values between in the same format.
const NUM = /^(\D*?)(-?)(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(\s?(?:%|k|M|B|ms|s|min|h)?)$/;

export function parseNumber(text) {
  if (typeof text !== 'string') return null;
  const m = NUM.exec(text.trim());
  if (!m) return null;
  const grouped = m[3].includes(',');
  const value = Number((m[2] + m[3].replace(/,/g, '')) + (m[4] ? '.' + m[4] : ''));
  if (!Number.isFinite(value)) return null;
  return { prefix: m[1], value, decimals: m[4] ? m[4].length : 0, grouped, suffix: m[5] };
}

export function formatLike(p, value) {
  const fixed = Math.abs(value).toFixed(p.decimals);
  const [int, frac] = fixed.split('.');
  const body = p.grouped ? Number(int).toLocaleString('en-US') : int;
  return p.prefix + (value < 0 ? '-' : '') + body + (frac ? '.' + frac : '') + p.suffix;
}

// The text between `from` and `to` at time fraction t (0..1, eased), or null when
// the two are not the same kind of number (then the caller swaps them).
export function tweenStep(from, to, t) {
  const a = parseNumber(from), b = parseNumber(to);
  if (!a || !b || a.prefix !== b.prefix || a.suffix !== b.suffix) return null;
  if (t >= 1) return to;
  const v = a.value + (b.value - a.value) * easeOut(t);
  return formatLike({ ...b, grouped: a.grouped || b.grouped }, v);
}

export const reducedMotion = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;

// Tween an element's text from what it shows to `to` over TWEEN_MS with
// requestAnimationFrame; a swap without motion when reduced motion is asked for
// or the texts are not numbers alike.
export function tweenText(el, to, ms = TWEEN_MS) {
  const from = el.textContent;
  if (from === to) return;
  if (reducedMotion() || tweenStep(from, to, 0) === null) { el.textContent = to; return; }
  const t0 = performance.now();
  const id = (el.__tween || 0) + 1;
  el.__tween = id;
  const frame = (now) => {
    if (el.__tween !== id) return; // a newer tween took over
    const t = (now - t0) / ms;
    el.textContent = tweenStep(from, to, t);
    if (t < 1) requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

const wait = (ms, signal) => new Promise((resolve, reject) => {
  const id = setTimeout(resolve, ms);
  if (signal) signal.addEventListener('abort', () => { clearTimeout(id); reject(signal.reason); }, { once: true });
});

// GET /api/<path>, following a job to its end: `onPartial(partial, progress,
// source)` for each 202 (partial may be null), the final body returned. Throws
// {status, body} for an error reply, and the abort reason when `signal` aborts.
export async function getProgressive(path, { signal, onPartial, fetchFn = fetch, pollMs = POLL_MS } = {}) {
  let url = '/api/' + path;
  for (;;) {
    const r = await fetchFn(url, { headers: { Accept: 'application/json' }, signal });
    let body = null;
    try { body = await r.json(); } catch (e) { body = null; }
    if (r.status !== 202) {
      if (!r.ok) throw Object.assign(new Error(body && body.message ? body.message : `HTTP ${r.status}`), { status: r.status, body: body || {} });
      return body;
    }
    if (onPartial) onPartial(body.partial, body.progress, body.source);
    await wait(pollMs, signal);
    url = '/api/' + path + (path.includes('?') ? '&' : '?') + 'job=' + encodeURIComponent(body.job);
  }
}

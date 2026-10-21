// Did it work?: the parts shared by Checks and the change pages. The one write
// (POST /api/changes, with this launch's token from the page's meta tag), the
// verdict chips, the "Mark as applied" form, the "capcom noticed" banner and the
// summary card. A note is the user's own text: always set with textContent.

import { h, s, usd, monthDay, localDate, forget, ApiError } from './lib.js';

const meta = (name) => { const m = document.querySelector(`meta[name="${name}"]`); return m ? m.getAttribute('content') : null; };

// `capcom serve --read-only`: no button that writes
export const READ_ONLY = meta('capcom-read-only') === '1';

export async function postChange(body) {
  const r = await fetch('/api/changes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-Capcom-Token': meta('capcom-token') || '' },
    body: JSON.stringify(body),
  });
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) throw new ApiError(r.status, data);
  forget(); // every kept reply may show the mark: fetch again
  window.dispatchEvent(new Event('capcom-changed'));
  return data;
}

// ------------------------------------------------------------------ dates

export const isoOf = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
export function shiftDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
export function todayIso() { return isoOf(new Date()); }

// "today", "yesterday", "Oct 22"
export function dayWord(iso) {
  if (iso === todayIso()) return 'today';
  if (iso === isoOf(shiftDays(new Date(), -1))) return 'yesterday';
  return monthDay(localDate(iso));
}

export function agoText(days) {
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  return `${days} days ago`;
}

// ------------------------------------------------------------------ verdicts

const CLOCK = [['circle', { cx: 12, cy: 12, r: 9 }], ['path', { d: 'M12 7v5l3 2' }]];
export const VERDICTS = {
  working: { label: 'Working', cls: 'good', icon: [['path', { d: 'M5 12.5l4.5 4.5L19 7.5' }]] },
  no_effect: { label: 'No clear effect', cls: 'meh', icon: [['path', { d: 'M6 12h12' }]] },
  worse: { label: 'Worse', cls: 'bad', icon: [['path', { d: 'M12 19V6M6 11l6-6 6 6' }]] },
  too_early: { label: 'Too early', cls: 'wait', icon: CLOCK },
  no_data: { label: 'No data', cls: 'wait', icon: [['circle', { cx: 12, cy: 12, r: 9 }], ['path', { d: 'M8 12h8' }]] },
};
export const VERDICT_ORDER = ['working', 'no_effect', 'worse', 'too_early', 'no_data'];

export function verdictIcon(v, size = 13) {
  return s('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2.6, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' },
    (VERDICTS[v] || VERDICTS.no_data).icon.map(([tag, attrs]) => s(tag, attrs)));
}

// A verdict as a chip: icon and label, never color alone. `text` replaces the label.
export function verdictChip(v, text, opts = {}) {
  const info = VERDICTS[v] || VERDICTS.no_data;
  const tag = opts.href ? 'a' : 'span';
  return h(`${tag}.vchip.${info.cls}${opts.big ? '.big' : ''}`, { href: opts.href || null, title: opts.title || null },
    verdictIcon(v, opts.big ? 16 : 13), text || info.label);
}

// Money a change saves, as the pages say it: a saving is a drop in spend ("−$124").
export function savingText(x, digits) {
  if (x === null || x === undefined) return '—';
  const d = digits ?? (Math.abs(x) >= 10 ? 0 : 2);
  if (Math.abs(x) < 0.005) return usd(0, d);
  return (x > 0 ? '−' : '+') + usd(Math.abs(x), d);
}

// A rate as the measure reads: dollars when it is in dollars, else a number.
export function rateText(x, measure) {
  if (x === null || x === undefined) return '—';
  if (/dollar|\$/i.test(measure || '')) return usd(x, x >= 10 ? 0 : 2);
  const a = Math.abs(x);
  if (a >= 1000) return Math.round(x).toLocaleString('en-US');
  if (a >= 100) return x.toFixed(0);
  if (a >= 1) return x.toFixed(1);
  return x.toFixed(2);
}

// The chip of a marked finding on Checks: its verdict when known, linked to its page.
export function markedChip(mark, result) {
  const href = `/changes/${mark.id}`;
  if (!result || result.verdict === 'too_early') {
    return verdictChip('too_early', `Applied ${dayWord(mark.applied_on)}, being measured`, { href, title: 'Open the measure of this change' });
  }
  return verdictChip(result.verdict, null, { href, title: `Applied ${dayWord(mark.applied_on)}. Open the measure of this change` });
}

// ------------------------------------------------------------------ the form

// The first sentence of the finding's advice, short enough for a radio label.
function shortAdvice(text) {
  const first = (text || '').split(/(?<=\.)\s/)[0].replace(/\.$/, '');
  return first.length > 110 ? first.slice(0, 107) + '…' : first;
}

// "Mark as applied", inline under a finding: when, what, a note, the measure it
// will be judged by. `f`: a /api/checks finding; `days`: the Checks window.
export function markForm(f, days, onCancel) {
  const today = new Date();
  const choices = [
    [isoOf(today), `Today, ${monthDay(today)}`],
    [isoOf(shiftDays(today, -1)), 'Yesterday'],
    ['pick', 'Pick a date'],
  ];
  let when = choices[0][0];
  const picker = h('input.dateinput', { type: 'date', max: isoOf(today), value: isoOf(shiftDays(today, -2)), 'aria-label': 'The day you made the change', hidden: true });
  const whenId = `when-${f.n}`;
  const whenGroup = h('div.pick', { role: 'group', 'aria-labelledby': whenId });
  const drawWhen = () => {
    whenGroup.replaceChildren(...choices.map(([v, label]) => h('button.opt', {
      type: 'button', 'aria-pressed': String(v === when),
      onclick: () => { when = v; picker.hidden = v !== 'pick'; drawWhen(); if (v === 'pick') picker.focus(); },
    }, label)), picker);
  };
  drawWhen();
  const name = `what-${f.n}`;
  const radio = (value, checked, label) => h('label.radio', null,
    h('input', { type: 'radio', name, value, checked: checked || null }), label);
  const advice = shortAdvice(f.advice);
  const what = h('fieldset.what', null, h('legend', { text: 'What did you change?' }),
    radio('suggested', true, advice ? `The suggested change: ${advice}` : 'The suggested change'),
    radio('other', false, 'Something else'));
  const noteId = `note-${f.n}`;
  const note = h('input.textinput', { id: noteId, type: 'text', maxlength: 500, autocomplete: 'off' });
  const scope = f.scope === 'all' ? 'every project' : (f.scope_label || f.scope);
  const measure = f.measure ? f.measure.charAt(0).toLowerCase() + f.measure.slice(1) : 'this finding’s own rate';
  const err = h('p.formerr', { role: 'alert', hidden: true });
  const submit = h('button.btn.primary', { type: 'submit', text: 'Mark as applied' });
  const form = h('form.markform', { 'aria-label': `Mark “${f.title}” as applied` },
    h('div.formrow', null,
      h('div.field', null, h('span.flabel', { id: whenId, text: 'When did you make it?' }), whenGroup),
      what),
    h('div.field', null,
      h('label.flabel', { for: noteId }, 'Note ', h('span.note', { text: '(optional, stays on this machine)' })),
      note),
    h('p.measureline', null, 'capcom will compare ', h('b', { text: `${measure} in ${scope}` }),
      ' over the 4 weeks before with the days after, so a busier week doesn’t count against you. A verdict needs 7 days and enough requests.'),
    err,
    h('div.btnrow', null, submit, h('button.btn', { type: 'button', text: 'Cancel', onclick: onCancel })));
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const applied = when === 'pick' ? picker.value : when;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(applied || '')) { err.textContent = 'Pick the day you made the change.'; err.hidden = false; return; }
    const picked = form.querySelector(`input[name="${name}"]:checked`);
    submit.disabled = true;
    err.hidden = true;
    try {
      await postChange({ action: 'mark', kind: f.kind, scope: f.scope, applied_on: applied, what: picked ? picked.value : 'suggested', note: note.value.trim(), days });
    } catch (ex) {
      err.textContent = 'Not saved: ' + (ex.message || String(ex));
      err.hidden = false;
      submit.disabled = false;
    }
  });
  return form;
}

// ------------------------------------------------------------------ noticed

const DISMISSED = 'capcom-not-this';

function dismissed() {
  try { return new Set(JSON.parse(localStorage.getItem(DISMISSED) || '[]')); } catch (e) { return new Set(); }
}

function dismiss(key) {
  try {
    const all = [...dismissed(), key].slice(-200);
    localStorage.setItem(DISMISSED, JSON.stringify(all));
  } catch (e) { /* storage off: it shows again next time */ }
}

const EYE = 'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z';

// "capcom noticed a change": one banner per change it saw that answers a finding,
// unless "Not this" was picked for it in this browser.
export function noticedBanners(items, days) {
  if (READ_ONLY) return [];
  const gone = dismissed();
  return items.filter((n) => !gone.has(`${n.finding_id}|${n.since}`)).map((n) => {
    const key = `${n.finding_id}|${n.since}`;
    const since = new Date(n.since);
    const sinceDay = isoOf(since);
    const err = h('p.formerr', { role: 'alert', hidden: true });
    const box = h('section.card.noticed', { 'aria-label': 'capcom noticed a change' });
    const scope = n.scope === 'all' ? 'every project' : n.scope_label;
    const markBtn = h('button.btn.primary', { type: 'button', text: 'Mark as applied' });
    markBtn.addEventListener('click', async () => {
      markBtn.disabled = true;
      try {
        await postChange({ action: 'mark', kind: n.kind, scope: n.scope, applied_on: sinceDay, source: 'noticed', days });
      } catch (ex) {
        err.textContent = 'Not saved: ' + (ex.message || String(ex));
        err.hidden = false;
        markBtn.disabled = false;
      }
    });
    box.append(
      s('svg', { width: 20, height: 20, viewBox: '0 0 24 24', fill: 'none', stroke: 'var(--s1)', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' },
        s('path', { d: EYE }), s('circle', { cx: 12, cy: 12, r: 3 })),
      h('div.msg', null,
        h('strong', { text: 'capcom noticed a change' }),
        h('span', null, h('code', { text: n.path }), ' now says ', h('code', { text: n.detail }),
          ` (since ${dayWord(sinceDay)}, ${String(since.getHours()).padStart(2, '0')}:${String(since.getMinutes()).padStart(2, '0')}). Mark “${n.title}” as applied for ${scope}?`),
        err),
      h('div.btnrow', null, markBtn,
        h('button.btn', { type: 'button', text: 'Not this', onclick: () => { dismiss(key); box.remove(); } })));
    return box;
  });
}

// ------------------------------------------------------------------ summary card

// The "Did it work?" card on Checks: how many changes are marked and where they stand.
export function summaryCard(d) {
  const card = h('a.card.tight.didit', { href: '/changes' }, h('span.label', { text: 'Did it work?' }));
  if (!d || !d.marks) {
    card.append(h('span.serif.mid', { text: 'Nothing marked yet' }),
      h('span.note', { text: READ_ONLY ? 'Mark a change as applied (not in read-only mode) and capcom measures whether it worked.' : 'Made one of these changes? Mark it as applied and capcom measures whether it worked.' }));
    return card;
  }
  const c = d.counts;
  const early = c.too_early === d.marks;
  card.append(h('span.serif.mid', { text: `${d.marks} change${d.marks === 1 ? '' : 's'} marked${early ? ', being measured' : ''}` }));
  const chips = VERDICT_ORDER.filter((v) => c[v]).map((v) => verdictChip(v, `${c[v]} ${VERDICTS[v].label.toLowerCase()}`));
  const row = h('div.chips', { style: 'align-items:center' }, chips);
  if (d.next_verdict) row.append(h('span.note', { text: `a verdict from ${monthDay(localDate(d.next_verdict))}` }));
  card.append(row);
  return card;
}

// capcom's layout checks: one pure function, evaluated in the page as
// `(<this file>)(opts)`, that returns the problems found in the current layout,
// each naming the element: [{kind, el, detail}]. It reads the DOM only.
//
// kinds:
//   page-scroll        the document scrolls sideways; el is an element past the edge
//   overlap            two visible text runs or controls cover each other
//   small-target       a control under 24 x 24 px (44 x 44 for the navigation and
//                      buttons at touch widths); links inside running text and a
//                      chart's data marks ([data-mark], .mk) are exempt
//   clipped            text cut by a box that hides overflow, without an ellipsis
//   truncated-no-title text cut with an ellipsis, but no title to read it in full
//   escapes            text running out of its card or panel
//   see-through-sticky a sticky or fixed box over the content without a solid background
//   sticky-too-tall    a sticky or fixed box over the content taller than 30 % of the screen
//
// opts: {touch: bool, modal: selector of an open overlay (check only inside it),
//        ignore: selector of intended overlays}
(function capcomLayoutCheck(opts) {
  opts = Object.assign({ touch: false, modal: null, ignore: '.tip, [data-layout-ignore]' }, opts || {});
  const doc = document.documentElement;
  const W = doc.clientWidth;
  const out = [];
  const seen = new Set();
  const add = (kind, el, detail) => {
    const key = kind + '|' + describe(el) + '|' + (detail || '');
    if (seen.has(key) || out.length > 60) return;
    seen.add(key);
    out.push({ kind, el: describe(el), detail: detail || '' });
  };
  const root = opts.modal ? document.querySelector(opts.modal) : document.body;
  if (!root) return [{ kind: 'missing', el: String(opts.modal), detail: '' }];

  function describe(el) {
    if (el && el.nodeType !== 1) el = el.parentElement;
    if (!el) return '?';
    const one = (e) => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + [...e.classList].slice(0, 2).map((c) => '.' + c).join('');
    const parts = [];
    for (let p = el.parentElement; p && parts.length < 2 && p !== document.body; p = p.parentElement) {
      if (p.id || p.classList.length) parts.unshift(one(p));
    }
    const text = (el.getAttribute('aria-label') || el.textContent || el.value || el.placeholder || '').trim().replace(/\s+/g, ' ').slice(0, 40);
    return [...parts, one(el)].join(' > ') + (text ? ` "${text}"` : '');
  }

  const ignored = (el) => !!(el.closest && el.closest(opts.ignore));
  const visible = (el) => el.checkVisibility ? el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) : !!el.offsetParent;
  const inter = (a, b) => ({ left: Math.max(a.left, b.left), top: Math.max(a.top, b.top), right: Math.min(a.right, b.right), bottom: Math.min(a.bottom, b.bottom) });
  const area = (r) => Math.max(0, r.right - r.left) * Math.max(0, r.bottom - r.top);
  const box = (r) => ({ left: r.left, top: r.top, right: r.right, bottom: r.bottom });

  // the part of the viewport an element's content can show in: its ancestors that
  // hide or scroll their overflow cut it. `hard` holds only the ones that hide it
  // (overflow hidden or clip), which a reader cannot scroll to.
  const clipCache = new Map();
  function clips(el) {
    if (!el || el === doc || el === document.body) return { any: null, hard: null, hardEl: null };
    if (clipCache.has(el)) return clipCache.get(el);
    const up = clips(el.parentElement);
    const cs = getComputedStyle(el);
    let res = up;
    const ox = cs.overflowX, oy = cs.overflowY;
    if (ox !== 'visible' || oy !== 'visible') {
      const r = el.getBoundingClientRect();
      const pad = { left: r.left + el.clientLeft, top: r.top + el.clientTop, right: r.left + el.clientLeft + el.clientWidth, bottom: r.top + el.clientTop + el.clientHeight };
      const lim = {
        left: ox === 'visible' ? -1e9 : pad.left, right: ox === 'visible' ? 1e9 : pad.right,
        top: oy === 'visible' ? -1e9 : pad.top, bottom: oy === 'visible' ? 1e9 : pad.bottom,
      };
      const hardX = ox === 'hidden' || ox === 'clip', hardY = oy === 'hidden' || oy === 'clip';
      const hardLim = { left: hardX ? lim.left : -1e9, right: hardX ? lim.right : 1e9, top: hardY ? lim.top : -1e9, bottom: hardY ? lim.bottom : 1e9 };
      res = {
        any: up.any ? inter(up.any, lim) : lim,
        hard: hardX || hardY ? (up.hard ? inter(up.hard, hardLim) : hardLim) : up.hard,
        hardEl: hardX || hardY ? el : up.hardEl,
      };
    }
    clipCache.set(el, res);
    return res;
  }
  const shown = (r, el) => { const c = clips(el).any; return c ? inter(r, c) : box(r); };

  // ------------------------------------------------------------ the boxes
  const texts = []; // {r (shown rect), raw, el}
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.data.trim() && n.parentElement && !n.parentElement.closest('script, style, noscript, title') ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT),
  });
  const range = document.createRange();
  const textEls = new Set();
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const el = n.parentElement;
    if (ignored(el) || !visible(el)) continue;
    range.selectNodeContents(n);
    for (const raw of range.getClientRects()) {
      if (raw.width < 1 || raw.height < 1) continue;
      const r = shown(raw, el);
      if (area(r) < 1) continue;
      texts.push({ r, raw, el });
      textEls.add(el);
    }
  }
  const SEL = 'a[href], button, input, select, textarea, summary, [role="button"], [tabindex]:not([tabindex="-1"])';
  const controls = [];
  for (const el of root.querySelectorAll(SEL)) {
    if (ignored(el) || !visible(el) || el.disabled) continue;
    const raw = el.getBoundingClientRect();
    if (raw.width < 1 && raw.height < 1) continue;
    const r = shown(raw, el);
    if (area(r) < 1) continue;
    controls.push({ r, raw, el });
  }

  // ------------------------------------------------------------ page scroll
  if (!opts.modal && doc.scrollWidth > W + 1) {
    let n = 0;
    for (const el of document.body.querySelectorAll('*')) {
      if (n >= 3 || !visible(el)) continue;
      const raw = el.getBoundingClientRect();
      if (raw.right <= W + 1 || raw.width < 1) continue;
      const c = clips(el).any;
      if (c && c.right <= W + 1) continue; // cut by a box that scrolls or hides it
      const parent = el.parentElement && el.parentElement.getBoundingClientRect();
      if (parent && parent.right > W + 1 && el.parentElement !== document.body) continue; // report the outermost
      add('page-scroll', el, `right edge ${Math.round(raw.right)} > ${W}`);
      n++;
    }
    if (!n) add('page-scroll', doc, `scrollWidth ${doc.scrollWidth} > ${W}`);
  }

  // ------------------------------------------------------------ sticky or fixed boxes
  // a box that stays put while the page scrolls must not sit over the content: it
  // needs a solid background, and it must not take much of the screen if it spans it
  if (!opts.modal) {
    const main = document.getElementById('main');
    const m = main && main.getBoundingClientRect();
    for (const el of document.body.querySelectorAll('*')) {
      const cs = getComputedStyle(el);
      if ((cs.position !== 'fixed' && cs.position !== 'sticky') || ignored(el) || !visible(el)) continue;
      const r = el.getBoundingClientRect();
      if (!m || r.right <= m.left + 1 || r.left >= m.right - 1) continue; // beside the content
      const bg = cs.backgroundColor.match(/[\d.]+/g) || [];
      if (cs.backgroundColor === 'transparent' || (bg.length === 4 && +bg[3] < 0.9)) add('see-through-sticky', el, cs.position);
      if (r.height > 0.3 * window.innerHeight) add('sticky-too-tall', el, `${Math.round(r.height)} px of ${window.innerHeight}`);
    }
  }

  // ------------------------------------------------------------ overlaps
  // a control and the text inside it, or nested controls, are one thing; two
  // texts or controls that are not nested must not cover each other
  const items = [...texts.map((t) => ({ ...t, kind: 't' })), ...controls.map((c) => ({ ...c, kind: 'c' }))];
  items.sort((a, b) => a.r.top - b.r.top);
  const related = (a, b) => a === b || a.contains(b) || b.contains(a);
  for (let i = 0; i < items.length; i++) {
    const a = items[i];
    for (let j = i + 1; j < items.length && items[j].r.top < a.r.bottom; j++) {
      const b = items[j];
      if (related(a.el, b.el)) continue;
      const x = inter(a.r, b.r);
      const w = x.right - x.left, hgt = x.bottom - x.top;
      if (w <= 2 || hgt <= 2) continue;
      const small = Math.min(area(a.r), area(b.r));
      if (a.kind === 't' || b.kind === 't') {
        // a control's own label sits inside it: compare the text with the control only
        // when the text is not that control's
        const ca = a.kind === 'c' ? a.el : a.el.closest(SEL), cb = b.kind === 'c' ? b.el : b.el.closest(SEL);
        if (ca && cb && ca === cb) continue;
        if (area(x) < 0.2 * small) continue;
      }
      add('overlap', a.el, 'with ' + describe(b.el));
    }
  }

  // ------------------------------------------------------------ target sizes
  const TOUCH = '#sidebar a, #sidebar button, #sidebar input, .topbar a, .topbar button, button, summary, select, input';
  // a chart's data marks (a bar segment, a heatmap cell) are exempt: they are reached
  // by keyboard too and their numbers are shown elsewhere (a table, a sentence), the
  // "equivalent" exception of WCAG 2.5.8; links and buttons that carry a tooltip
  // are still checked
  const MARK = '[data-mark], .mk:not(a, button)';
  for (const c of controls) {
    const el = c.el;
    if (el.matches(MARK)) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'inline' && el.tagName === 'A') {
      const host = el.parentElement;
      if (host && host.textContent.trim().length > el.textContent.trim().length + 2) continue; // a link in a sentence
    }
    const raw = c.raw;
    const need = opts.touch && el.matches(TOUCH) ? 44 : 24;
    // a control whose box is cut by a scrolling box is checked at its full size
    if (raw.width + 0.5 < need || raw.height + 0.5 < need) add('small-target', el, `${Math.round(raw.width)}x${Math.round(raw.height)} < ${need}`);
  }

  // ------------------------------------------------------------ clipped text
  for (const t of texts) {
    const hard = clips(t.el).hard;
    if (!hard) continue;
    const cutX = Math.max(0, hard.left - t.raw.left) + Math.max(0, t.raw.right - hard.right);
    const cutY = Math.max(0, hard.top - t.raw.top) + Math.max(0, t.raw.bottom - hard.bottom);
    if (cutX <= 1.5 && cutY <= 2.5) continue;
    if (cutX <= 1.5 && area(inter(t.raw, hard)) < 1) continue; // a whole line hidden below a clamp
    // an ellipsis or a line clamp on the way to the clipping box shows the cut
    let marked = false;
    for (let e = t.el; e && e !== clips(t.el).hardEl.parentElement; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.textOverflow === 'ellipsis' || (cs.webkitLineClamp && cs.webkitLineClamp !== 'none')) { marked = true; break; }
    }
    if (!marked) add('clipped', t.el, `cut by ${describe(clips(t.el).hardEl).split(' "')[0]}`);
    else {
      // the full text must be readable somewhere: a title or label holding it
      const host = t.el.closest('[title], [aria-label]');
      const full = host && (host.getAttribute('title') || '') + ' ' + (host.getAttribute('aria-label') || '');
      if (!full || !full.includes(t.el.textContent.trim().slice(0, 24))) add('truncated-no-title', t.el, '');
    }
  }

  // ------------------------------------------------------------ text escaping its card
  for (const t of texts) {
    const card = t.el.closest('.card, .panel, #sidebar, .topbar');
    if (!card || !root.contains(card)) continue;
    const c = card.getBoundingClientRect();
    if (t.r.right > c.right + 2 || t.r.left < c.left - 2) add('escapes', t.el, 'out of ' + describe(card).split(' "')[0]);
  }
  return out;
})

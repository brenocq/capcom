// Swapping a freshly drawn tree in for the one on screen without a jump: the new
// tree goes in at once (its own listeners, its own data), but every number that
// changed starts at the value the old tree showed and tweens to the new one,
// bars and SVG shapes glide to their new size, and parts that are new fade in.
// Focus, a typed search, open <details> and inner scroll positions carry over.
// Reduced motion: the same swap, without the tweens and the fade.

import { tweenMixed, reducedMotion, TWEEN_MS } from './loading.js';

// The attributes whose numbers tween: inline styles (bar widths, heights,
// positions) and SVG geometry.
const TWEEN_ATTRS = ['style', 'd', 'x', 'y', 'width', 'height', 'cx', 'cy', 'r', 'x1', 'x2', 'y1', 'y2', 'points', 'transform'];

// ------------------------------------------------------------------ tweens

const tweens = new Set();
let raf = 0;

function tick(now) {
  for (const tw of tweens) {
    const t = (now - tw.t0) / tw.ms;
    tw.apply(t >= 1 ? 1 : t);
    if (t >= 1) tweens.delete(tw);
  }
  raf = tweens.size ? requestAnimationFrame(tick) : 0;
}

function addTween(apply, ms = TWEEN_MS) {
  apply(0);
  tweens.add({ apply, t0: performance.now(), ms });
  if (!raf) raf = requestAnimationFrame(tick);
}

// Drop the tweens of nodes that left the document (the tree they ran on was replaced).
function prune() {
  for (const tw of tweens) if (tw.node && !tw.node.isConnected) tweens.delete(tw);
}

// ------------------------------------------------------------------ pairing

const sig = (n) => (n.nodeType === 3 ? '#text' : n.nodeType === 1 ? n.tagName + '.' + (n.getAttribute('class') || '') : null);

// Pair the fresh tree's nodes with the live tree's, child by child: a fresh child
// pairs with the next live child of the same tag and class within a few places
// (so one inserted or removed row does not unpair the rest).
function pair(live, fresh, plan) {
  plan.els.push([live, fresh]);
  const L = [...live.childNodes].filter(sig), F = [...fresh.childNodes].filter(sig);
  let j = 0;
  for (const f of F) {
    const want = sig(f);
    let hit = -1;
    for (let k = j; k < L.length && k < j + 4; k++) if (sig(L[k]) === want) { hit = k; break; }
    if (hit < 0) { if (f.nodeType === 1) plan.added.push(f); continue; }
    j = hit + 1;
    if (f.nodeType === 3) { if (L[hit].data !== f.data) plan.texts.push([f, L[hit].data]); } else pair(L[hit], f, plan);
  }
}

// ------------------------------------------------------------------ swap

// Put `fresh` in place of `live` (or into it, for the same node) and animate
// what changed. Returns the map from live elements to fresh ones.
export function swapIn(live, fresh, { animate = true } = {}) {
  const plan = { els: [], texts: [], added: [] };
  if (live !== fresh && live.nodeType === 1 && sig(live) === sig(fresh)) pair(live, fresh, plan);
  const moves = animate && !reducedMotion();
  const map = new Map(plan.els);
  const focused = document.activeElement;
  const scrolls = [];
  for (const [l, f] of plan.els) {
    if (l.scrollTop || l.scrollLeft) scrolls.push([f, l.scrollTop, l.scrollLeft]);
    if (l.tagName === 'DETAILS') f.open = l.open;
    if ((l.tagName === 'INPUT' || l.tagName === 'TEXTAREA') && l === focused) f.value = l.value;
    if (!moves) continue;
    for (const a of TWEEN_ATTRS) {
      const from = l.getAttribute(a), to = f.getAttribute(a);
      if (from === null || to === null || from === to) continue;
      if (tweenMixed(from, to, 0, true) === null) continue;
      addTween(Object.assign((t) => f.setAttribute(a, tweenMixed(from, to, t, true)), { node: f }));
    }
  }
  if (moves) {
    for (const [node, from] of plan.texts) {
      const to = node.data;
      if (tweenMixed(from, to, 0) === null) continue;
      addTween(Object.assign((t) => { node.data = tweenMixed(from, to, t); }, { node }));
    }
    if (plan.els.length > 1) for (const el of plan.added) el.setAttribute('data-fade', '');
  }
  if (live !== fresh) live.replaceWith(fresh);
  prune();
  for (const [el, top, left] of scrolls) { el.scrollTop = top; el.scrollLeft = left; }
  if (focused && map.has(focused)) {
    const f = map.get(focused);
    if (f.tabIndex < 0 && !f.hasAttribute('tabindex')) f.setAttribute('tabindex', '-1');
    f.focus({ preventScroll: true });
    if (focused.selectionStart != null && f.setSelectionRange) {
      try { f.setSelectionRange(focused.selectionStart, focused.selectionEnd); } catch (e) { /* not a text field */ }
    }
  }
  if (moves) setTimeout(() => { for (const el of plan.added) el.removeAttribute('data-fade'); }, 600);
  return map;
}

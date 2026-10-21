// Sets the theme before the first paint: the saved choice, else the system's.
(function () {
  var saved = null;
  try { saved = localStorage.getItem('capcom-theme'); } catch (e) { /* storage off */ }
  var dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = saved === 'dark' || saved === 'light' ? saved : (dark ? 'dark' : 'light');
})();

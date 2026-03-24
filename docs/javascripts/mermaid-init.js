/* ── Mermaid theme-aware initialisation ──────────────────────────────────────
   Load order in mkdocs.yml:
     1. javascripts/mermaid-init.js  ← this file (sets startOnLoad:false first)
     2. mermaid@11 CDN               ← reads window.mermaid config, skips auto-run
   Source text is cached by an inline <script> in main.html before any CDN
   script has a chance to replace .mermaid element content with rendered SVG.
   --------------------------------------------------------------------------- */

(function () {

  /* ── Prevent Mermaid CDN auto-start ─────────────────────────────────────── */
  /* Mermaid v10/v11 reads window.mermaid on startup.                         */
  window.mermaid = { startOnLoad: false };

  /* ── Palette tokens ─────────────────────────────────────────────────────── */

  /*
   * Design intent
   * ─────────────
   * LIGHT:  warm off-white canvas, clean white nodes, amber actor/note
   *         accents, stone-grey arrows — calm, document-like.
   *
   * DARK:   near-black canvas, zinc-800 nodes, warm amber-tinted secondary
   *         tier, cool slate-tinted tertiary tier, muted arrows — rich but
   *         easy on the eyes.
   *
   * SHARED: amber (#f59e0b) is the accent in both modes — actor borders,
   *         note borders, activation boxes, link colours.
   */

  var L = {
    bg:          '#f8f8f6',  /* site --bg                    */
    surface:     '#ffffff',  /* main node fill               */
    surface2:    '#f0f0ec',  /* cluster / subgraph fill      */
    border:      '#d0d0c8',  /* node / cluster border        */
    borderDim:   '#e0e0d8',  /* cluster border (lighter)     */
    line:        '#78716c',  /* stone-500 — arrow lines      */
    lineDim:     '#c8c8c0',  /* sequence lifelines           */
    text:        '#1a1a14',  /* site --text                  */
    textMid:     '#5a5a4a',  /* site --text-2, labels        */
    tier2:       '#fef3c7',  /* amber-100 — secondary nodes  */
    tier3:       '#e0f2fe',  /* sky-100   — tertiary nodes   */
    accent:      '#f59e0b',  /* amber-500                    */
    accentSoft:  '#d97706',  /* amber-600 (note borders)     */
    accentFill:  '#fef3c7',  /* amber-100 (actor / note bg)  */
    accentNote:  '#fefce8',  /* yellow-50 (note body)        */
    edgeBg:      '#ffffff',
  };

  var D = {
    bg:          '#141418',  /* site --surface (dark)        */
    surface:     '#1c1c22',  /* main node fill               */
    surface2:    '#0d0d10',  /* cluster / subgraph fill      */
    border:      '#3a3a4a',  /* node border                  */
    borderDim:   '#22222e',  /* cluster border               */
    line:        '#6b6b80',  /* muted arrow lines            */
    lineDim:     '#2e2e3e',  /* sequence lifelines           */
    text:        '#f0f0f4',  /* site --text (dark)           */
    textMid:     '#9090a8',  /* site --text-2 (dark)         */
    tier2:       '#1e1510',  /* warm amber-tinted dark       */
    tier3:       '#0c1520',  /* cool slate-tinted dark       */
    accent:      '#f59e0b',  /* amber — same as light        */
    accentSoft:  '#d97706',  /* amber-600                    */
    accentFill:  '#1e1510',  /* warm dark (actor / note bg)  */
    accentNote:  '#1c1a08',  /* slightly yellower dark       */
    edgeBg:      '#1c1c22',
  };

  /* ── Build themeVariables ───────────────────────────────────────────────── */

  function makeVars(p) {
    return {
      /* General */
      background:            p.bg,
      primaryColor:          p.surface,
      primaryTextColor:      p.text,
      primaryBorderColor:    p.border,
      secondaryColor:        p.tier2,
      tertiaryColor:         p.tier3,
      lineColor:             p.line,
      edgeLabelBackground:   p.edgeBg,
      titleColor:            p.text,

      /* Flowchart / Graph */
      mainBkg:               p.surface,
      nodeBorder:            p.border,
      clusterBkg:            p.surface2,
      clusterBorder:         p.borderDim,

      /* Typography */
      fontFamily:            '"Geist", -apple-system, BlinkMacSystemFont, sans-serif',
      fontSize:              '13.5px',
      defaultLinkColor:      p.accent,

      /* Sequence — actors */
      actorBkg:              p.accentFill,
      actorBorder:           p.accent,
      actorTextColor:        p.text,
      actorLineColor:        p.lineDim,

      /* Sequence — messages */
      signalColor:           p.line,
      signalTextColor:       p.text,

      /* Sequence — alt / else label boxes */
      labelBoxBkgColor:      p.surface,
      labelBoxBorderColor:   p.border,
      labelTextColor:        p.textMid,

      /* Sequence — loops */
      loopTextColor:         p.text,

      /* Sequence — notes */
      noteBorderColor:       p.accentSoft,
      noteBkgColor:          p.accentNote,
      noteTextColor:         p.text,

      /* Sequence — activation boxes */
      activationBorderColor: p.accent,
      activationBkgColor:    p.accentFill,
    };
  }

  var THEMES = {
    light: { theme: 'base', themeVariables: makeVars(L) },
    dark:  { theme: 'base', themeVariables: makeVars(D) },
  };

  /* ── Helpers ────────────────────────────────────────────────────────────── */

  function isDark() {
    return document.documentElement.getAttribute('data-md-color-scheme') === 'slate'
        || document.body.getAttribute('data-md-color-scheme') === 'slate';
  }

  /* Restore original diagram source (cached in main.html inline script) */
  function restoreSource(el) {
    var src = el.dataset.mermaidSrc;
    if (src) {
      el.textContent = src;
      el.removeAttribute('data-processed');
    }
  }

  /* ── Render ─────────────────────────────────────────────────────────────── */

  function render() {
    if (typeof mermaid === 'undefined') return;
    var cfg = isDark() ? THEMES.dark : THEMES.light;
    mermaid.initialize(Object.assign({ startOnLoad: false, securityLevel: 'loose' }, cfg));
    document.querySelectorAll('.mermaid').forEach(restoreSource);
    mermaid.run({ querySelector: '.mermaid' });
  }

  /* ── Boot ───────────────────────────────────────────────────────────────── */

  /* DOMContentLoaded fires after both deferred scripts have executed.
     At that point Mermaid CDN is loaded but (startOnLoad:false) hasn't run.
     We trigger the first render here.                                        */
  document.addEventListener('DOMContentLoaded', render);

  /* ── Theme toggle ───────────────────────────────────────────────────────── */

  document.addEventListener('change', function (e) {
    if (e.target && e.target.closest && e.target.closest('[data-md-component="palette"]')) {
      setTimeout(render, 80);
    }
  });

  if (window.MutationObserver) {
    var obs = new MutationObserver(function (mutations) {
      for (var i = 0; i < mutations.length; i++) {
        if (mutations[i].attributeName === 'data-md-color-scheme') {
          setTimeout(render, 80);
          return;
        }
      }
    });
    obs.observe(document.documentElement, { attributes: true });
    obs.observe(document.body, { attributes: true });
  }

})();

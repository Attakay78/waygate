---
hide:
  - navigation
  - toc
---

<div class="hp-page">

<!-- ═══════════════════════════════════════════════
     HERO  — two-column: text left, code window right
══════════════════════════════════════════════════ -->
<section class="hp-hero">
  <div class="hp-hero-inner">

    <div class="hp-hero-text">
      <a href="changelog/" class="hp-badge">
        <span class="hp-badge-dot"></span>
        v0.8.0 &nbsp;·&nbsp; Feature flags &amp; OpenFeature are here
      </a>

      <h1 class="hp-h1">
        Runtime control<br>for <span class="hp-accent">Python APIs</span>
      </h1>

      <p class="hp-hero-desc">
        Feature flags, rate limiting, and maintenance mode. No redeployment.
        Self-hosted. OpenFeature compliant. Zero restarts.
      </p>

      <div class="hp-hero-btns">
        <a href="tutorial/installation/" class="hp-btn hp-btn-primary">Get Started →</a>
        <a href="https://github.com/Attakay78/api-shield" class="hp-btn hp-btn-ghost" target="_blank">
          <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/></svg>
          GitHub
        </a>
      </div>

      <div class="hp-install">
        <span class="hp-install-label">$ </span>
        <code class="hp-install-cmd">uv add "api-shield[all]"</code>
        <button class="hp-copy-btn" data-copy='uv add "api-shield[all]"' title="Copy to clipboard">
          <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        </button>
      </div>
    </div>

    <div class="hp-hero-visual">
      <div class="hp-code-win">
        <div class="hp-code-win-bar">
          <span class="hp-dot hp-dot-r"></span>
          <span class="hp-dot hp-dot-y"></span>
          <span class="hp-dot hp-dot-g"></span>
          <span class="hp-code-win-title">app.py</span>
        </div>
        <pre class="hp-code-win-body"><code><span class="t-kw">from</span> <span class="t-mod">fastapi</span> <span class="t-kw">import</span> FastAPI
<span class="t-kw">from</span> <span class="t-mod">shield.core.config</span> <span class="t-kw">import</span> make_engine
<span class="t-kw">from</span> <span class="t-mod">shield.fastapi</span> <span class="t-kw">import</span> (
    ShieldMiddleware, ShieldAdmin,
    maintenance, env_only, deprecated,
    force_active, rate_limit,
)

engine <span class="t-op">=</span> <span class="t-fn">make_engine</span>()
app    <span class="t-op">=</span> <span class="t-cls">FastAPI</span>()
app.<span class="t-fn">add_middleware</span>(<span class="t-cls">ShieldMiddleware</span>, engine<span class="t-op">=</span>engine)

<span class="t-cm"># Database migration in progress</span>
<span class="t-dec">@app.get</span>(<span class="t-str">"/payments"</span>)
<span class="t-dec">@maintenance</span>(reason<span class="t-op">=</span><span class="t-str">"Back at 04:00 UTC"</span>)
<span class="t-kw">async def</span> <span class="t-fn">get_payments</span>(): ...

<span class="t-cm"># Hidden in production silently</span>
<span class="t-dec">@app.get</span>(<span class="t-str">"/debug"</span>)
<span class="t-dec">@env_only</span>(<span class="t-str">"dev"</span>, <span class="t-str">"staging"</span>)
<span class="t-kw">async def</span> <span class="t-fn">debug_info</span>(): ...

<span class="t-cm"># 100 req/min per IP, no extra config</span>
<span class="t-dec">@app.get</span>(<span class="t-str">"/search"</span>)
<span class="t-dec">@rate_limit</span>(<span class="t-str">"100/minute"</span>, key<span class="t-op">=</span><span class="t-str">"ip"</span>)
<span class="t-kw">async def</span> <span class="t-fn">search</span>(): ...

<span class="t-cm"># Immune to all checks, always 200</span>
<span class="t-dec">@app.get</span>(<span class="t-str">"/health"</span>)
<span class="t-dec">@force_active</span>
<span class="t-kw">async def</span> <span class="t-fn">health</span>(): ...

<span class="t-cm"># Dashboard + REST API at /shield</span>
app.<span class="t-fn">mount</span>(<span class="t-str">"/shield"</span>,
    <span class="t-cls">ShieldAdmin</span>(engine<span class="t-op">=</span>engine, auth<span class="t-op">=</span>(<span class="t-str">"admin"</span>, <span class="t-str">"secret"</span>))
)</code></pre>
      </div>
    </div>

  </div>
</section>

<!-- ═══════════════════════════════════════════════
     MARQUEE  — scrolling feature strip
══════════════════════════════════════════════════ -->
<div class="hp-marquee-outer">
  <div class="hp-marquee-track">
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Zero-restart control</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>OpenFeature compliant</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Rate limiting</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Maintenance mode</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Feature flags</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Audit log</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Redis backends</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Webhooks</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>CLI control</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Multi-service fleet</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Scheduled windows</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Canary rollouts</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>A/B testing</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Env gating</span>
    <!-- duplicate for seamless loop -->
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Zero-restart control</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>OpenFeature compliant</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Rate limiting</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Maintenance mode</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Feature flags</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Audit log</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Redis backends</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Webhooks</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>CLI control</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Multi-service fleet</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Scheduled windows</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Canary rollouts</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>A/B testing</span>
    <span class="hp-mq-item"><span class="hp-mq-dot"></span>Env gating</span>
  </div>
</div>

<!-- ═══════════════════════════════════════════════
     STATS
══════════════════════════════════════════════════ -->
<section class="hp-stats">
  <div class="hp-stats-inner">
    <div class="hp-stat">
      <span class="hp-stat-num">467</span>
      <span class="hp-stat-label">Tests Passing</span>
    </div>
    <div class="hp-stat">
      <span class="hp-stat-num">3</span>
      <span class="hp-stat-label">Storage Backends</span>
    </div>
    <div class="hp-stat">
      <span class="hp-stat-num">0</span>
      <span class="hp-stat-label">Restarts Needed</span>
    </div>
    <div class="hp-stat">
      <span class="hp-stat-num">MIT</span>
      <span class="hp-stat-label">Open Source</span>
    </div>
  </div>
</section>

<!-- ═══════════════════════════════════════════════
     ECOSYSTEM  — problem / solution split
══════════════════════════════════════════════════ -->
<section class="hp-ecosystem hp-reveal">
  <div class="hp-ecosystem-inner">

    <div class="hp-ecosystem-col">
      <span class="hp-label">The problem</span>
      <h2 class="hp-h2">Most runtime tools are blunt instruments</h2>
      <p class="hp-body">Shut everything down or nothing at all. Redeployment just to flip a flag. No audit trail. No per-route control. No way to gradually roll out a change to 5% of users.</p>
    </div>

    <div class="hp-ecosystem-col">
      <span class="hp-label">The solution</span>
      <h2 class="hp-h2">One ecosystem. Every Python API.</h2>
      <p class="hp-body"><code class="hp-inline-code">api-shield</code> treats every route as a first-class entity with its own lifecycle. State changes are immediate. No restart. No redeploy. Full control from a dashboard, CLI, or REST API.</p>
    </div>

  </div>

  <div class="hp-pillars">
    <div class="hp-pillar">
      <div class="hp-pillar-icon">
        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg>
      </div>
      <h4 class="hp-pillar-title">Feature Flags</h4>
      <p class="hp-pillar-body">OpenFeature compliant. Boolean, string, float, JSON. Targeting rules, segments, percentage rollouts, prerequisites.</p>
    </div>
    <div class="hp-pillar">
      <div class="hp-pillar-icon">
        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
      </div>
      <h4 class="hp-pillar-title">Rate Limiting</h4>
      <p class="hp-pillar-body">Per-IP, per-user, per-key, or global. Tiered limits, burst allowance, real-time policy mutation. Memory, file, or Redis.</p>
    </div>
    <div class="hp-pillar">
      <div class="hp-pillar-icon">
        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
      </div>
      <h4 class="hp-pillar-title">Route Lifecycle</h4>
      <p class="hp-pillar-body">Maintenance, env gating, deprecation, instant disable. Per route. Managed from dashboard, CLI, or REST API with no code changes.</p>
    </div>
  </div>
</section>

<!-- ═══════════════════════════════════════════════
     DIFFERENTIATION
══════════════════════════════════════════════════ -->
<section class="hp-diff hp-reveal">
  <div class="hp-diff-inner">
    <div class="hp-diff-header">
      <span class="hp-label">What makes it different</span>
      <h2 class="hp-h2">Other tools flag features.<br>api-shield controls routes.</h2>
      <p class="hp-body">LaunchDarkly, Flagsmith, Unleash — they operate at the application layer and have no concept of what a route is. You can't ask them to put <code class="hp-inline-code">/api/payments</code> into maintenance mode, schedule the window, reset its rate limit counters when it comes back, or show you a live dashboard of route states across your fleet. That is a different problem, and that is what api-shield solves.</p>
    </div>

    <div class="hp-diff-grid">
      <div class="hp-diff-card">
        <div class="hp-diff-icon">
          <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
        </div>
        <h4 class="hp-diff-title">Route-aware request context</h4>
        <p class="hp-diff-body">api-shield reads <code>request.state.user_id</code>, FastAPI dependencies, and ASGI request context directly. The route is the unit of control, not a string key passed to an SDK.</p>
      </div>

      <div class="hp-diff-card">
        <div class="hp-diff-icon">
          <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        </div>
        <h4 class="hp-diff-title">Maintenance windows, not just toggles</h4>
        <p class="hp-diff-body">Schedule <code>/api/payments</code> out for 2 hours. When the window closes, the route comes back automatically, rate limit counters reset, and a webhook fires to Slack. No code change needed.</p>
      </div>

      <div class="hp-diff-card">
        <div class="hp-diff-icon">
          <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
        </div>
        <h4 class="hp-diff-title">No SaaS, no API keys</h4>
        <p class="hp-diff-body">Back your state with Redis you already run, or a plain JSON file for local dev. No data leaves your infra. No third-party uptime dependency sitting in your request path.</p>
      </div>
    </div>
  </div>
</section>

<!-- ═══════════════════════════════════════════════
     FEATURES  — 3-column numbered grid
══════════════════════════════════════════════════ -->
<section class="hp-features hp-reveal">
  <div class="hp-features-inner">

    <div class="hp-section-header">
      <span class="hp-label">Why api-shield</span>
      <h2 class="hp-h2">Built for production from day one</h2>
    </div>

    <div class="hp-feat-grid">

      <div class="hp-feat-item">
        <span class="hp-feat-num">01</span>
        <h4 class="hp-feat-title">Decorator-first DX</h4>
        <p class="hp-feat-body">State lives next to the route. <code>@maintenance</code>, <code>@disabled</code>, <code>@env_only</code>, <code>@rate_limit</code>. One line, zero boilerplate.</p>
      </div>

      <div class="hp-feat-item">
        <span class="hp-feat-num">02</span>
        <h4 class="hp-feat-title">Fail-open by default</h4>
        <p class="hp-feat-body">If the backend is unreachable, requests pass through. Shield never takes down your API due to its own failures.</p>
      </div>

      <div class="hp-feat-item">
        <span class="hp-feat-num">03</span>
        <h4 class="hp-feat-title">OpenFeature compliant</h4>
        <p class="hp-feat-body">Use any OpenFeature-compatible SDK. Switch providers without rewriting flag evaluation logic. Vendor-portable from day one.</p>
      </div>

      <div class="hp-feat-item">
        <span class="hp-feat-num">04</span>
        <h4 class="hp-feat-title">HTMX admin dashboard</h4>
        <p class="hp-feat-body">Live SSE updates. Audit log. Flag evaluation stream. No JavaScript framework. Mount at any path in two lines.</p>
      </div>

      <div class="hp-feat-item">
        <span class="hp-feat-num">05</span>
        <h4 class="hp-feat-title">Multi-service fleet</h4>
        <p class="hp-feat-body">ShieldServer + ShieldSDK for centralized control across multiple services. State synced via SSE with zero per-request latency.</p>
      </div>

      <div class="hp-feat-item">
        <span class="hp-feat-num">06</span>
        <h4 class="hp-feat-title">Full CLI + REST API</h4>
        <p class="hp-feat-body">Every dashboard action is available from the terminal or CI pipeline. Token auth. Cross-platform config at <code>~/.shield/config.json</code>.</p>
      </div>

    </div>
  </div>
</section>

<!-- ═══════════════════════════════════════════════
     CTA
══════════════════════════════════════════════════ -->
<section class="hp-cta hp-reveal">
  <div class="hp-cta-inner">
    <span class="hp-label">Get started</span>
    <h2 class="hp-h2">Add runtime control to your API today</h2>
    <p class="hp-body">Install in seconds. No external services. Works with any ASGI framework.</p>

    <div class="hp-cta-install">
      <span class="hp-install-label">$ </span>
      <code class="hp-install-cmd">uv add "api-shield[all]"</code>
      <button class="hp-copy-btn" data-copy='uv add "api-shield[all]"' title="Copy">
        <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2 2v1"/></svg>
      </button>
    </div>

    <div class="hp-cta-btns">
      <a href="tutorial/installation/" class="hp-btn hp-btn-primary">Read the Docs</a>
      <a href="https://github.com/Attakay78/api-shield" class="hp-btn hp-btn-ghost" target="_blank">Star on GitHub</a>
    </div>

    <div class="hp-cta-badges">
      <img src="https://img.shields.io/pypi/v/api-shield?color=F59E0B&label=pypi" alt="PyPI">
      <img src="https://img.shields.io/pypi/pyversions/api-shield?color=F59E0B" alt="Python">
      <img src="https://img.shields.io/github/license/Attakay78/api-shield?color=F59E0B" alt="License">
    </div>
  </div>
</section>

</div>

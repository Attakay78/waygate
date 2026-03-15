# docs: add MkDocs Material documentation site

Adds a full documentation site built with MkDocs Material, covering everything from quickstart to production deployment patterns.

## What's included

- **Tutorial** — installation, first decorator, middleware, backends, admin dashboard, CLI
- **Reference** — decorators, ShieldEngine, backends, middleware, models, exceptions, CLI commands
- **Guides** — production monitoring and deployment automation scripts
- **Adapters** — FastAPI guide and custom backend walkthrough
- **Changelog** — v0.1.0 and v0.2.0 release history

## Other changes

- `README.md` trimmed to essentials — links to the docs site for deep dives
- `.github/workflows/docs.yml` — auto-publishes to GitHub Pages on every merge that touches `docs/` or `mkdocs.yml`
- `pyproject.toml` — `[docs]` optional dependency group added

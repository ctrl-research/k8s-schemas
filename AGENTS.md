# AGENTS.md

## Purpose

Static JSON Schema catalog for Kubernetes CRDs, generated from upstream charts
and release manifests and served via GitHub Pages at
https://ctrl-research.github.io/k8s-schemas/. Consumed by
yaml-language-server modelines and kubeconform in the homelab-cluster repo.

## Tech stack

- **Python 3** (stdlib + PyYAML) for generation and version checking
- **helm** CLI for chart-based CRD extraction
- **GitHub Actions** for CI, Pages deploy, and scheduled version bumps
- **Renovate** as a secondary bump mechanism (custom regex manager)

## Structure

```
sources.yaml                       # CRD sources + version pins (the only config)
scripts/generate.py                # sources.yaml -> site/ (schemas + index)
scripts/check-versions.py          # stale-pin report; --update bumps + keeps history
.github/workflows/ci.yaml          # PR: generate + validate all schemas
.github/workflows/publish.yaml     # main push + weekly: build + deploy Pages
.github/workflows/update-versions.yaml  # daily: bump PR for stale pins
```

## Conventions

- Never push directly to `main`; all changes via PR with review.
- `site/` is build output — never commit it to `main`. The publish workflow
  commits it to the `gh-pages` branch (which Pages serves) **additively**:
  never force-push or delete files there; stale pinned tiers are permanent
  URLs by design.
- Version bumps must preserve history: the old `version:` moves into
  `extraVersions` (max 5) so published pinned-tier URLs keep resolving.
  `check-versions.py --update` does this automatically — don't hand-edit pins
  without doing the same.
- Each source's `version:` line must keep the `# renovate:` comment directly
  above it (the Renovate regex manager and check-versions both rely on it).
- Source order in `sources.yaml` decides latest-tier ownership for duplicated
  CRDs — keep canonical sources (e.g. gateway-api) above charts that bundle
  copies of the same CRDs.
- OCI chart tags don't always match the Flux OCIRepository `semver:` field
  style — verify the literal tag exists (e.g. flux-operator publishes `0.37.0`,
  not `v0.37.0`).

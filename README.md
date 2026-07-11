# k8s-schemas

Self-hosted JSON Schema catalog for Kubernetes resources — CRDs from upstream
charts and release manifests, core Kubernetes API types from the upstream
OpenAPI spec (strict flavor: unknown fields rejected), and kustomize schemas —
served over GitHub Pages:

**https://ctrl-research.github.io/k8s-schemas/**

Schemas are extracted from the exact operator versions pinned in
[`sources.yaml`](sources.yaml), so editor validation and CI validate against
what the cluster actually runs — including full `description` text for editor
hover docs.

Generated schemas are committed to the [`gh-pages`](../../tree/gh-pages)
branch (which Pages serves directly), so content changes are diffable per
publish and every file is also fetchable via
`https://raw.githubusercontent.com/ctrl-research/k8s-schemas/gh-pages/{path}`.
Publishes are **additive**: changed files update, but nothing is ever deleted,
so pinned-tier URLs remain valid even after versions age out of
`extraVersions`.

## URL layout

```
{group}/{kind}_{version}.json                           # latest tier — tracks sources.yaml pins
{source}/{sourceVersion}/{group}/{kind}_{version}.json  # pinned tier — stable forever
catalog.json                                            # machine-readable index
```

Core Kubernetes types use `core/` for the empty API group
(`core/namespace_v1.json`, `apps/deployment_v1.json`); kustomize schemas live
under `kustomize.config.k8s.io/` including `component_v1alpha1.json`.

Examples:

```
https://ctrl-research.github.io/k8s-schemas/helm.toolkit.fluxcd.io/helmrelease_v2.json
https://ctrl-research.github.io/k8s-schemas/external-secrets/0.19.2/external-secrets.io/externalsecret_v1.json
```

## Usage

**yaml-language-server** (per-file modeline):

```yaml
# yaml-language-server: $schema=https://ctrl-research.github.io/k8s-schemas/helm.toolkit.fluxcd.io/helmrelease_v2.json
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
```

**kubeconform** (the latest tier matches its schema-location template):

```sh
kubeconform -strict \
  -schema-location default \
  -schema-location 'https://ctrl-research.github.io/k8s-schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  manifests/
```

## How it stays current

- **`update-versions` workflow** (daily): resolves the newest stable version of
  every source natively (GitHub releases API for `url` sources, `index.yaml`
  for `helm` repos, registry tags for `oci` charts) and opens a bump PR. The
  previous pin is demoted into `extraVersions` (last 5 kept), so
  already-published pinned-tier URLs keep resolving after the bump.
- **Renovate**: the `# renovate:` comments in `sources.yaml` let Renovate
  propose the same bumps if the app is enabled.
- **`publish` workflow**: every push to `main` (and a weekly cron) regenerates
  the full site and deploys it to GitHub Pages.
- **`ci` workflow**: PRs regenerate the catalog and check every schema is
  valid draft-07 JSON Schema.

## Adding a source

Append an entry to `sources.yaml`:

```yaml
  - name: cert-manager
    kind: helm                                  # helm | oci | url
    repo: https://charts.jetstack.io
    chart: cert-manager
    # renovate: datasource=helm depName=cert-manager registryUrl=https://charts.jetstack.io
    version: v1.18.0
```

Rules of thumb:

- `helm`/`oci` sources are rendered with `helm template --include-crds` and
  default values; add a `values:` list of `--set` strings if the chart hides
  its CRDs behind a flag.
- `url` sources take a list of manifest URLs with `{version}` substituted in
  (the version checker assumes these are GitHub release/raw URLs).
- `k8s` converts core API types from the Kubernetes OpenAPI spec; `jsonschema`
  republishes existing JSON Schema files verbatim (used for kustomize).
- Order matters: when two sources ship the same CRD (e.g. envoy-gateway
  bundles the gateway-api CRDs), the first source in the file owns the latest
  tier; later duplicates publish to their pinned tier only.

## Local development

```sh
pip install pyyaml
python3 scripts/generate.py --output-dir site   # needs helm on PATH
python3 scripts/generate.py --only metallb      # single source while iterating
python3 scripts/check-versions.py               # report stale pins
python3 scripts/check-versions.py --update      # bump pins + accumulate history
```

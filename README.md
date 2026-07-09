# k8s-schemas

Self-hosted JSON Schema catalog for Kubernetes CRDs, generated from upstream
charts and release manifests and served over GitHub Pages:

**https://ctrl-research.github.io/k8s-schemas/**

Schemas are extracted from the exact operator versions pinned in
[`sources.yaml`](sources.yaml) and converted from each CRD's `openAPIV3Schema`,
so editor validation and CI validate against what the cluster actually runs —
including full `description` text for editor hover docs.

## URL layout

```
{group}/{kind}_{version}.json                           # latest tier — tracks sources.yaml pins
{source}/{sourceVersion}/{group}/{kind}_{version}.json  # pinned tier — stable forever
catalog.json                                            # machine-readable index
```

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

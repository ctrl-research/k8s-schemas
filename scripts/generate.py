#!/usr/bin/env python3
"""Generate a static JSON Schema catalog from Kubernetes CRD sources.

Reads sources.yaml, fetches CRDs from each source (helm chart, OCI chart, or
raw manifest URLs), converts every served CRD version's openAPIV3Schema to
JSON Schema, and writes a site/ tree compatible with both kubeconform's
schema-location template and yaml-language-server modelines:

    site/{group}/{kind}_{version}.json                          latest tier
    site/{source}/{sourceVersion}/{group}/{kind}_{version}.json pinned tier
    site/catalog.json                                           machine index
    site/index.html                                             browsable index

When two sources ship the same CRD, the source listed first in sources.yaml
owns the latest tier; later duplicates only publish to their pinned tier.
"""
import argparse
import html
import json
import pathlib
import shutil
import subprocess
import sys
import urllib.request

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class CRDLoader(yaml.SafeLoader):
    """SafeLoader that tolerates a literal `=` key (YAML 1.1 'value' tag).

    kube-prometheus-stack's CRDs contain a property named `=`, which pyyaml
    otherwise refuses to construct.
    """


CRDLoader.add_constructor("tag:yaml.org,2002:value", lambda loader, node: node.value)


def load_docs(stream):
    return list(yaml.load_all(stream, Loader=CRDLoader))


def fetch_url_docs(source, version):
    docs = []
    for template in source["urls"]:
        url = template.format(version=version)
        with urllib.request.urlopen(url, timeout=60) as resp:
            docs.extend(load_docs(resp.read()))
    return docs


def fetch_chart_docs(source, version):
    cmd = ["helm", "template", "schema-extract"]
    if source["kind"] == "oci":
        cmd += [source["chart"]]
    else:
        cmd += [source["chart"], "--repo", source["repo"]]
    cmd += ["--version", version, "--include-crds", "--namespace", "schema-extract"]
    for opt in source.get("values", []):
        cmd += ["--set", opt]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr.strip()[:500]}")
    return load_docs(result.stdout)


def to_json_schema(node):
    """Recursively convert an OpenAPI v3 structural schema to JSON Schema."""
    if isinstance(node, list):
        return [to_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {k: to_json_schema(v) for k, v in node.items()}
    # int-or-string properties carry no JSON-Schema type
    if out.pop("x-kubernetes-int-or-string", None) or out.get("format") == "int-or-string":
        out.pop("type", None)
        out.pop("format", None)
        out["oneOf"] = [{"type": "string"}, {"type": "integer"}]
    # OpenAPI nullable -> JSON Schema type union
    if out.pop("nullable", None) and isinstance(out.get("type"), str):
        out["type"] = [out["type"], "null"]
    return out


def convert_crd(crd):
    """Yield (group, kind, version, schema) for every served version of a CRD."""
    spec = crd["spec"]
    group = spec["group"]
    kind = spec["names"]["kind"]
    for ver in spec["versions"]:
        openapi = (ver.get("schema") or {}).get("openAPIV3Schema")
        if not openapi:
            continue
        schema = to_json_schema(openapi)
        schema["$schema"] = "http://json-schema.org/draft-07/schema#"
        yield group, kind, ver["name"], schema


def write_schema(path, schema):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, indent=2, sort_keys=False) + "\n")


def build_index_html(catalog, site):
    by_source = {}
    for entry in catalog:
        by_source.setdefault(entry["source"], {}).setdefault(
            entry["sourceVersion"], []
        ).append(entry)
    sections = []
    for source, versions in by_source.items():
        blocks = []
        for version, entries in versions.items():
            links = "\n".join(
                f'<li><a href="{e["pinnedPath"]}">{e["group"]}/{e["kind"]}_{e["version"]}.json</a></li>'
                for e in sorted(entries, key=lambda e: (e["group"], e["kind"], e["version"]))
            )
            blocks.append(f"<details><summary>{html.escape(version)} ({len(entries)} schemas)</summary><ul>{links}</ul></details>")
        sections.append(f"<section><h2>{html.escape(source)}</h2>{''.join(blocks)}</section>")
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>k8s-schemas</title>
<style>
  body {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; max-width: 60rem;
         margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
  h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.1rem; margin-bottom: .25rem; }}
  code, pre {{ background: rgba(127,127,127,.12); border-radius: 4px; padding: .1rem .3rem; }}
  pre {{ padding: .75rem; overflow-x: auto; }}
  ul {{ margin: .25rem 0; }} li {{ list-style: none; }}
  @media (prefers-color-scheme: dark) {{ body {{ background: #111; color: #ddd; }} a {{ color: #8cf; }} }}
</style>
</head>
<body>
<h1>k8s-schemas</h1>
<p>JSON Schemas for Kubernetes CRDs, generated from upstream charts and manifests.</p>
<p>Latest tier (tracks pinned source versions):</p>
<pre>{{group}}/{{kind}}_{{version}}.json</pre>
<p>Pinned tier (one per source version):</p>
<pre>{{source}}/{{sourceVersion}}/{{group}}/{{kind}}_{{version}}.json</pre>
<p>kubeconform: <code>-schema-location '&lt;base&gt;/{{{{.Group}}}}/{{{{.ResourceKind}}}}_{{{{.ResourceAPIVersion}}}}.json'</code>
&middot; <a href="catalog.json">catalog.json</a></p>
{''.join(sections)}
</body>
</html>
"""
    (site / "index.html").write_text(page)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", default=str(REPO_ROOT / "sources.yaml"))
    parser.add_argument("--output-dir", default=str(REPO_ROOT / "site"))
    parser.add_argument("--only", help="comma-separated source names to generate")
    args = parser.parse_args()

    config = yaml.safe_load(pathlib.Path(args.sources).read_text())
    sources = config["sources"]
    if args.only:
        wanted = set(args.only.split(","))
        sources = [s for s in sources if s["name"] in wanted]

    site = pathlib.Path(args.output_dir)
    if site.exists():
        shutil.rmtree(site)
    site.mkdir(parents=True)
    (site / ".nojekyll").touch()

    catalog = []
    latest_owner = {}  # relative latest-tier path -> source name
    failures = []

    for source in sources:
        name = source["name"]
        versions = [source["version"]] + source.get("extraVersions", [])
        for i, source_version in enumerate(versions):
            is_latest = i == 0
            try:
                if source["kind"] == "url":
                    docs = fetch_url_docs(source, source_version)
                else:
                    docs = fetch_chart_docs(source, source_version)
            except Exception as exc:
                failures.append(f"{name}@{source_version}: {exc}")
                continue
            crds = [d for d in docs if isinstance(d, dict) and d.get("kind") == "CustomResourceDefinition"]
            if not crds:
                failures.append(f"{name}@{source_version}: no CRDs found")
                continue
            count = 0
            for crd in crds:
                for group, kind, version, schema in convert_crd(crd):
                    rel = f"{group}/{kind.lower()}_{version}.json"
                    pinned = f"{name}/{source_version}/{rel}"
                    write_schema(site / pinned, schema)
                    if is_latest:
                        owner = latest_owner.setdefault(rel, name)
                        if owner == name:
                            write_schema(site / rel, schema)
                        else:
                            print(f"  note: {rel} owned by {owner}, {name} publishes pinned tier only")
                    catalog.append({
                        "source": name, "sourceVersion": source_version,
                        "group": group, "kind": kind, "version": version,
                        "latestPath": rel if latest_owner.get(rel) == name and is_latest else None,
                        "pinnedPath": pinned,
                    })
                    count += 1
            print(f"{name}@{source_version}: {count} schemas")

    (site / "catalog.json").write_text(json.dumps({"schemas": catalog}, indent=2) + "\n")
    build_index_html(catalog, site)
    print(f"\nwrote {len(catalog)} schemas to {site}")

    if failures:
        print("\nFAILURES:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

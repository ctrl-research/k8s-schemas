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
    # Guard on literal values: schemas that *describe* schemas (the CRD type
    # itself, JSONSchemaProps) define properties NAMED x-kubernetes-int-or-string
    # or nullable, whose values are schema dicts rather than markers.
    if out.get("x-kubernetes-int-or-string") is True or out.get("format") == "int-or-string":
        out.pop("x-kubernetes-int-or-string", None)
        out.pop("type", None)
        if out.get("format") == "int-or-string":
            out.pop("format")
        out["oneOf"] = [{"type": "string"}, {"type": "integer"}]
    # OpenAPI nullable -> JSON Schema type union
    if out.get("nullable") is True and isinstance(out.get("type"), str):
        out.pop("nullable")
        out["type"] = [out["type"], "null"]
    return out


K8S_SWAGGER = "https://raw.githubusercontent.com/kubernetes/kubernetes/{version}/api/openapi-spec/swagger.json"


def strictify(node):
    """Objects that declare properties reject unknown fields (catches typos),
    mirroring kubernetes-json-schema's -strict flavor."""
    if isinstance(node, list):
        for item in node:
            strictify(item)
    elif isinstance(node, dict):
        # require type == "object" so this never fires on a properties *map*
        # that happens to define a property named "properties"
        if node.get("type") == "object" and "properties" in node \
                and "additionalProperties" not in node:
            node["additionalProperties"] = False
        for value in node.values():
            strictify(value)
    return node


def nullify_optional(node):
    """Let optional properties accept null, as Kubernetes serializes empty
    optional fields (mirrors openapi2jsonschema's --kubernetes flag; e.g.
    CRD manifests ship `status: {conditions: null}`)."""
    if isinstance(node, list):
        for item in node:
            nullify_optional(item)
    elif isinstance(node, dict):
        # Applies to every property, required or not — the API server defaults
        # required-but-null fields (gateway-api ships `storedVersions: null`).
        # Guard: a real schema node's `type` is absent, a string, or a list
        # (after this pass rewrites an ancestor); a properties *map* that
        # defines a field named "properties" (JSONSchemaProps) also defines
        # "type" as a field, whose value is a schema dict — excluded here.
        if isinstance(node.get("properties"), dict) and not isinstance(node.get("type"), dict):
            for prop in node["properties"].values():
                if not isinstance(prop, dict):
                    continue
                prop_type = prop.get("type")
                if isinstance(prop_type, str) and prop_type != "null":
                    prop["type"] = [prop_type, "null"]
                elif isinstance(prop_type, list) and "null" not in prop_type:
                    prop["type"] = prop_type + ["null"]
        for value in node.values():
            nullify_optional(value)
    return node


def k8s_schemas(version):
    """Yield (group, kind, version, schema) for core k8s API types.

    Built from the upstream OpenAPI spec with $refs inlined; the empty core
    group publishes under core/ (e.g. core/namespace_v1.json).
    """
    with urllib.request.urlopen(K8S_SWAGGER.format(version=version), timeout=120) as resp:
        definitions = json.loads(resp.read())["definitions"]

    def inline(node, stack):
        if isinstance(node, list):
            return [inline(item, stack) for item in node]
        if not isinstance(node, dict):
            return node
        # a real reference has a string value; JSONSchemaProps defines an
        # actual *property* named "$ref" whose value is a schema dict
        if isinstance(node.get("$ref"), str):
            target = node["$ref"].removeprefix("#/definitions/")
            if target in stack:  # recursive type (e.g. JSONSchemaProps)
                return {}
            merged = {**definitions[target], **{k: v for k, v in node.items() if k != "$ref"}}
            return inline(merged, stack | {target})
        return {k: inline(v, stack) for k, v in node.items()}

    for name, definition in definitions.items():
        gvks = definition.get("x-kubernetes-group-version-kind", [])
        if len(gvks) != 1 or gvks[0]["kind"].endswith("List"):
            continue
        gvk = gvks[0]
        schema = nullify_optional(strictify(to_json_schema(inline(definition, frozenset({name})))))
        schema["$schema"] = "http://json-schema.org/draft-07/schema#"
        yield gvk["group"] or "core", gvk["kind"], gvk["version"], schema


def jsonschema_schemas(source):
    """Republish existing JSON Schema files (e.g. schemastore's kustomization)."""
    for item in source["schemas"]:
        with urllib.request.urlopen(item["url"], timeout=60) as resp:
            schema = json.loads(resp.read())
        yield item["group"], item["kind"], item["version"], schema


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
                if source["kind"] == "k8s":
                    schemas = list(k8s_schemas(source_version))
                elif source["kind"] == "jsonschema":
                    schemas = list(jsonschema_schemas(source))
                else:
                    if source["kind"] == "url":
                        docs = fetch_url_docs(source, source_version)
                    else:
                        docs = fetch_chart_docs(source, source_version)
                    crds = [d for d in docs if isinstance(d, dict) and d.get("kind") == "CustomResourceDefinition"]
                    schemas = [item for crd in crds for item in convert_crd(crd)]
            except Exception as exc:
                failures.append(f"{name}@{source_version}: {exc}")
                continue
            if not schemas:
                failures.append(f"{name}@{source_version}: no schemas found")
                continue
            for group, kind, version, schema in schemas:
                rel = f"{group}/{kind.lower()}_{version}.json"
                pinned = f"{name}/{source_version}/{rel}"
                write_schema(site / pinned, schema)
                if is_latest:
                    owner = latest_owner.setdefault(rel, name)
                    if owner == name:
                        write_schema(site / rel, schema)
                    else:
                        print(f"  note: {rel} owned by {owner}, {name} publishes pinned tier only")
                description = (schema.get("description") or "").strip()
                catalog.append({
                    "source": name, "sourceVersion": source_version,
                    "group": group, "kind": kind, "version": version,
                    "latestPath": rel if latest_owner.get(rel) == name and is_latest else None,
                    "pinnedPath": pinned,
                    "description": description[:200],
                })
            print(f"{name}@{source_version}: {len(schemas)} schemas")

    (site / "catalog.json").write_text(json.dumps({"schemas": catalog}, indent=2) + "\n")
    shutil.copy(REPO_ROOT / "assets" / "index.html", site / "index.html")
    print(f"\nwrote {len(catalog)} schemas to {site}")

    if failures:
        print("\nFAILURES:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

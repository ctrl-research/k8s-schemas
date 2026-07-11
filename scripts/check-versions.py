#!/usr/bin/env python3
"""Check sources.yaml pins against the latest upstream versions.

Resolves the newest stable version for every source natively per kind:
  url  -> GitHub releases API (latest release tag)
  helm -> the repo's index.yaml
  oci  -> registry tags list (anonymous pull token)

With --update, rewrites the renovate-managed `version:` line of each stale
source in sources.yaml and demotes the previous pin into `extraVersions`
(capped at KEEP_HISTORY entries) so already-published pinned-tier URLs keep
resolving after the bump. Prints a markdown summary either way; exits 0
whether or not updates were found (the workflow decides what to do with the
diff).
"""
import argparse
import json
import os
import pathlib
import re
import sys
import urllib.parse
import urllib.request

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
STABLE = re.compile(r"^v?\d+(\.\d+)*$")
KEEP_HISTORY = 5


def get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def semver_key(version):
    return [int(part) for part in version.lstrip("v").split(".")]


def newest(versions):
    stable = [v for v in versions if STABLE.match(v)]
    return max(stable, key=semver_key) if stable else None


def latest_github_release(source):
    if source["kind"] == "k8s":
        repo = "kubernetes/kubernetes"
    else:
        match = re.search(r"github(?:usercontent)?\.com/([^/]+/[^/]+)", source["urls"][0])
        repo = match.group(1)
    headers = {}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return get_json(f"https://api.github.com/repos/{repo}/releases/latest", headers)["tag_name"]


def latest_helm_version(source):
    with urllib.request.urlopen(f"{source['repo'].rstrip('/')}/index.yaml", timeout=30) as resp:
        index = yaml.safe_load(resp.read())
    return newest([e["version"] for e in index["entries"][source["chart"]]])


def latest_oci_version(source):
    registry, _, name = source["chart"].removeprefix("oci://").partition("/")
    if registry in ("docker.io", "registry-1.docker.io"):
        token_url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{name}:pull"
        registry = "registry-1.docker.io"
    else:
        token_url = f"https://{registry}/token?service={registry}&scope=repository:{name}:pull"
    token = get_json(token_url)["token"]
    tags = get_json(
        f"https://{registry}/v2/{name}/tags/list",
        {"Authorization": f"Bearer {token}"},
    )["tags"]
    return newest(tags)


RESOLVERS = {
    "url": latest_github_release,
    "helm": latest_helm_version,
    "oci": latest_oci_version,
    "k8s": latest_github_release,
    # schemastore republishes are rolling — always current
    "jsonschema": lambda source: source["version"],
}


def rewrite_pins(text, name, old, new, existing_extra):
    """Bump one source's version pin and demote the old pin into extraVersions."""
    history = ([old] + [v for v in existing_extra if v != old])[:KEEP_HISTORY]
    extra_block = "    extraVersions:\n" + "".join(f"      - {v}\n" for v in history)
    block = re.compile(
        rf"(?P<head>- name: {re.escape(name)}\n(?:(?!  - name: ).*\n)*?    version: ){re.escape(old)}\n"
        rf"(?:    extraVersions:\n(?:      - .*\n)*)?"
    )
    updated, count = block.subn(lambda m: f"{m.group('head')}{new}\n{extra_block}", text)
    if count != 1:
        raise RuntimeError(f"could not rewrite pins for {name} (matched {count} times)")
    return updated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true", help="rewrite stale pins in sources.yaml")
    parser.add_argument("--sources", default=str(REPO_ROOT / "sources.yaml"))
    args = parser.parse_args()

    path = pathlib.Path(args.sources)
    text = path.read_text()
    rows, errors, stale = [], [], []

    for source in yaml.safe_load(text)["sources"]:
        name, current = source["name"], source["version"]
        try:
            latest = RESOLVERS[source["kind"]](source)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            rows.append((name, current, "?", "check failed"))
            continue
        if latest and latest.lstrip("v") != current.lstrip("v"):
            stale.append((name, current, latest, source.get("extraVersions", [])))
            rows.append((name, current, latest, "stale"))
        else:
            rows.append((name, current, latest or current, "current"))

    print("| source | pinned | latest | status |")
    print("|---|---|---|---|")
    for row in rows:
        print("| " + " | ".join(row) + " |")

    if args.update and stale:
        for name, current, latest, extra in stale:
            # keep the pin's existing v-prefix style
            new = latest if current.startswith("v") == latest.startswith("v") else (
                latest.lstrip("v") if not current.startswith("v") else f"v{latest.lstrip('v')}"
            )
            text = rewrite_pins(text, name, current, new, extra)
        path.write_text(text)
        print(f"\nupdated {len(stale)} pin(s) in {path.name}", file=sys.stderr)

    if errors:
        print("\ncheck failures:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)


if __name__ == "__main__":
    main()

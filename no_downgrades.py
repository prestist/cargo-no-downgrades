#!/usr/bin/env python3
"""Refuses a Cargo.lock that pins any package below the base branch's version.

Usage: no_downgrades.py <base Cargo.lock> <head Cargo.lock>

A dependency bumped on main and then pinned back by a branch that was cut
before the bump is the usual way a downgrade slips into a merge. Only
packages the base already locks are compared, by (name, source). In addition
to the highest locked version, compare dependency edges of matching packages
so switching to an older version is caught even when both remain locked.
"""

from collections import defaultdict
import re
import sys
import tomllib


def load(path):
    with open(path, "rb") as file:
        lock = tomllib.load(file)
    packages = lock.get("package", [])
    # Older Cargo lockfiles stored the workspace package separately.
    if "root" in lock:
        packages.append(lock["root"])
    return packages


def package_key(package):
    return package["name"], package.get("source")


def locked_versions(packages):
    locked = {}
    for package in packages:
        key = package_key(package)
        version = parse(package["version"])
        locked[key] = max(locked.get(key, version), version)
    return locked


def versions(path):
    return locked_versions(load(path))


def dependency_source(source):
    # Cargo omits the precise Git revision from dependency IDs, but keeps
    # it in the package's source field. Preserve branch/tag/rev queries.
    return source.partition("#")[0] if source and source.startswith("git+") else source


def parse(version):
    # Build metadata (`1.2.3+llvm`) does not order versions.
    version, _, _ = version.partition("+")
    release, _, pre = version.partition("-")
    # SemVer 11: numeric identifiers sort numerically and below text ones;
    # otherwise use ASCII ordering, then the number of identifiers.
    identifiers = tuple(
        (0, int(part)) if part.isascii() and part.isdigit() else (1, part)
        for part in pre.split(".")
    ) if pre else ()
    # A pre-release sorts below the release it precedes.
    return tuple(int(part) for part in release.split(".")), pre == "", identifiers


def dependency_graph(packages):
    """Resolve Cargo's abbreviated dependency IDs within each lockfile."""
    by_name = defaultdict(list)
    for package in packages:
        by_name[package["name"]].append(package)

    graph = defaultdict(dict)
    for package in packages:
        edges = defaultdict(set)
        for dependency in package.get("dependencies", []):
            match = re.fullmatch(r"([^ ()]+)(?: ([^ ()]+))?(?: \(([^()]+)\))?", dependency)
            if not match:
                raise ValueError(f"invalid dependency ID: {dependency!r}")
            name, version, source = match.groups()
            candidates = [
                candidate for candidate in by_name[name]
                if (version is None or candidate["version"] == version)
                and (source is None or dependency_source(candidate.get("source"))
                     == dependency_source(source))
            ]
            if source is None and len({p["version"] for p in candidates}) == 1:
                # Path dependencies have no source even when a registry
                # package with the same name and version is also locked.
                paths = [p for p in candidates if "source" not in p]
                if paths:
                    candidates = paths
            if len(candidates) != 1:
                raise ValueError(
                    f"{package['name']}: dependency {dependency!r} resolves to "
                    f"{len(candidates)} packages; expected exactly one"
                )
            resolved = candidates[0]
            edges[package_key(resolved)].add(parse(resolved["version"]))
        group = graph[package_key(package)]
        if package["version"] in group:
            raise ValueError(f"duplicate package: {package['name']} {package['version']}")
        group[package["version"]] = edges
    return graph


def matching_parents(base, head):
    """Match exact versions first, then a single remaining version change."""
    for version in sorted(base.keys() & head.keys()):
        yield version, version
    removed = base.keys() - head.keys()
    added = head.keys() - base.keys()
    if len(removed) == len(added) == 1:
        yield next(iter(removed)), next(iter(added))


def find_downgrades(base_packages, head_packages):
    base = locked_versions(base_packages)
    head = locked_versions(head_packages)
    downgrades = [
        (key[0], was, head[key])
        for key, was in base.items()
        if key in head and head[key] < was
    ]
    # Build both graphs in full, even for unchanged or removed packages, so
    # malformed/ambiguous dependency IDs cannot silently disable the check.
    base_graph = dependency_graph(base_packages)
    head_graph = dependency_graph(head_packages)
    for parent, base_versions in base_graph.items():
        head_versions = head_graph.get(parent, {})
        for old_version, new_version in matching_parents(base_versions, head_versions):
            old_edges = base_versions[old_version]
            new_edges = head_versions[new_version]
            for dependency, old in old_edges.items():
                new = new_edges.get(dependency, set())
                removed, added = sorted(old - new), sorted(new - old)
                # A pure addition/removal is not a switch. When counts differ,
                # the lockfile alone cannot distinguish switches from removals
                # plus additions. Do not guess a correspondence in that case.
                if len(removed) != len(added):
                    continue
                for was, now in zip(removed, added):
                    if now < was:
                        parent_label = f"{parent[0]} {old_version}"
                        if old_version != new_version:
                            parent_label += f" -> {new_version}"
                        label = f"{parent_label} -> {dependency[0]}"
                        downgrades.append((label, was, now))
    return downgrades


def main(base_path, head_path):
    downgrades = find_downgrades(load(base_path), load(head_path))
    for name, was, now in downgrades:
        print(f"{name}: {show(was)} on the base branch, {show(now)} here")
    if downgrades:
        print("Cargo.lock downgrades a package the base branch already had; rebase and re-pin.")
        return 1
    return 0


def show(version):
    release, is_release, pre = version
    text = ".".join(map(str, release))
    suffix = ".".join(str(value) for _, value in pre)
    return text if is_release else f"{text}-{suffix}"


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))

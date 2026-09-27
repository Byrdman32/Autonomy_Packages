#!/usr/bin/env python3
"""Builds the Autonomy dependency packages and the flat apt repository that serves them.

Standard library only (Python 3.11+ for tomllib).

    packages.py plan  --arch ARCH [--index Packages]            what this run needs to build, in build order
    packages.py build --arch ARCH [--index Packages] --out DIR  build those packages (as root, in the builder image)
                      [--repo-url URL --keyring FILE]           where to install already-published dependencies from
    packages.py index [--index Packages] --debs DIR --out DIR   merge new .debs into the index; write Packages and Release

A recipe is packages/<name>.toml and becomes the package <package_prefix><name> (repo.toml), version
<version>-<revision>. A package is built when that name, version and architecture isn't in the published index yet.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHITECTURES = ("amd64", "arm64")
DEPENDS_RE = re.compile(r"^\s*(?P<name>[a-z0-9][a-z0-9+.-]*)\s*(?:\(\s*=\s*(?P<version>[^)\s]+)\s*\))?\s*$")


class PackagingError(Exception):
    """A problem with the recipes or the published index that needs a person to fix it."""


# ----------------------------------------------------------------------------------------------------------------------
# Recipes
# ----------------------------------------------------------------------------------------------------------------------


@dataclass
class Recipe:
    name: str  # recipe name: the file name without .toml
    package: str  # Debian package name
    description: str
    version: str
    revision: int
    source: str
    tag: str
    architecture: str = "any"  # "any" (built per architecture) or "all"
    prefix: str = "/usr/local"
    build_type: str = "Release"
    cflags: str = ""
    depends: list[str] = field(default_factory=list)  # other recipes, pinned to their exact version
    apt: list[str] = field(default_factory=list)  # build-time apt packages
    cmake_args: list[str] = field(default_factory=list)

    @property
    def deb_version(self) -> str:
        return f"{self.version}-{self.revision}"

    def deb_architecture(self, arch: str) -> str:
        return "all" if self.architecture == "all" else arch

    def file_name(self, arch: str) -> str:
        return f"{self.package}_{self.deb_version}_{self.deb_architecture(arch)}.deb"


def load_settings(root: Path = ROOT) -> dict:
    return tomllib.loads((root / "repo.toml").read_text())


def load_recipes(root: Path = ROOT) -> dict[str, Recipe]:
    settings = load_settings(root)
    recipes = {}
    for path in sorted((root / "packages").glob("*.toml")):
        data = tomllib.loads(path.read_text())
        name = path.stem
        try:
            recipe = Recipe(name=name, package=settings["package_prefix"] + name, **data)
        except TypeError as error:
            raise PackagingError(f"{path.name}: {error}") from None
        if recipe.architecture not in ("any", "all"):
            raise PackagingError(f"{path.name}: architecture must be 'any' or 'all'")
        if not re.fullmatch(r"[A-Za-z0-9.]+", recipe.version) or recipe.revision < 1:
            # Plain versions only: GitHub renames release assets containing '+' or '~'.
            raise PackagingError(f"{path.name}: version must be letters, digits and dots, and revision >= 1")
        recipes[name] = recipe
    for recipe in recipes.values():
        for dependency in recipe.depends:
            if dependency not in recipes:
                raise PackagingError(f"{recipe.name}.toml depends on unknown recipe {dependency!r}")
    return recipes


def build_order(recipes: dict[str, Recipe]) -> list[Recipe]:
    """Recipes with each one after everything it depends on; ties in name order."""
    ordered: list[Recipe] = []
    state: dict[str, str] = {}

    def visit(name: str, chain: list[str]) -> None:
        if state.get(name) == "done":
            return
        if state.get(name) == "visiting":
            raise PackagingError("dependency cycle: " + " -> ".join(chain + [name]))
        state[name] = "visiting"
        for dependency in sorted(recipes[name].depends):
            visit(dependency, chain + [name])
        state[name] = "done"
        ordered.append(recipes[name])

    for name in sorted(recipes):
        visit(name, [])
    return ordered


# ----------------------------------------------------------------------------------------------------------------------
# Published index
# ----------------------------------------------------------------------------------------------------------------------


def parse_index(text: str) -> list[dict[str, str]]:
    """Stanzas of a Debian Packages file, with continuation lines joined."""
    stanzas = []
    for block in re.split(r"\n\s*\n", text.strip()):
        stanza: dict[str, str] = {}
        key = None
        for line in block.splitlines():
            if line[:1] in (" ", "\t") and key is not None:
                stanza[key] += "\n" + line
            elif ":" in line:
                key, value = line.split(":", 1)
                stanza[key] = value.strip()
        if stanza.get("Package"):
            stanzas.append(stanza)
    return stanzas


def render_index(stanzas: list[dict[str, str]]) -> str:
    return "\n".join("".join(f"{key}: {value}\n" for key, value in stanza.items()) for stanza in stanzas)


def pinned_depends(stanza: dict[str, str]) -> dict[str, str]:
    """Package -> exact version for the `(= version)` entries of a stanza's Depends."""
    pins = {}
    for entry in stanza.get("Depends", "").split(","):
        match = DEPENDS_RE.match(entry.split("|")[0])
        if match and match.group("version"):
            pins[match.group("name")] = match.group("version")
    return pins


def read_index(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.is_file():
        return []
    return parse_index(path.read_text())


# ----------------------------------------------------------------------------------------------------------------------
# Plan
# ----------------------------------------------------------------------------------------------------------------------


@dataclass
class Plan:
    build: list[Recipe]  # to build in this run, in order
    published: list[Recipe]  # already in the index for this architecture

    def to_json(self, arch: str) -> dict:
        return {
            "arch": arch,
            "build": [{"name": r.name, "package": r.package, "version": r.deb_version, "file": r.file_name(arch)} for r in self.build],
            "published": [r.package for r in self.published],
        }


def plan(recipes: dict[str, Recipe], index: list[dict[str, str]], arch: str, include_all: bool = True) -> Plan:
    """What an `arch` run builds. `all` packages are built by the amd64 run only (include_all).

    Fails if a published package was built against a different dependency version than its recipe now pins:
    its revision must go up so it's rebuilt.
    """
    if arch not in ARCHITECTURES:
        raise PackagingError(f"unknown architecture {arch!r}")
    published = {(s["Package"], s.get("Version", ""), s.get("Architecture", "")): s for s in index}
    to_build: list[Recipe] = []
    done: list[Recipe] = []
    problems = []
    for recipe in build_order(recipes):
        deb_arch = recipe.deb_architecture(arch)
        stanza = published.get((recipe.package, recipe.deb_version, deb_arch))
        if stanza is None:
            if deb_arch != "all" or include_all:
                to_build.append(recipe)
            continue
        pins = pinned_depends(stanza)
        for dependency in recipe.depends:
            wanted = recipes[dependency]
            if pins.get(wanted.package) != wanted.deb_version:
                problems.append(
                    f"{recipe.package} {recipe.deb_version} ({deb_arch}) was built against {wanted.package} "
                    f"{pins.get(wanted.package, '(none)')}, but {dependency}.toml is now {wanted.deb_version}: "
                    f"bump the revision in packages/{recipe.name}.toml"
                )
        done.append(recipe)
    if problems:
        raise PackagingError("\n".join(problems))
    return Plan(to_build, done)


# ----------------------------------------------------------------------------------------------------------------------
# Build
# ----------------------------------------------------------------------------------------------------------------------


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+ " + " ".join(command), flush=True)
    return subprocess.run(command, check=True, **kwargs)


def apt_install(packages: list[str]) -> None:
    if packages:
        run(["apt-get", "install", "-y", "--no-install-recommends", *packages], env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})


def configure_apt_source(repo_url: str, keyring: Path) -> None:
    """Adds the published repository so already-published dependencies install from it."""
    Path("/etc/apt/keyrings").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(keyring, "/etc/apt/keyrings/autonomy.gpg")
    Path("/etc/apt/sources.list.d/autonomy.list").write_text(f"deb [signed-by=/etc/apt/keyrings/autonomy.gpg] {repo_url.rstrip('/')}/ ./\n")
    run(["apt-get", "update"])


def cmake_command(recipe: Recipe, source: Path, build: Path) -> list[str]:
    command = [
        "cmake", "-S", str(source), "-B", str(build), "-G", "Ninja",
        f"-DCMAKE_BUILD_TYPE={recipe.build_type}", f"-DCMAKE_INSTALL_PREFIX={recipe.prefix}",
        "-DCMAKE_C_COMPILER=gcc-13", "-DCMAKE_CXX_COMPILER=g++-13", "-DCMAKE_CXX_STANDARD=20", "-DBUILD_TESTING=OFF",
    ]  # fmt: skip
    if recipe.prefix != "/usr/local":
        command.append(f"-DCMAKE_PREFIX_PATH={recipe.prefix}")
    if recipe.cflags:
        command += [
            f"-DCMAKE_C_FLAGS={recipe.cflags}", f"-DCMAKE_CXX_FLAGS={recipe.cflags}",
            f"-DCMAKE_EXE_LINKER_FLAGS={recipe.cflags}", f"-DCMAKE_SHARED_LINKER_FLAGS={recipe.cflags}",
        ]  # fmt: skip
    return command + recipe.cmake_args


def elf_files(stage: Path) -> list[Path]:
    found = []
    for path in stage.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                if handle.read(4) == b"\x7fELF":
                    found.append(path)
    return sorted(found)


def shared_library_depends(stage: Path, prefix: str) -> list[str]:
    """System package dependencies of the staged binaries, from dpkg-shlibdeps.

    Libraries from other Autonomy packages have no dependency information and are skipped here; the recipe's
    `depends` adds those, pinned exactly.
    """
    binaries = elf_files(stage)
    if not binaries:
        return []
    with tempfile.TemporaryDirectory() as scratch:
        (Path(scratch) / "debian").mkdir()
        (Path(scratch) / "debian" / "control").write_text("Source: autonomy\n\nPackage: autonomy\nArchitecture: any\n")
        lib_dirs = [f"-l{path}" for path in {binary.parent for binary in binaries}]
        result = run(
            ["dpkg-shlibdeps", "-O", "--ignore-missing-info", "--warnings=0", *lib_dirs, *map(str, binaries)],
            cwd=scratch, capture_output=True, text=True,
        )  # fmt: skip
    for line in result.stdout.splitlines():
        if line.startswith("shlibs:Depends="):
            return [entry.strip() for entry in line.split("=", 1)[1].split(",") if entry.strip()]
    return []


def installed_size_kib(stage: Path) -> int:
    return sum(path.lstat().st_size for path in stage.rglob("*") if path.is_file() or path.is_symlink()) // 1024 + 1


def control_file(recipe: Recipe, recipes: dict[str, Recipe], arch: str, settings: dict, system_depends: list[str], size: int) -> str:
    depends = [f"{recipes[name].package} (= {recipes[name].deb_version})" for name in recipe.depends] + system_depends
    homepage = recipe.source.removesuffix(".git")
    fields = {
        "Package": recipe.package,
        "Version": recipe.deb_version,
        "Architecture": recipe.deb_architecture(arch),
        "Maintainer": settings["maintainer"],
        "Installed-Size": str(size),
        "Depends": ", ".join(depends),
        "Section": "libdevel",
        "Priority": "optional",
        "Homepage": homepage,
        "Description": f"{recipe.description}\n Built from {homepage} at {recipe.tag.format(version=recipe.version)},\n installed under {recipe.prefix}.",
    }
    return "".join(f"{key}: {value}\n" for key, value in fields.items() if value)


LDCONFIG_SCRIPT = """#!/bin/sh
set -e
if [ "$1" = "{action}" ]; then
    ldconfig
fi
"""


def package(recipe: Recipe, recipes: dict[str, Recipe], arch: str, settings: dict, stage: Path, out: Path) -> Path:
    """Turns an installed tree into a .deb."""
    if recipe.architecture == "all" and (binaries := elf_files(stage)):
        found = ", ".join(str(path.relative_to(stage)) for path in binaries[:5])
        raise PackagingError(f"{recipe.name} is architecture 'all' but installs compiled files: {found}")
    system_depends = shared_library_depends(stage, recipe.prefix)
    debian = stage / "DEBIAN"
    debian.mkdir()
    sums = []
    for path in sorted(stage.rglob("*")):
        if path.is_file() and not path.is_symlink() and debian not in path.parents:
            sums.append(f"{hashlib.md5(path.read_bytes()).hexdigest()}  {path.relative_to(stage)}\n")
    (debian / "md5sums").write_text("".join(sums))
    if any(path.suffix == ".so" or ".so." in path.name for path in stage.rglob("*")):
        for script, action in (("postinst", "configure"), ("postrm", "remove")):
            (debian / script).write_text(LDCONFIG_SCRIPT.replace("{action}", action))
            (debian / script).chmod(0o755)
    (debian / "control").write_text(control_file(recipe, recipes, arch, settings, system_depends, installed_size_kib(stage)))
    out.mkdir(parents=True, exist_ok=True)
    deb = out / recipe.file_name(arch)
    run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(stage), str(deb)])
    return deb


def build_recipe(recipe: Recipe, recipes: dict[str, Recipe], arch: str, settings: dict, out: Path, work: Path) -> Path:
    apt_install(recipe.apt)
    source, build, stage = work / "src", work / "build", work / "stage"
    tag = recipe.tag.format(version=recipe.version)
    run(["git", "clone", "--quiet", "--depth", "1", "--branch", tag, recipe.source, str(source)])
    run(cmake_command(recipe, source, build))
    run(["cmake", "--build", str(build), "--parallel", str(os.cpu_count() or 2)])
    run(["cmake", "--install", str(build)], env={**os.environ, "DESTDIR": str(stage)})
    return package(recipe, recipes, arch, settings, stage, out)


def build(args: argparse.Namespace) -> int:
    recipes = load_recipes()
    settings = load_settings()
    work_plan = plan(recipes, read_index(args.index), args.arch, include_all=args.arch == "amd64")
    if args.only:
        work_plan.build = [recipe for recipe in work_plan.build if recipe.name in args.only]
    if not work_plan.build:
        print("Nothing to build.")
        return 0
    building = {recipe.name for recipe in work_plan.build}
    if args.repo_url and work_plan.published:
        configure_apt_source(args.repo_url, args.keyring)
    else:
        run(["apt-get", "update"])
    built: dict[str, Path] = {}
    for recipe in work_plan.build:
        for name in recipe.depends:
            dependency = recipes[name]
            if name in built:
                apt_install([str(built[name].resolve())])
            elif name not in building:
                apt_install([f"{dependency.package}={dependency.deb_version}"])
            else:
                raise PackagingError(f"{name} wasn't built before {recipe.name}")
        with tempfile.TemporaryDirectory(prefix=f"{recipe.name}-") as work:
            built[recipe.name] = build_recipe(recipe, recipes, args.arch, settings, args.out, Path(work))
        print(f"Built {built[recipe.name].name}", flush=True)
    return 0


# ----------------------------------------------------------------------------------------------------------------------
# Index
# ----------------------------------------------------------------------------------------------------------------------


def deb_stanza(deb: Path) -> dict[str, str]:
    """A Packages stanza for one .deb: its control fields plus where it is and its hashes."""
    control = subprocess.run(["dpkg-deb", "--field", str(deb)], check=True, capture_output=True, text=True).stdout
    stanza = parse_index(control)[0]
    data = deb.read_bytes()
    stanza.pop("Description", None)
    description = parse_index(control)[0].get("Description", "")
    stanza.update(
        {
            "Filename": f"./{deb.name}",
            "Size": str(len(data)),
            "MD5sum": hashlib.md5(data).hexdigest(),
            "SHA256": hashlib.sha256(data).hexdigest(),
            "Description": description,
        }
    )
    return stanza


def merge_index(existing: list[dict[str, str]], new: list[dict[str, str]]) -> list[dict[str, str]]:
    """The index with the new packages added; published packages are never replaced."""
    def key(stanza: dict[str, str]) -> tuple[str, str, str]:
        return stanza["Package"], stanza.get("Version", ""), stanza.get("Architecture", "")

    merged = {key(stanza): stanza for stanza in existing}
    for stanza in new:
        if key(stanza) in merged:
            raise PackagingError(f"{stanza['Package']} {stanza.get('Version')} ({stanza.get('Architecture')}) is already published")
        merged[key(stanza)] = stanza
    return [merged[k] for k in sorted(merged)]


def release_file(settings: dict, files: dict[str, bytes], date: str) -> str:
    lines = [
        f"Origin: {settings['origin']}",
        f"Label: {settings['label']}",
        f"Date: {date}",
        "Architectures: " + " ".join((*ARCHITECTURES, "all")),
    ]
    for header, digest in (("MD5Sum", hashlib.md5), ("SHA256", hashlib.sha256)):
        lines.append(f"{header}:")
        lines += [f" {digest(data).hexdigest()} {len(data):>10} {name}" for name, data in sorted(files.items())]
    return "\n".join(lines) + "\n"


def index(args: argparse.Namespace) -> int:
    from email.utils import formatdate

    settings = load_settings()
    new = [deb_stanza(deb) for deb in sorted(args.debs.glob("*.deb"))]
    stanzas = merge_index(read_index(args.index), new)
    packages_text = render_index(stanzas).encode()
    files = {"Packages": packages_text, "Packages.gz": gzip.compress(packages_text, mtime=0)}
    args.out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (args.out / name).write_bytes(data)
    (args.out / "Release").write_text(release_file(settings, files, formatdate(usegmt=True)))
    print(json.dumps({"added": [f"{s['Package']} {s['Version']} {s['Architecture']}" for s in new], "total": len(stanzas)}, indent=2))
    return 0


# ----------------------------------------------------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    plan_command = commands.add_parser("plan", help="list what an architecture's run builds")
    build_command = commands.add_parser("build", help="build the missing packages for an architecture")
    for command in (plan_command, build_command):
        command.add_argument("--arch", choices=ARCHITECTURES, required=True)
        command.add_argument("--index", type=Path, help="the published Packages file; missing means nothing is published")
    build_command.add_argument("--out", type=Path, required=True)
    build_command.add_argument("--repo-url", help="published repository, for dependencies that aren't rebuilt")
    build_command.add_argument("--keyring", type=Path, default=ROOT / "keys" / "autonomy-archive-keyring.gpg")
    build_command.add_argument("--only", nargs="+", metavar="RECIPE", help="build only these (for local testing)")

    index_command = commands.add_parser("index", help="add built packages to the index")
    index_command.add_argument("--index", type=Path)
    index_command.add_argument("--debs", type=Path, required=True)
    index_command.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = plan(load_recipes(), read_index(args.index), args.arch, include_all=args.arch == "amd64")
            print(json.dumps(result.to_json(args.arch), indent=2))
            return 0
        if args.command == "build":
            return build(args)
        return index(args)
    except PackagingError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

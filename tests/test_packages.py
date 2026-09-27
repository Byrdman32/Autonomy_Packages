"""Tests for tools/packages.py."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

import packages  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def write_repo(tmp_path, recipes: dict[str, str]) -> Path:
    (tmp_path / "packages").mkdir()
    (tmp_path / "repo.toml").write_text('maintainer = "M <m@x>"\norigin = "O"\nlabel = "L"\npackage_prefix = "autonomy-"\nrelease_tag = "apt"\n')
    for name, body in recipes.items():
        (tmp_path / "packages" / f"{name}.toml").write_text('description = "d"\nsource = "https://x/y.git"\ntag = "v{version}"\n' + body)
    return tmp_path


BASE = 'version = "1.0"\nrevision = 1\n'


def stanza(package, version, arch, depends=""):
    fields = {"Package": package, "Version": version, "Architecture": arch}
    if depends:
        fields["Depends"] = depends
    return fields


def test_repository_recipes_load_and_order():
    recipes = packages.load_recipes(ROOT)
    order = [recipe.name for recipe in packages.build_order(recipes)]
    assert order.index("abseil") < order.index("protobuf")
    assert order.index("abseil-tsan") < order.index("protobuf-tsan")
    assert order.index("libzmq") < order.index("cppzmq")
    assert recipes["protobuf"].package == "autonomy-protobuf"
    assert recipes["quill"].file_name("arm64") == "autonomy-quill_13.0.0-1_all.deb"


def test_cycle_and_unknown_dependency_are_errors(tmp_path):
    write_repo(tmp_path, {"a": BASE + 'depends = ["b"]\n', "b": BASE + 'depends = ["a"]\n'})
    with pytest.raises(packages.PackagingError, match="cycle"):
        packages.build_order(packages.load_recipes(tmp_path))
    (tmp_path / "packages" / "b.toml").unlink()
    with pytest.raises(packages.PackagingError, match="unknown recipe 'b'"):
        packages.load_recipes(tmp_path)


def test_bad_version_and_unknown_keys_are_errors(tmp_path):
    write_repo(tmp_path, {"a": 'version = "1.0+git"\nrevision = 1\n'})
    with pytest.raises(packages.PackagingError, match="letters, digits and dots"):
        packages.load_recipes(tmp_path)
    (tmp_path / "packages" / "a.toml").write_text('description = "d"\nsource = "s"\ntag = "t"\n' + BASE + "colour = 1\n")
    with pytest.raises(packages.PackagingError, match="colour"):
        packages.load_recipes(tmp_path)


def test_plan_builds_only_missing_packages(tmp_path):
    write_repo(tmp_path, {"base": BASE, "lib": BASE + 'depends = ["base"]\n', "headers": BASE + 'architecture = "all"\n'})
    recipes = packages.load_recipes(tmp_path)
    index = [stanza("autonomy-base", "1.0-1", "amd64")]

    amd64 = packages.plan(recipes, index, "amd64")
    assert [recipe.name for recipe in amd64.build] == ["headers", "lib"]
    assert [recipe.name for recipe in amd64.published] == ["base"]

    # arm64 has nothing published yet, and leaves the architecture-independent package to the amd64 run.
    arm64 = packages.plan(recipes, index, "arm64", include_all=False)
    assert [recipe.name for recipe in arm64.build] == ["base", "lib"]


def test_plan_rejects_packages_built_against_an_old_dependency(tmp_path):
    write_repo(tmp_path, {"base": 'version = "2.0"\nrevision = 1\n', "lib": BASE + 'depends = ["base"]\n'})
    recipes = packages.load_recipes(tmp_path)
    index = [stanza("autonomy-lib", "1.0-1", "amd64", "autonomy-base (= 1.0-1), libc6 (>= 2.34)")]
    with pytest.raises(packages.PackagingError, match=r"bump the revision in packages/lib.toml"):
        packages.plan(recipes, index, "amd64")

    index[0]["Depends"] = "autonomy-base (= 2.0-1), libc6 (>= 2.34)"
    assert [recipe.name for recipe in packages.plan(recipes, index, "amd64").build] == ["base"]


def test_index_round_trip_and_depends_pins():
    text = "Package: a\nVersion: 1-1\nDepends: autonomy-b (= 2.0-1), libc6 (>= 2.34) | libc7\nDescription: short\n longer line\n\nPackage: c\nVersion: 3-1\n"
    stanzas = packages.parse_index(text)
    assert stanzas[0]["Description"] == "short\n longer line"
    assert packages.pinned_depends(stanzas[0]) == {"autonomy-b": "2.0-1"}
    assert packages.parse_index(packages.render_index(stanzas)) == stanzas


def test_merge_index_never_replaces_published_packages():
    existing = [stanza("autonomy-a", "1.0-1", "amd64")]
    merged = packages.merge_index(existing, [stanza("autonomy-a", "1.0-1", "arm64"), stanza("autonomy-a", "1.1-1", "amd64")])
    assert [(s["Version"], s["Architecture"]) for s in merged] == [("1.0-1", "amd64"), ("1.0-1", "arm64"), ("1.1-1", "amd64")]
    with pytest.raises(packages.PackagingError, match="already published"):
        packages.merge_index(existing, [stanza("autonomy-a", "1.0-1", "amd64")])


def test_release_file_hashes_the_indexes():
    release = packages.release_file({"origin": "O", "label": "L"}, {"Packages": b"abc"}, "Sat, 01 Jan 2028 00:00:00 GMT")
    assert "Origin: O\nLabel: L\n" in release
    assert " ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad          3 Packages" in release


def test_cmake_command_for_a_sanitizer_prefix():
    recipes = packages.load_recipes(ROOT)
    command = packages.cmake_command(recipes["protobuf-tsan"], Path("/s"), Path("/b"))
    assert "-DCMAKE_INSTALL_PREFIX=/opt/sanitizers/tsan" in command
    assert "-DCMAKE_PREFIX_PATH=/opt/sanitizers/tsan" in command
    assert "-DCMAKE_CXX_FLAGS=-fsanitize=thread" in command
    assert "-DCMAKE_PREFIX_PATH=/opt/sanitizers/tsan" not in packages.cmake_command(recipes["protobuf"], Path("/s"), Path("/b"))


def test_architecture_all_packages_cannot_contain_binaries(tmp_path):
    write_repo(tmp_path, {"headers": BASE + 'architecture = "all"\n'})
    recipe = packages.load_recipes(tmp_path)["headers"]
    stage = tmp_path / "stage"
    (stage / "usr/local/lib").mkdir(parents=True)
    (stage / "usr/local/lib/libx.so").write_bytes(b"\x7fELF" + b"\0" * 12)
    with pytest.raises(packages.PackagingError, match="architecture 'all' but installs compiled files: usr/local/lib/libx.so"):
        packages.package(recipe, {"headers": recipe}, "amd64", packages.load_settings(tmp_path), stage, tmp_path / "out")


def test_build_makes_a_private_copy_of_unpublished_all_dependencies(tmp_path, monkeypatch):
    # The arm64 run doesn't publish 'all' packages, but can still need one that the amd64 run hasn't published yet.
    write_repo(tmp_path, {"headers": BASE + 'architecture = "all"\n', "lib": BASE + 'depends = ["headers"]\n'})
    events = []

    def fake_build(recipe, recipes, arch, settings, out, work):
        events.append(("build", recipe.name, out))
        return out / recipe.file_name(arch)

    monkeypatch.setattr(packages, "build_recipe", fake_build)
    monkeypatch.setattr(packages, "apt_install", lambda names: events.append(("install", names[0].rsplit("/", 1)[-1])))
    monkeypatch.setattr(packages, "run", lambda command, **kwargs: None)

    out = tmp_path / "dist"
    args = packages.argparse.Namespace(arch="arm64", index=None, only=None, repo_url=None, keyring=None, out=out)
    assert packages.build(args, root=tmp_path) == 0

    assert [event[:2] for event in events] == [
        ("build", "headers"),
        ("install", "autonomy-headers_1.0-1_all.deb"),
        ("build", "lib"),
    ]
    assert events[0][2] != out  # the private copy isn't part of the run's output
    assert events[2][2] == out


def test_build_installs_published_dependencies_from_the_repository(tmp_path, monkeypatch):
    write_repo(tmp_path, {"base": BASE, "lib": BASE + 'depends = ["base"]\n'})
    (tmp_path / "Packages").write_text("Package: autonomy-base\nVersion: 1.0-1\nArchitecture: amd64\n")
    events = []
    monkeypatch.setattr(packages, "build_recipe", lambda recipe, recipes, arch, settings, out, work: events.append(("build", recipe.name)) or out / "x.deb")
    monkeypatch.setattr(packages, "apt_install", lambda names: events.append(("install", names[0])))
    monkeypatch.setattr(packages, "run", lambda command, **kwargs: None)

    args = packages.argparse.Namespace(arch="amd64", index=tmp_path / "Packages", only=None, repo_url=None, keyring=None, out=tmp_path / "dist")
    assert packages.build(args, root=tmp_path) == 0
    assert events == [("install", "autonomy-base=1.0-1"), ("build", "lib")]

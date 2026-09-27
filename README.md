# Autonomy_Packages

Prebuilt Debian packages of the C++ dependencies that Autonomy_Services builds against, for Ubuntu 24.04 on
amd64 and arm64 (Jetson Orin). Docker images and machines install them with apt instead of compiling them, so
changing one pinned version rebuilds one package, not all of them.

| Package | What it is |
| --- | --- |
| `autonomy-abseil` | Abseil (shared) |
| `autonomy-protobuf` | protobuf runtime and `protoc` (shared) |
| `autonomy-libzmq`, `autonomy-cppzmq` | ZeroMQ and its C++ binding |
| `autonomy-quill` | Quill logging (header-only) |
| `autonomy-eigen` | Eigen (header-only) |
| `autonomy-geographiclib` | GeographicLib (shared) |
| `autonomy-googletest` | GoogleTest and GoogleMock (static) |
| `autonomy-opencv` | OpenCV without CUDA, GUI or FFmpeg (shared) |
| `autonomy-rovecomm` | RoveComm_CPP (static) and the RoveComm `manifest.json`, in `/opt/rovecomm` |
| `autonomy-abseil-tsan`, `autonomy-protobuf-tsan` | Abseil and protobuf built with ThreadSanitizer, in `/opt/sanitizers/tsan` |

Everything installs under `/usr/local` unless noted. Versions are `<upstream version>-<revision>`, e.g.
`autonomy-protobuf 36.2-1`.

## Using the repository

```bash
sudo install -d /etc/apt/keyrings
sudo curl -fsSL -o /etc/apt/keyrings/autonomy.gpg \
    https://github.com/Byrdman32/Autonomy_Packages/releases/download/apt/autonomy-archive-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/autonomy.gpg] https://github.com/Byrdman32/Autonomy_Packages/releases/download/apt ./" \
    | sudo tee /etc/apt/sources.list.d/autonomy.list
sudo apt-get update
sudo apt-get install autonomy-protobuf="36.2-*"    # the newest revision of 36.2
```

Dependencies between the packages resolve automatically: installing `autonomy-protobuf` brings the exact
`autonomy-abseil` it was built against.

## How it works

- Each `packages/<name>.toml` is a recipe: upstream source and tag, version, revision, CMake arguments, and the
  other recipes it depends on. `repo.toml` holds the settings shared by all of them.
- `tools/packages.py` builds a recipe into a `.deb` (`build`), works out which packages are missing from the
  published index (`plan`), and adds new packages to the index (`index`). The build runs in
  `docker/builder.Dockerfile`, whose Ubuntu, GCC and CMake match the images that install the packages.
- The repository is a flat apt repository stored as the assets of the `apt` release: every `.deb`, the
  `Packages` index, and a `Release` file signed with the repository key. Packages are only ever added, so an old
  Autonomy_Services tag can still install what it was built with.
- CI builds only what isn't published yet, one job per package and architecture, all at once, on native amd64
  and arm64 runners; header-only packages are built once as `all`. A job whose dependency isn't published yet
  builds a private copy of it to build against, so no job waits for another. Slow recipes (`weight` in the
  recipe) start first. Pull requests build and test; merging to `develop` publishes, only if every job passed.

## Changing a package

- **New upstream version:** change `version` and set `revision = 1`.
- **Packaging change for the same version** (CMake arguments, a fix): increase `revision`.
- **Upstream has no tag for the commit you need:** set `commit` (the full SHA) instead of `tag`, and a version
  like `25.2.3.33` (the last tag plus the commit count from `git describe`).
- **Upstream needs a fix to build here:** add a patch to `packages/patches/` with a first line saying why, and
  list it in the recipe's `patches`. `submodules = true` and `install_files` cover submodule sources and files
  upstream doesn't install.
- **A dependency changed:** increase `revision` in every recipe that depends on it. CI fails with the recipes to
  bump if you forget, since those packages were built against the old version.

To build locally:

```bash
docker build -f docker/builder.Dockerfile -t autonomy-packages-builder .
docker run --rm -v "$PWD:/repo" -w /repo autonomy-packages-builder \
    python3 tools/packages.py build --arch amd64 --out dist --only quill
```

## The signing key

CI signs the index with the private key in the `APT_SIGNING_KEY` secret; `keys/autonomy-archive-keyring.gpg`
is the matching public key that consumers trust. The publish job checks that the two match.

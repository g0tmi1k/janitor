The Janitor sits atop a number of other projects, and those are where
most of the interesting things happen. You may want to check out one of them.
They're probably also easier to setup and run, unlike the Janitor.

Environment
===========

It is recommended to use Debian Testing as the base OS/chroot.
Alternatively using the latest [Ubuntu](https://packages.ubuntu.com/search?keywords=rustc) LTS version
(or even [Debian](https://tracker.debian.org/pkg/rustc) stable) could be done
but will require installing the latest `cargo`, and therefore `rustc`, outside
of the OS network apt package repos as their versions may be too dated as a
result not supported.

Mostly you can use pip to install Python-based dependencies. In addition to
those, you'll also want to install various other bits of software.
On a Debian-based OS, run:

```console
$ sudo apt install \
    git \
    libgpgme-dev \
    libssl-dev \
    pkg-config \
    protobuf-compiler \
    python3-pip
$ sudo apt install \
    curl \
  && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh \
  && . "$HOME/.cargo/env"
```
<!--
If updating the packages above, check:
- ./.github/workflows/python-package.yml
- ./CONTRIBUTING.md
- ./Dockerfile_*

rustup should be in Debian 13 (https://tracker.debian.org/pkg/rustup)
  Switch to using this package when its out (drop curl command and package)
  https://wiki.debian.org/Rust

Debian "bookworm" 12 is the current stable version which has rustc@1.63.0
> lock file version `4` was found, but this version of Cargo does not understand this lock file, perhaps Cargo needs to be updated?

Ubuntu "Noble Numbat" 24.04 is the current LTS version which has rustc@1.75.0
> lock file version 4 requires `-Znext-lockfile-bump`

GitHub Actions (SaaS) uses package(s) outside of the standard network apt repos:
> $ dpkg -l | grep "cargo\|rustc" -> "empty"
> $ cargo --version -> 1.84.1 (66221abde 2024-11-19) // $ rustc --version -> 1.84.1 (e71f9a9a9 2025-01-27)

REF: https://github.com/rust-lang/cargo/issues/14655#issuecomment-2400237392
> Lockfile v4 has been stable since Rust 1.78.
-->

For example, to create a development environment:

```console
$ sudo apt install \
    python3-venv
$ git clone https://github.com/jelmer/janitor.git
$ cd janitor/
$ python3 -m venv .venv
$ cp -v ./scripts/* ./.venv/bin/
$ . ./.venv/bin/activate
$ pip3 install --editable .[dev,debian]
```

Containers
===========

There are various Dockerfiles for each service of Janitor which can be built
by doing:

```console
$ sudo apt install \
    buildah  \
    docker.io
$ make docker-all
```

#!/usr/bin/python3

import argparse
import os
import pwd
import shlex
import shutil
import subprocess
import tempfile

from iniparse import RawConfigParser
from janitor.config import read_config, get_distribution


def create_chroot(distro, sbuild_path, suites, sbuild_arch, include=[],
                  setup_hooks=[]):
    cmd = ["mmdebstrap", "--variant=buildd", distro.name, sbuild_path,
           distro.archive_mirror_uri, '--mode=unshare',
           '--arch=%s' % sbuild_arch]
    cmd.append("--components=%s" % ','.join(distro.component))
    if include:
        cmd.append("--include=%s" % ','.join(include))
    for name in distro.extra:
        cmd.append("--extra-repository=deb %s %s %s" % (
            distro.archive_mirror_uri, name, ' '.join(distro.component)))

    for setup_hook in setup_hooks:
        cmd.append('--setup-hook=%s' % setup_hook)

    print(shlex.join(cmd))
    subprocess.check_call(cmd)

    ext = os.path.splitext(sbuild_path)[1]
    dirname, basename = os.path.split(sbuild_path)
    for suite in suites:
        os.symlink(
            os.path.join(dirname, "%s-%s-sbuild%s" % (suite, sbuild_arch, ext)),
            basename)


def get_sbuild_architecture():
    return subprocess.check_output(
        ["dpkg-architecture", "-qDEB_BUILD_ARCH"]).decode().strip()


parser = argparse.ArgumentParser()
parser.add_argument('--remove-old', action='store_true')
parser.add_argument(
    '--include', type=str, action='append', help='Include specified package.',
    default=[])
parser.add_argument(
    '--base-directory', type=str, help='Base directory for chroots',
    default=os.path.expanduser('~/.cache/sbuild'))
parser.add_argument(
    '--user', type=str, help='User to create home directory for')
parser.add_argument(
    "--config", type=str, default="janitor.conf", help="Path to configuration."
)
parser.add_argument("distribution", type=str, nargs="*")
args = parser.parse_args()

with open(args.config, "r") as f:
    config = read_config(f)

if not args.distribution:
    args.distribution = [d.name for d in config.distribution]

for distribution in args.distribution:
    try:
        distro_config = get_distribution(config, distribution)
    except KeyError:
        parser.error('no such distribution: %s' % distribution)

    sbuild_arch = get_sbuild_architecture()

    suites = []
    for campaign in config.campaign:
        if not campaign.HasField('debian_build'):
            continue
        if campaign.debian_build.base_distribution != distro_config.name:
            continue
        suites.append(campaign.debian_build.build_distribution)
    sbuild_path = os.path.join(
        args.base_directory, distro_config.chroot + '.tar.xz')
    setup_hooks = []
    if args.user:
        setup_hooks.append(
            'install -d --owner=%s %s' % (
                args.user, pwd.getpwname(args.user).pw_dir))
    create_chroot(
        distro_config, sbuild_path, suites, sbuild_arch, args.include,
        setup_hooks=setup_hooks)
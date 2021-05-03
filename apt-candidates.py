#!/usr/bin/python3
# Copyright (C) 2020 Jelmer Vernooij <jelmer@jelmer.uk>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

"""Exporting of candidates from apt sources list."""

from debian.changelog import Version
from debian.deb822 import Sources
from aiohttp import ClientSession
from email.utils import parseaddr
import gzip
from janitor.candidates_pb2 import CandidateList


async def iter_sources(url):
    async with ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise Exception("URL %s returned response code %d" % (url, resp.status))
            contents = await resp.read()
            if url.endswith(".gz"):
                contents = gzip.decompress(contents)
            for source in Sources.iter_paragraphs(contents):
                yield source


async def main():
    import argparse
    import re

    parser = argparse.ArgumentParser(prog="apt-candidates")
    parser.add_argument("url", nargs="*")
    parser.add_argument(
        "--maintainer", action="append", type=str, help="Filter by maintainer email"
    )
    parser.add_argument(
        "--suite", action="append", type=str, help="Suite to generate candidate for."
    )
    parser.add_argument("--value", type=int, help="Value to specify.", default=10)
    parser.add_argument(
        "--version-re", type=str, help="Filter on versions matching regex."
    )
    parser.add_argument(
        "--exclude-native", action="store_true", help="Exclude native packages."
    )
    parser.add_argument("--package", action='append', type=str)
    args = parser.parse_args()

    if args.version_re:
        version_re = re.compile(args.version_re)
    else:
        version_re = None

    for url in args.url:
        async for source in iter_sources(url):
            if version_re is not None and not version_re.search(source["Version"]):
                continue
            if args.package and source["Package"] not in args.package:
                continue
            if args.exclude_native and not Version(source["Version"]).debian_revision:
                continue
            maintainer_email = parseaddr(source["Maintainer"])[1]
            if args.maintainer and maintainer_email not in args.maintainer:
                continue
            for suite in args.suite:
                cl = CandidateList()
                candidate = cl.candidate.add()
                candidate.suite = suite
                candidate.package = source["Package"]
                candidate.value = args.value
                print(cl)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())

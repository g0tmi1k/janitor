#!/usr/bin/python3
# Copyright (C) 2021 Jelmer Vernooij <jelmer@jelmer.uk>
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

"""Exporting of upstream metadata from UDD."""

from typing import List, Optional, AsyncIterator, Tuple

from janitor.package_metadata_pb2 import PackageList
from janitor.udd import UDD
from janitor.vcs import unsplit_vcs_url


async def iter_upstream_codebases(
    udd: UDD, packages: Optional[List[str]] = None
) -> AsyncIterator[Tuple[str, str, str]]:
    args = []
    query = """
select distinct on (sources.source) sources.source || '-upstream',
  upstream_metadata.value, ''
  from sources
  left join upstream_metadata on upstream_metadata.source = sources.source
  where sources.release = 'sid' AND upstream_metadata.key = 'Repository'
"""
    if packages:
        query += " and sources.source = ANY($1::text[])"
        args.append(packages)
    query += " order by sources.source, sources.version desc"
    for row in await udd.fetch(query, *args):
        yield row


async def main():
    import argparse

    parser = argparse.ArgumentParser(prog="upstream-metadata")
    parser.add_argument("packages", nargs="*")
    args = parser.parse_args()

    udd = await UDD.public_udd_mirror()

    async for (name, branch_url, subpath) in iter_upstream_codebases(
        udd, args.packages
    ):
        pl = PackageList()
        package = pl.package.add()
        package.name = name
        package.vcs_url = unsplit_vcs_url(branch_url, None, subpath)
        package.maintainer_email = "dummy@example.com"
        print(pl)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())

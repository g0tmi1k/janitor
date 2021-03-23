#!/usr/bin/python3
# Copyright (C) 2019-2020 Jelmer Vernooij <jelmer@jelmer.uk>
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

import asyncio
import os
import logging
import sys
import tempfile

from aiohttp import web
from aiohttp.web_middlewares import normalize_path_middleware

from ..artifacts import get_artifact_manager, ArtifactsMissing
from ..config import read_config
from ..prometheus import setup_metrics
from ..pubsub import pubsub_reader


logger = logging.getLogger('janitor.debian.auto_upload')


async def run_web_server(listen_addr, port, config):
    trailing_slash_redirect = normalize_path_middleware(append_slash=True)
    app = web.Application(middlewares=[trailing_slash_redirect])
    app.config = config
    setup_metrics(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, listen_addr, port)
    await site.start()


async def upload_build_result(result, artifact_manager, dput_host):
    logging.info('Uploading results for %s', result['log_id'])
    with tempfile.TemporaryDirectory() as td:
        try:
            await artifact_manager.retrieve_artifacts(
                result['log_id'], td)
        except ArtifactsMissing:
            logging.error(
                'artifacts for build %s are missing',
                result['log_id'])
            return
        for entry in os.scandir(td):
            if entry.name.endswith('.changes'):
                changes_filename = entry.name
                break
        else:
            logging.error('no changes filename in build artifacts')
            return
        logging.debug('Running dput.')
        p = await asyncio.create_subprocess_exec(
            ['dput', dput_host, changes_filename], cwd=td.name,
            stderr=asyncio.subprocess.PIPE)
        (stdout, stderr) = await p.communicate()
        if p.returncode == 0:
            logging.info('Successfully uploaded run %s', result['log_id'])
        else:
            logging.error(
                'Error (exit code %d) uploading %s for %s: %s', 
                p.returncode, changes_filename,
                result['log_id'], stderr.decode())


async def listen_to_runner(runner_url, artifact_manager, dput_host):
    from aiohttp.client import ClientSession
    import urllib.parse

    url = urllib.parse.urljoin(runner_url, "ws/result")
    async with ClientSession() as session:
        async for result in pubsub_reader(session, url):
            if result["code"] != "success":
                continue
            await upload_build_result(result, artifact_manager, dput_host)


async def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(prog="janitor.debian.auto_upload")
    parser.add_argument(
        "--listen-address", type=str, help="Listen address", default="localhost"
    )
    parser.add_argument("--port", type=int, help="Listen port", default=9933)
    parser.add_argument(
        "--config", type=str, default="janitor.conf", help="Path to configuration."
    )
    parser.add_argument("--verbose", action='store_true')
    parser.add_argument("--dput-host", type=str, help="dput host to upload to.")

    args = parser.parse_args()
    if not args.dists_directory:
        parser.print_usage()
        sys.exit(1)

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    with open(args.config, "r") as f:
        config = read_config(f)

    os.makedirs(args.dists_directory, exist_ok=True)

    artifact_manager = get_artifact_manager(config.artifact_location)

    loop = asyncio.get_event_loop()
    await asyncio.gather(
        loop.create_task(
            run_web_server(
                args.listen_address,
                args.port,
                config,
            )
        ),
        loop.create_task(listen_to_runner(args.runner_url, artifact_manager, args.dput_host))
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))

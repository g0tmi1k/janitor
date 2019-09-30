#!/usr/bin/python3
# Copyright (C) 2019 Jelmer Vernooij <jelmer@jelmer.uk>
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

"""Publishing VCS changes."""

from aiohttp import web
import asyncio
import functools
import shlex
import sys
import urllib.parse

from prometheus_client import (
    Counter,
    Gauge,
    push_to_gateway,
    REGISTRY,
)

from silver_platter.proposal import (
    iter_all_mps,
    )
from silver_platter.utils import (
    open_branch,
    BranchMissing,
    BranchUnavailable,
    )

from . import (
    state,
    )
from .config import read_config
from .policy import (
    read_policy,
    apply_policy,
    )
from .prometheus import setup_metrics
from .trace import note, warning
from .vcs import (
    LocalVcsManager,
    get_run_diff,
    )


MODE_SKIP = 'skip'
MODE_BUILD_ONLY = 'build-only'
MODE_PUSH = 'push'
MODE_PUSH_DERIVED = 'push-derived'
MODE_PROPOSE = 'propose'
MODE_ATTEMPT_PUSH = 'attempt-push'
SUPPORTED_MODES = [
    MODE_PUSH,
    MODE_SKIP,
    MODE_BUILD_ONLY,
    MODE_PUSH_DERIVED,
    MODE_PROPOSE,
    MODE_ATTEMPT_PUSH,
    ]


proposal_rate_limited_count = Counter(
    'proposal_rate_limited',
    'Number of attempts to create a proposal that was rate-limited',
    ['package', 'suite'])
open_proposal_count = Gauge(
    'open_proposal_count', 'Number of open proposals.',
    labelnames=('maintainer',))
merge_proposal_count = Gauge(
    'merge_proposal_count', 'Number of merge proposals by status.',
    labelnames=('status',))
last_success_gauge = Gauge(
    'job_last_success_unixtime',
    'Last time a batch job successfully finished')


class PublishFailure(Exception):

    def __init__(self, code, description):
        self.code = code
        self.description = description


async def publish_one(
        suite, pkg, command, subworker_result, main_branch_url,
        mode, log_id, maintainer_email, vcs_manager, branch_name,
        dry_run=False, possible_hosters=None,
        possible_transports=None, allow_create_proposal=None):
    assert mode in SUPPORTED_MODES
    local_branch = vcs_manager.get_branch(pkg, branch_name)
    if local_branch is None:
        raise PublishFailure(
            'result-branch-not-found', 'can not find local branch')

    request = {
        'dry-run': dry_run,
        'suite': suite,
        'package': pkg,
        'command': command,
        'subworker_result': subworker_result,
        'main_branch_url': main_branch_url,
        'local_branch_url': local_branch.user_url,
        'mode': mode,
        'log_id': log_id,
        'allow_create_proposal': allow_create_proposal}

    args = [sys.executable, '-m', 'janitor.publish_one']

    p = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    (stdout, stderr) = await p.communicate(json.dumps(request).encode())

    if p.returncode == 1:
        response = json.loads(stdout.decode())
        raise PublishFailure(response['code'], response['description'])

    if p.returncode == 0:
        response = json.loads(stdout.decode())

        proposal_url = output.get('proposal_url')
        branch_name = output.get('branch_name')
        is_new = output.get('is_new')

        if proposal_url and is_new:
            merge_proposal_count.labels(status='open').inc()
            open_proposal_count.labels(
                maintainer=maintainer_email).inc()

        return proposal_url, branch_name, is_new

    raise PublishFailure('publisher-invalid-response', stderr)


async def publish_pending_new(rate_limiter, policy, vcs_manager,
                              dry_run=False):
    possible_hosters = []
    possible_transports = []

    async for (pkg, command, build_version, result_code, context,
               start_time, log_id, revision, subworker_result, branch_name,
               suite, maintainer_email, uploader_emails, main_branch_url,
               main_branch_revision) in state.iter_publish_ready():

        mode, unused_update_changelog, unused_committer = apply_policy(
            policy, suite.replace('-', '_'), pkg, maintainer_email,
            uploader_emails or [])
        if mode in (MODE_BUILD_ONLY, MODE_SKIP):
            continue
        if await state.already_published(pkg, branch_name, revision, mode):
            continue
        if not rate_limiter.allowed(maintainer_email) and \
                mode in (MODE_PROPOSE, MODE_ATTEMPT_PUSH):
            proposal_rate_limited_count.labels(package=pkg, suite=suite).inc()
            warning(
                'Not creating proposal for %s, maximum number of open merge '
                'proposals reached for maintainer %s', pkg, maintainer_email)
            if mode == MODE_PROPOSE:
                mode = MODE_BUILD_ONLY
            if mode == MODE_ATTEMPT_PUSH:
                mode = MODE_PUSH
        if mode == MODE_ATTEMPT_PUSH and \
                "salsa.debian.org/debian/" in main_branch_url:
            # Make sure we don't accidentally push to unsuspecting collab-maint
            # repositories, even if debian-janitor becomes a member of "debian"
            # in the future.
            mode = MODE_PROPOSE
        if mode in (MODE_BUILD_ONLY, MODE_SKIP):
            continue
        note('Publishing %s / %r (mode: %s)', pkg, command, mode)
        try:
            proposal_url, branch_name, is_new = await publish_one(
                suite, pkg, command, subworker_result,
                main_branch_url, mode, log_id, maintainer_email,
                vcs_manager=vcs_manager, branch_name=branch_name,
                dry_run=dry_run, possible_hosters=possible_hosters,
                possible_transports=possible_transports)
        except PublishFailure as e:
            code = e.code
            description = e.description
            branch_name = None
            proposal_url = None
            note('Failed(%s): %s', code, description)
        else:
            code = 'success'
            description = 'Success'
            if proposal_url and is_new:
                rate_limiter.inc(maintainer_email)

        await state.store_publish(
            pkg, branch_name, main_branch_revision,
            revision, mode, code, description,
            proposal_url if proposal_url else None)


async def diff_request(vcs_manager, request):
    run_id = request.match_info['run_id']
    run = await state.get_run(run_id)
    diff = get_run_diff(vcs_manager, run)
    return web.Response(body=diff, content_type='text/x-diff')


async def publish_and_store():

    try:
        proposal_url, branch_name, is_new = await publish_one(
    except PublishFailure as e:
        await state.store_publish(
            run.package, run.branch_name,
            run.main_branch_revision.decode('utf-8'),
            run.revision.decode('utf-8'), mode, e.code, e.description,
            None)
        return web.json_response(
            {'code': e.code, 'description': e.description}, status=400)

    if proposal_url and is_new:
        rate_limiter.inc(package.maintainer_email)

    await state.store_publish(
        run.package, branch_name, run.main_branch_revision.decode('utf-8'),
        run.revision.decode('utf-8'), mode, 'success', 'Success',
        proposal_url if proposal_url else None)


async def publish_request(rate_limiter, dry_run, vcs_manager, request):
    package = request.match_info['package']
    suite = request.match_info['suite']
    post = await request.post()
    mode = post.get('mode', MODE_PROPOSE)
    try:
        package = await state.get_package(package)
    except IndexError:
        return web.json_response({}, status=400)

    if (mode in (MODE_PROPOSE, MODE_ATTEMPT_PUSH) and
            not rate_limiter.allowed(package.maintainer_email)):
        return web.json_response(
            {'maintainer_email': package.maintainer_email,
             'code': 'rate-limited',
             'description':
                'Maximum number of open merge proposals for maintainer '
                'reached'},
            status=429)

    if mode in (MODE_SKIP, MODE_BUILD_ONLY):
        return web.json_response(
            {'code': 'done',
             'description':
                'Nothing to do'})

    run = await state.get_last_unmerged_success(package.name, suite)
    if run is None:
        return web.json_response({}, status=400)
    note('Handling request to publish %s/%s', package.name, suite)

    request.loop.create_task(publish_and_store(
        suite, package.name, run.command, run.result,
        run.branch_url, mode, run.id, package.maintainer_email,
        vcs_manager=vcs_manager, branch_name=run.branch_name,
        dry_run=dry_run, allow_create_proposal=True))

    return web.json_response(
        {'run_id': run.id, 'mode': mode},
        status=202)


async def run_web_server(listen_addr, port, rate_limiter, vcs_manager,
                         dry_run=False):
    app = web.Application()
    setup_metrics(app)
    app.router.add_post(
        "/{suite}/{package}/publish",
        functools.partial(publish_request, rate_limiter, dry_run, vcs_manager))
    app.router.add_get(
        "/diff/{run_id}",
        functools.partial(diff_request, vcs_manager))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, listen_addr, port)
    await site.start()


async def process_queue_loop(rate_limiter, policy, dry_run, vcs_manager,
                             interval, auto_publish=True):
    while True:
        await check_existing(rate_limiter, vcs_manager, dry_run)
        await asyncio.sleep(interval)
        if auto_publish:
            await publish_pending_new(
                rate_limiter, policy, vcs_manager, dry_run)


def is_conflicted(mp):
    try:
        return not mp.can_be_merged()
    except NotImplementedError:
        # TODO(jelmer): Download and attempt to merge locally?
        return None


async def check_existing(rate_limiter, vcs_manager, dry_run=False):
    open_mps_per_maintainer = {}
    possible_transports = []
    status_count = {'open': 0, 'closed': 0, 'merged': 0}
    for hoster, mp, status in iter_all_mps():
        await state.set_proposal_status(mp.url, status)
        status_count[status] += 1
        if not await state.get_proposal_revision(mp.url):
            try:
                revision = open_branch(
                    mp.get_source_branch_url(),
                    possible_transports=possible_transports).last_revision()
            except (BranchMissing, BranchUnavailable):
                pass
            else:
                await state.set_proposal_revision(
                    mp.url, revision.decode('utf-8'))
        if status != 'open':
            continue
        maintainer_email = await state.get_maintainer_email_for_proposal(
            mp.url)
        if maintainer_email is None:
            source_branch_url = mp.get_target_branch_url()
            maintainer_email = await state.get_maintainer_email_for_branch_url(
                source_branch_url)
            if maintainer_email is None:
                warning('No maintainer email known for %s', mp.url)
        if maintainer_email is not None:
            open_mps_per_maintainer.setdefault(maintainer_email, 0)
            open_mps_per_maintainer[maintainer_email] += 1
        mp_run = await state.get_merge_proposal_run(mp.url)
        if mp_run is None:
            warning('Unable to find local metadata for %s, skipping.', mp.url)
            continue

        recent_runs = []
        async for run in state.iter_previous_runs(
                mp_run.package, mp_run.suite):
            if run == mp_run:
                break
            recent_runs.append(run)

        for run in recent_runs:
            if run.result_code not in ('success', 'nothing-to-do'):
                note('%s: Last run failed (%s). Not touching merge proposal.',
                     mp.url, run.result_code)
                break

            if run.result_code == 'nothing-to-do':
                continue

            if run.suite == 'unchanged':
                continue

            note('%s needs to be updated.', mp.url)
            try:
                mp_url, branch_name, is_new = await publish_one(
                    run.suite, run.package, run.command, run.result,
                    run.branch_url, MODE_PROPOSE, run.id,
                    maintainer_email,
                    vcs_manager=vcs_manager, branch_name=run.branch_name,
                    dry_run=dry_run, allow_create_proposal=True)
            except PublishFailure as e:
                note('%s: Updating merge proposal failed: %s (%s)',
                     mp.url, e.code, e.description)
                await state.store_publish(
                    run.package, branch_name,
                    run.main_branch_revision.decode('utf-8'),
                    run.revision.decode('utf-8'), MODE_PROPOSE, e.code,
                    e.description, mp.url)
                break
            else:
                await state.store_publish(
                    run.package, branch_name,
                    run.main_branch_revision.decode('utf-8'),
                    run.revision.decode('utf-8'), MODE_PROPOSE, 'success',
                    'Succesfully updated', mp_url)

                assert not is_new, "Intended to update proposal %r" % mp_url
                break
        else:
            if recent_runs:
                # A new run happened since the last, but there was nothing to
                # do.
                note('%s: Last run did not produce any changes, '
                     'closing proposal.', mp.url)
                # TODO(jelmer): Log this in the database
                mp.close()
                continue

            # It may take a while for the 'conflicted' bit on the proposal to
            # be refreshed, so only check it if we haven't made any other
            # changes.
            if is_conflicted(mp):
                note('%s is conflicted. Rescheduling.', mp.url)
                await state.add_to_queue(
                    run.branch_url, run.package, shlex.split(run.command),
                    run.suite, offset=-2, refresh=True,
                    requestor='publisher')

    for status, count in status_count.items():
        merge_proposal_count.labels(status=status).set(count)

    rate_limiter.set_open_mps_per_maintainer(open_mps_per_maintainer)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog='janitor.publish')
    parser.add_argument(
        '--max-mps-per-maintainer',
        default=0,
        type=int,
        help='Maximum number of open merge proposals per maintainer.')
    parser.add_argument(
        "--dry-run",
        help="Create branches but don't push or propose anything.",
        action="store_true", default=False)
    parser.add_argument(
        "--policy",
        help="Policy file to read.", type=str,
        default='policy.conf')
    parser.add_argument(
        '--prometheus', type=str,
        help='Prometheus push gateway to export to.')
    parser.add_argument(
        '--once', action='store_true',
        help="Just do one pass over the queue, don't run as a daemon.")
    parser.add_argument(
        '--listen-address', type=str,
        help='Listen address', default='localhost')
    parser.add_argument(
        '--port', type=int,
        help='Listen port', default=9912)
    parser.add_argument(
        '--interval', type=int,
        help=('Seconds to wait in between publishing '
              'pending proposals'), default=7200)
    parser.add_argument(
        '--no-auto-publish',
        action='store_true',
        help='Do not create merge proposals automatically.')
    parser.add_argument(
        '--config', type=str, default='janitor.conf',
        help='Path to load configuration from.')

    args = parser.parse_args()

    with open(args.policy, 'r') as f:
        policy = read_policy(f)

    with open(args.config, 'r') as f:
        config = read_config(f)

    state.DEFAULT_URL = config.database_location

    if args.max_mps_per_maintainer > 0:
        rate_limiter = MaintainerRateLimiter(args.max_mps_per_maintainer)
    else:
        rate_limiter = NonRateLimiter()

    if args.no_auto_publish and args.once:
        sys.stderr.write('--no-auto-publish and --once are mutually exclude.')
        sys.exit(1)

    loop = asyncio.get_event_loop()
    vcs_manager = LocalVcsManager(config.vcs_location)
    if args.once:
        loop.run_until_complete(publish_pending_new(
            policy, dry_run=args.dry_run,
            vcs_manager=vcs_manager))

        last_success_gauge.set_to_current_time()
        if args.prometheus:
            push_to_gateway(
                args.prometheus, job='janitor.publish',
                registry=REGISTRY)
    else:
        loop.run_until_complete(asyncio.gather(
            loop.create_task(process_queue_loop(
                rate_limiter, policy, dry_run=args.dry_run,
                vcs_manager=vcs_manager, interval=args.interval,
                auto_publish=not args.no_auto_publish)),
            loop.create_task(
                run_web_server(
                    args.listen_address, args.port, rate_limiter,
                    vcs_manager, args.dry_run))))


if __name__ == '__main__':
    sys.exit(main(sys.argv))

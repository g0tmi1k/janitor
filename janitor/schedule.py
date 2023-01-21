#!/usr/bin/python
# Copyright (C) 2018 Jelmer Vernooij <jelmer@jelmer.uk>
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

__all__ = [
    "bulk_add_to_queue",
]

from datetime import datetime, timedelta
import logging
import shlex
from typing import Optional

from debian.changelog import Version

import asyncpg

from . import set_user_agent
from .config import read_config
from .queue import Queue

FIRST_RUN_BONUS = 100.0


# Default estimation if there is no median for the campaign or the package.
DEFAULT_ESTIMATED_DURATION = 15
DEFAULT_SCHEDULE_OFFSET = -1.0


# In some cases, we want to ignore certain results when guessing
# whether a future run is going to be successful.
# For example, some results are transient, or sometimes new runs
# will give a clearer error message.
IGNORE_RESULT_CODE = {
    # Run worker failures from more than a day ago.
    "worker-failure": lambda run: ((datetime.utcnow() - run['start_time']).days > 0),
}

PUBLISH_MODE_VALUE = {
    "skip": 0,
    "build-only": 0,
    "push": 500,
    "propose": 400,
    "attempt-push": 450,
    "bts": 100,
}


async def iter_candidates_with_publish_policy(
        conn: asyncpg.Connection,
        packages: Optional[list[str]] = None,
        campaign: Optional[str] = None):
    query = """
SELECT
  package.name AS package,
  package.codebase AS codebase,
  package.branch_url AS branch_url,
  candidate.suite AS campaign,
  candidate.context AS context,
  candidate.value AS value,
  candidate.success_chance AS success_chance,
  named_publish_policy.per_branch_policy AS publish,
  candidate.command AS command
FROM candidate
INNER JOIN package on package.codebase = candidate.codebase
INNER JOIN named_publish_policy ON
    named_publish_policy.name = candidate.publish_policy
WHERE
  NOT package.removed
"""
    args = []
    if campaign is not None and packages is not None:
        query += " AND package.name = ANY($1::text[]) AND candidate.suite = $2"
        args.extend([packages, campaign])
    elif campaign is not None:
        query += " AND candidate.suite = $1"
        args.append(campaign)
    elif packages is not None:
        query += " AND package.name = ANY($1::text[])"
        args.append(packages)
    return await conn.fetch(query, *args)


def queue_item_from_candidate_and_publish_policy(row):
    value = row['value']
    for entry in row['publish']:
        value += PUBLISH_MODE_VALUE[entry['mode']]

    command = row['command']

    return (row['package'], row['codebase'],
            row['context'], command, row['campaign'],
            value, row['success_chance'])


async def estimate_success_probability(
    conn: asyncpg.Connection, codebase: str, campaign: str, context: Optional[str] = None
) -> tuple[float, int]:
    # TODO(jelmer): Bias this towards recent runs?
    total = 0
    success = 0
    if context is None:
        same_context_multiplier = 0.5
    else:
        same_context_multiplier = 1.0
    for run in await conn.fetch("""
SELECT
  result_code, instigated_context, context, failure_details, failure_transient,
  start_time
FROM run
WHERE codebase = $1 AND suite = $2
ORDER BY start_time DESC
""", codebase, campaign):
        try:
            ignore_checker = IGNORE_RESULT_CODE[run['result_code']]
        except KeyError:
            def ignore_checker(run):
                return run['failure_transient']

        if ignore_checker(run):
            continue
        total += 1
        if run['result_code'] == "success":
            success += 1
        same_context = False
        if context and context in (run['instigated_context'], run['context']):
            same_context = True
        if (run['result_code'] == "install-deps-unsatisfied-dependencies" and run['failure_details']
                and run['failure_details'].get('relations')):
            if await deps_satisfied(conn, campaign, run['failure_details']['relations']):
                success += 1
                same_context = False
        if same_context:
            same_context_multiplier = 0.1

    if total == 0:
        # If there were no previous runs, then it doesn't really matter that
        # we don't know the context.
        same_context_multiplier = 1.0

    return ((success * 10 + 1) / (total * 10 + 1) * same_context_multiplier), total


async def _estimate_duration(
    conn: asyncpg.Connection,
    codebase: Optional[str] = None,
    campaign: Optional[str] = None,
    limit: Optional[int] = 1000,
) -> Optional[timedelta]:
    query = """
SELECT AVG(duration) FROM
(select finish_time - start_time as duration FROM run
WHERE """
    args = []
    if codebase is not None:
        query += " codebase = $1"
        args.append(codebase)
    if campaign is not None:
        if codebase:
            query += " AND"
        query += " suite = $%d" % (len(args) + 1)
        args.append(campaign)
    query += " ORDER BY finish_time DESC"
    if limit is not None:
        query += " LIMIT %d" % limit
    query += ") as q"
    return await conn.fetchval(query, *args)


async def estimate_duration(
    conn: asyncpg.Connection, codebase: str, campaign: str
) -> timedelta:
    """Estimate the duration of a codebase build for a certain campaign."""
    estimated_duration = await _estimate_duration(
        conn, codebase=codebase, campaign=campaign
    )
    if estimated_duration is not None:
        return estimated_duration

    estimated_duration = await _estimate_duration(conn, codebase=codebase)
    if estimated_duration is not None:
        return estimated_duration

    estimated_duration = await _estimate_duration(conn, campaign=campaign)
    if estimated_duration is not None:
        return estimated_duration

    return timedelta(seconds=DEFAULT_ESTIMATED_DURATION)


async def do_schedule_regular(
        conn: asyncpg.Connection, *,
        package: str, codebase: str, campaign: str,
        command: Optional[str] = None, 
        candidate_value: Optional[float] = None,
        success_chance: Optional[float] = None,
        normalized_codebase_value: Optional[float] = None,
        requestor: Optional[str] = None,
        default_offset: float = 0.0,
        context: Optional[str] = None,
        change_set: Optional[str] = None,
        dry_run: bool = False,
        refresh: bool = False,
        bucket: Optional[str] = None) -> tuple[float, Optional[timedelta], int, str]:
    if candidate_value is None or success_chance is None or command is None:
        row = await conn.fetchrow(
            'SELECT value, success_chance, command, context FROM candidate '
            'WHERE codebase = $1 and suite = $2',
            codebase, campaign)
        if row is not None and candidate_value is None:
            candidate_value = row['value']
        if row is not None and success_chance is None:
            success_chance = row['success_chance']
        if row is not None and context is None:
            context = row['context']
        if row is not None and command is None:
            command = row['command']
    if candidate_value is None:
        candidate_value = 0
    estimated_duration = await estimate_duration(conn, codebase, campaign)
    assert estimated_duration >= timedelta(
        0
    ), "{}: estimated duration < 0.0: {!r}".format(codebase, estimated_duration)
    (
        estimated_probability_of_success,
        total_previous_runs,
    ) = await estimate_success_probability(conn, codebase, campaign, context)
    
    if total_previous_runs == 0:
        candidate_value += FIRST_RUN_BONUS
    assert (
        estimated_probability_of_success >= 0.0
        and estimated_probability_of_success <= 1.0
    ), ("Probability of success: %s" % estimated_probability_of_success)
    if success_chance is not None:
        success_chance *= estimated_probability_of_success
    estimated_cost = 20000.0 + (
        1.0 * estimated_duration.total_seconds() * 1000.0
        + estimated_duration.microseconds
    )
    assert estimated_cost > 0.0, "{}: Estimated cost: {:f}".format(
        codebase,
        estimated_cost,
    )
    if normalized_codebase_value is None:
        normalized_codebase_value = await conn.fetchval(
            "select coalesce(least(1.0 * value / (select max(value) from codebase), 1.0), 1.0) "
            "from codebase WHERE name = $1", codebase)
    estimated_value = (
        normalized_codebase_value * estimated_probability_of_success * candidate_value
    )
    assert estimated_value >= 0.0, "Estimated value: %s" % estimated_value
    offset = estimated_cost / estimated_value
    assert offset > 0.0
    offset = default_offset + offset
    logging.info(
        "Codebase %s/%s: "
        "normalized_codebase_value(%.2f) * "
        "probability_of_success(%.2f) * candidate_value(%d) = "
        "estimated_value(%.2f), estimated cost (%f)",
        campaign, codebase,
        normalized_codebase_value,
        estimated_probability_of_success,
        candidate_value,
        estimated_value,
        estimated_cost,
    )

    if bucket is None:
        bucket = 'default'

    assert command
    if not dry_run:
        queue = Queue(conn)
        queue_id, bucket = await queue.add(
            package=package,
            codebase=codebase,
            campaign=campaign,
            change_set=change_set,
            command=command,
            offset=offset,
            refresh=refresh,
            bucket=bucket,
            estimated_duration=estimated_duration,
            context=context,
            requestor=requestor or "scheduler",
        )
    else:
        queue_id = -1
    logging.info("Scheduled %s (%s) with offset %f", codebase, campaign, offset)
    return offset, estimated_duration, queue_id, bucket


async def bulk_add_to_queue(
    conn: asyncpg.Connection,
    todo,
    dry_run: bool = False,
    default_offset: float = 0.0,
    bucket: str = "default",
) -> None:
    codebase_values = {k: (v or 0) for (k, v) in await conn.fetch(
        "SELECT name, value FROM codebase WHERE name IS NOT NULL")}
    if codebase_values:
        max_codebase_value = max([(v or 0) for v in codebase_values.values()])
        if max_codebase_value:
            logging.info("Maximum value: %d", max_codebase_value)
    else:
        max_codebase_value = None
    for package, codebase, context, command, campaign, value, success_chance in todo:
        if max_codebase_value is not None:
            normalized_codebase_value = min(
                codebase_values.get(codebase, 0.0) / max_codebase_value, 1.0)
        else:
            normalized_codebase_value = 1.0
        await do_schedule_regular(
            conn, package=package, codebase=codebase, context=context,
            command=command, campaign=campaign, candidate_value=value,
            success_chance=success_chance,
            default_offset=default_offset,
            normalized_codebase_value=normalized_codebase_value,
            dry_run=dry_run,
            bucket=bucket)


async def dep_available(
    conn: asyncpg.Connection,
    name: str,
    archqual: Optional[str] = None,
    arch: Optional[str] = None,
    distribution: Optional[str] = None,
    version: Optional[tuple[str, Version]] = None,
    restrictions=None,
) -> bool:
    query = """\
SELECT
  1
FROM
  all_debian_versions
WHERE
  source = $1 AND %(version_match)s
"""
    args = [name]
    if version:
        version_match = "version {} $2".format(version[0])
        args.append(str(version[1]))
    else:
        version_match = "True"

    return bool(await conn.fetchval(
        query % {"version_match": version_match}, *args))


async def deps_satisfied(conn: asyncpg.Connection, campaign: str, dependencies) -> bool:
    for dep in dependencies:
        for subdep in dep:
            if await dep_available(conn, **subdep):
                break
        else:
            return False
    return True


async def main():
    import argparse
    from janitor import state
    from aiohttp_openmetrics import (
        Gauge,
        push_to_gateway,
        REGISTRY,
    )

    parser = argparse.ArgumentParser(prog="janitor.schedule")
    parser.add_argument(
        "--dry-run",
        help="Create branches but don't push or propose anything.",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--prometheus", type=str, help="Prometheus push gateway to export to."
    )
    parser.add_argument(
        "--config", type=str, default="janitor.conf", help="Path to configuration."
    )
    parser.add_argument("--campaign", type=str, help="Restrict to a specific campaign.")
    parser.add_argument("--gcp-logging", action='store_true', help='Use Google cloud logging.')
    parser.add_argument("packages", help="Package to process.", nargs="*")
    parser.add_argument("--debug", action="store_true")

    args = parser.parse_args()

    if args.gcp_logging:
        import google.cloud.logging
        client = google.cloud.logging.Client()
        client.get_default_handler()
        client.setup_logging()
    else:
        if args.debug:
            level = logging.DEBUG
        else:
            level = logging.INFO
        logging.basicConfig(level=level, format="%(message)s")

    last_success_gauge = Gauge(
        "job_last_success_unixtime", "Last time a batch job successfully finished"
    )

    logging.info('Reading configuration')
    with open(args.config) as f:
        config = read_config(f)

    set_user_agent(config.user_agent)

    async with state.create_pool(config.database_location) as conn:
        logging.info('Finding candidates with policy')
        logging.info('Determining schedule for candidates')
        todo = [
            queue_item_from_candidate_and_publish_policy(row)
            for row in
            await iter_candidates_with_publish_policy(
                conn, packages=(args.packages or None), campaign=args.campaign)]
        logging.info('Adding %d items to queue', len(todo))
        await bulk_add_to_queue(conn, todo, dry_run=args.dry_run)

    last_success_gauge.set_to_current_time()
    if args.prometheus:
        await push_to_gateway(args.prometheus, job="janitor.schedule", registry=REGISTRY)


async def do_schedule_control(
    conn: asyncpg.Connection,
    codebase: str,
    *,
    package: Optional[str] = None,
    change_set: Optional[str] = None,
    main_branch_revision: Optional[bytes] = None,
    offset: Optional[float] = None,
    refresh: bool = False,
    bucket: Optional[str] = None,
    requestor: Optional[str] = None,
    estimated_duration: Optional[timedelta] = None,
) -> tuple[float, Optional[timedelta], int, str]:
    command = ["brz", "up"]
    if main_branch_revision is not None:
        command.append("--revision=%s" % main_branch_revision.decode("utf-8"))
    if bucket is None:
        bucket = "control"
    return await do_schedule(
        conn,
        package=package,
        campaign="control",
        change_set=change_set,
        offset=offset,
        refresh=refresh,
        bucket=bucket,
        requestor=requestor,
        command=shlex.join(command),
        codebase=codebase,
    )


class CandidateUnavailable(Exception):
    def __init__(self, campaign: str, codebase: str):
        self.campaign = campaign
        self.codebase = codebase


async def do_schedule(
    conn: asyncpg.Connection,
    campaign: str,
    codebase: str,
    bucket: str,
    *,
    package: Optional[str] = None,
    change_set: Optional[str] = None,
    offset: Optional[float] = None,
    refresh: bool = False,
    requestor: Optional[str] = None,
    estimated_duration=None,
    command: Optional[str] = None,
) -> tuple[float, Optional[timedelta], int, str]:
    if offset is None:
        offset = DEFAULT_SCHEDULE_OFFSET
    if bucket is None:
        bucket = "default"
    assert codebase is not None
    if command is None:
        candidate = await conn.fetchrow(
            "SELECT command "
            "FROM candidate WHERE codebase = $1 AND suite = $2",
            codebase, campaign)
        if not candidate:
            raise CandidateUnavailable(campaign, codebase)
        command = candidate['command']
    if estimated_duration is None:
        estimated_duration = await estimate_duration(conn, codebase, campaign)
    queue = Queue(conn)
    queue_id, bucket = await queue.add(
        package=package,
        command=command,
        campaign=campaign,
        change_set=change_set,
        offset=offset,
        bucket=bucket,
        estimated_duration=estimated_duration,
        refresh=refresh,
        requestor=requestor,
        codebase=codebase,
    )
    return offset, estimated_duration, queue_id, bucket


if __name__ == "__main__":
    import asyncio

    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())

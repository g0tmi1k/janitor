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

import datetime
from debian.changelog import Version
import json
import asyncpg
import logging
from contextlib import asynccontextmanager
from typing import Optional, Tuple, List, Any, Union, AsyncIterable, Dict

from breezy import urlutils


class Database(object):
    def __init__(self, url):
        self.url = url
        self.pool = None

    @asynccontextmanager
    async def acquire(self):
        if self.pool is None:
            self.pool = await asyncpg.create_pool(self.url)
        async with self.pool.acquire() as conn:
            await conn.set_type_codec(
                "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
            )
            await conn.set_type_codec(
                "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
            )
            await conn.set_type_codec(
                "debversion", format="text", encoder=str, decoder=Version
            )
            yield conn


def get_result_branch(result_branches, role):
    for entry in result_branches:
        if role == entry[0]:
            return entry[1:]
    raise KeyError



class Run(object):

    id: str
    times: Tuple[datetime.datetime, datetime.datetime]
    command: str
    description: Optional[str]
    package: str
    build_version: Optional[Version]
    build_distribution: Optional[str]
    result_code: str
    main_branch_revision: Optional[bytes]
    revision: Optional[bytes]
    context: Optional[str]
    result: Optional[Any]
    suite: str
    instigated_context: Optional[str]
    vcs_type: str
    branch_url: str
    logfilenames: Optional[List[str]]
    review_status: str
    review_comment: Optional[str]
    worker_name: Optional[str]
    result_branches: Optional[List[Tuple[str, str, bytes, bytes]]]
    result_tags: Optional[List[Tuple[str, bytes]]]

    __slots__ = [
        "id",
        "start_time",
        "finish_time",
        "command",
        "description",
        "package",
        "build_version",
        "build_distribution",
        "result_code",
        "value",
        "main_branch_revision",
        "revision",
        "context",
        "result",
        "suite",
        "instigated_context",
        "vcs_type",
        "branch_url",
        "logfilenames",
        "review_status",
        "review_comment",
        "worker_name",
        "result_branches",
        "result_tags",
    ]

    def __init__(
        self,
        run_id,
        start_time,
        finish_time,
        command,
        description,
        package,
        build_version,
        build_distribution,
        result_code,
        value,
        main_branch_revision,
        revision,
        context,
        result,
        suite,
        instigated_context,
        vcs_type,
        branch_url,
        logfilenames,
        review_status,
        review_comment,
        worker_name,
        result_branches,
        result_tags,
    ):
        self.id = run_id
        self.start_time = start_time
        self.finish_time = finish_time
        self.command = command
        self.description = description
        self.package = package
        self.build_version = build_version
        self.build_distribution = build_distribution
        self.result_code = result_code
        self.value = value
        self.main_branch_revision = main_branch_revision
        self.revision = revision
        self.context = context
        self.result = result
        self.suite = suite
        self.instigated_context = instigated_context
        self.vcs_type = vcs_type
        self.branch_url = branch_url
        self.logfilenames = logfilenames
        self.review_status = review_status
        self.review_comment = review_comment
        self.worker_name = worker_name
        if result_branches is None:
            self.result_branches = None
        else:
            self.result_branches = [
                (
                    role,
                    name,
                    br.encode("utf-8") if br else None,
                    r.encode("utf-8") if r else None,
                )
                for (role, name, br, r) in result_branches
            ]
        if result_tags is None:
            self.result_tags = result_tags
        else:
            self.result_tags = [(name, r.encode("utf-8")) for (name, r) in result_tags]

    @property
    def duration(self) -> datetime.timedelta:
        return self.finish_time - self.start_time

    def get_result_branch(self, role):
        return get_result_branch(self.result_branches, role)

    @classmethod
    def from_row(cls, row) -> "Run":
        return cls(
            run_id=row['id'],
            start_time=row['start_time'],
            finish_time=row['finish_time'],
            command=row['command'],
            description=row['description'],
            package=row['package'],
            build_version=row['build_version'],
            build_distribution=row['build_distribution'],
            result_code=row['result_code'],
            main_branch_revision=(row['main_branch_revision'].encode("utf-8") if row['main_branch_revision'] else None),
            revision=(row['revision'].encode("utf-8") if row['revision'] else None),
            context=row['context'],
            result=row['result'],
            value=row['value'],
            suite=row['suite'],
            instigated_context=row['instigated_context'],
            vcs_type=row['vcs_type'],
            branch_url=row['branch_url'],
            logfilenames=row['logfilenames'],
            review_status=row['review_status'],
            review_comment=row['review_comment'],
            worker_name=row['worker'],
            result_branches=row['result_branches'],
            result_tags=row['result_tags'],
        )

    def __eq__(self, other) -> bool:
        if isinstance(other, Run):
            return self.id == other.id
        return False

    def __lt__(self, other) -> bool:
        if not isinstance(other, type(self)):
            raise TypeError(other)
        return self.id < other.id


async def iter_runs(
    db: Database,
    package: Optional[str] = None,
    run_id: Optional[str] = None,
    worker: Optional[str] = None,
    limit: Optional[int] = None,
):
    async with db.acquire() as conn:
        async for run in _iter_runs(
            conn, package=package, run_id=run_id, worker=worker, limit=limit
        ):
            yield run


async def _iter_runs(
    conn: asyncpg.Connection,
    package: Optional[str] = None,
    run_id: Optional[str] = None,
    worker: Optional[str] = None,
    suite: Optional[str] = None,
    limit: Optional[int] = None,
):
    """Iterate over runs.

    Args:
      package: package to restrict to
    Returns:
      iterator over Run objects
    """
    query = """
SELECT
    id, command, start_time, finish_time, description, package,
    debian_build.version AS build_version,
    debian_build.distribution AS build_distribution, result_code,
    value, main_branch_revision, revision, context, result, suite,
    instigated_context, vcs_type, branch_url, logfilenames, review_status,
    review_comment, worker,
    array(SELECT row(role, remote_name, base_revision,
     revision) FROM new_result_branch WHERE run_id = id) AS result_branches,
    result_tags
FROM
    run
LEFT JOIN
    debian_build ON debian_build.run_id = run.id
"""
    conditions = []
    args = []
    if package is not None:
        args.append(package)
        conditions.append("package = $%d" % len(args))
    if run_id is not None:
        args.append(run_id)
        conditions.append("id = $%d" % len(args))
    if worker is not None:
        args.append(worker)
        conditions.append("worker = $%d" % len(args))
    if suite is not None:
        args.append(suite)
        conditions.append("suite = $%d" % len(args))
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += "ORDER BY finish_time DESC"
    if limit:
        query += " LIMIT %d" % limit
    for row in await conn.fetch(query, *args):
        yield Run.from_row(row)


class QueueItem(object):

    __slots__ = [
        "id",
        "branch_url",
        "subpath",
        "package",
        "context",
        "command",
        "estimated_duration",
        "suite",
        "refresh",
        "requestor",
        "vcs_type",
        "upstream_branch_url",
    ]

    def __init__(
        self,
        id,
        branch_url,
        subpath,
        package,
        context,
        command,
        estimated_duration,
        suite,
        refresh,
        requestor,
        vcs_type,
        upstream_branch_url,
    ):
        self.id = id
        self.package = package
        self.branch_url = branch_url
        self.subpath = subpath
        self.context = context
        self.command = command
        self.estimated_duration = estimated_duration
        self.suite = suite
        self.refresh = refresh
        self.requestor = requestor
        self.vcs_type = vcs_type
        self.upstream_branch_url = upstream_branch_url

    @classmethod
    def from_row(cls, row) -> "QueueItem":
        return cls(
            id=row['id'],
            branch_url=row['branch_url'],
            subpath=row['subpath'],
            package=row['package'],
            context=row['context'],
            command=row['command'],
            estimated_duration=row['estimated_duration'],
            suite=row['suite'],
            refresh=row['refresh'],
            requestor=row['requestor'],
            vcs_type=row['vcs_type'],
            upstream_branch_url=row['upstream_branch_url'],
        )

    def _tuple(self):
        return (
            self.id,
            self.branch_url,
            self.subpath,
            self.package,
            self.context,
            self.command,
            self.estimated_duration,
            self.suite,
            self.refresh,
            self.requestor,
            self.vcs_type,
            self.upstream_branch_url,
        )

    def __eq__(self, other):
        if isinstance(other, QueueItem):
            return self.id == other.id
        return False

    def __lt__(self, other):
        return self.id < other.id

    def __hash__(self):
        return hash((type(self), self.id))


async def get_queue_position(conn: asyncpg.Connection, suite, package):
    row = await conn.fetchrow(
        "SELECT position, wait_time FROM queue_positions "
        "WHERE package = $1 AND suite = $2",
        package, suite)
    if not row:
        return (None, None)
    return row


async def get_queue_item(conn: asyncpg.Connection, queue_id: int):
    query = """
SELECT
    package.branch_url AS branch_url,
    package.subpath AS subpath,
    queue.package AS package,
    queue.command AS command,
    queue.context AS context,
    queue.id AS id,
    queue.estimated_duration AS estimated_duration,
    queue.suite AS suite,
    queue.refresh AS refresh,
    queue.requestor AS requestor,
    package.vcs_type AS vcs_type,
    upstream.upstream_branch_url AS upstream_branch_url
FROM
    queue
LEFT JOIN package ON package.name = queue.package
LEFT OUTER JOIN upstream ON upstream.name = package.name
WHERE queue.id = $1
"""
    row = await conn.fetchrow(query, queue_id)
    if row:
        return QueueItem.from_row(row)
    return None


async def iter_queue(conn: asyncpg.Connection, limit=None):
    query = """
SELECT
    package.branch_url AS branch_url,
    package.subpath AS subpath,
    queue.package AS package,
    queue.command AS command,
    queue.context AS context,
    queue.id AS id,
    queue.estimated_duration AS estimated_duration,
    queue.suite AS suite,
    queue.refresh AS refresh,
    queue.requestor AS requestor,
    package.vcs_type AS vcs_type,
    upstream.upstream_branch_url AS upstream_branch_url
FROM
    queue
LEFT JOIN package ON package.name = queue.package
LEFT OUTER JOIN upstream ON upstream.name = package.name
ORDER BY
queue.bucket ASC,
queue.priority ASC,
queue.id ASC
"""
    if limit:
        query += " LIMIT %d" % limit
    for row in await conn.fetch(query):
        yield QueueItem.from_row(row)


async def iter_publish_ready(
    conn: asyncpg.Connection,
    suites: Optional[List[str]] = None,
    review_status: Optional[Union[str, List[str]]] = None,
    limit: Optional[int] = None,
    publishable_only: bool = False,
    needs_review: Optional[bool] = None,
    run_id: Optional[str] = None,
) -> AsyncIterable[
    Tuple[
        Run,
        int,
        str,
        List[str],
        str,
        str,
        Optional[str],
        bool,
        List[Tuple[str, str, bytes, bytes, Optional[str], Optional[int], Optional[str]]],
    ]
]:
    args: List[Any] = []
    query = """
SELECT * FROM publish_ready
"""
    conditions = []
    if suites is not None:
        args.append(suites)
        conditions.append("suite = ANY($%d::text[])" % len(args))
    if run_id is not None:
        args.append(run_id)
        conditions.append("id = $%d" % len(args))
    if review_status is not None:
        if not isinstance(review_status, list):
            review_status = [review_status]
        args.append(review_status)
        conditions.append("review_status = ANY($%d::review_status[])" % (len(args),))

    publishable_condition = (
        "exists (select from unnest(unpublished_branches) where "
        "mode in ('propose', 'attempt-push', 'push-derived', 'push'))"
    )

    order_by = []

    if publishable_only:
        conditions.append(publishable_condition)
    else:
        order_by.append(publishable_condition + " DESC")

    if needs_review is not None:
        args.append(needs_review)
        conditions.append('needs_review = $%d' % (len(args)))

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    order_by.extend(["value DESC NULLS LAST", "finish_time DESC"])

    if order_by:
        query += " ORDER BY " + ", ".join(order_by) + " "

    if limit is not None:
        query += " LIMIT %d" % limit
    for record in await conn.fetch(query, *args):
        yield tuple(  # type: ignore
            [Run.from_row(record),
             record['value'],
             record['maintainer_email'],
             record['uploader_emails'],
             record['update_changelog'],
             record['policy_command'],
             record['qa_review_policy'],
             record['needs_review'],
             record['unpublished_branches']
             ]
        )


async def get_publish_policy(
    conn: asyncpg.Connection, package: str, suite: str
) -> Tuple[Optional[Dict[str, Tuple[str, Optional[int]]]], Optional[str], Optional[List[str]]]:
    row = await conn.fetchrow(
        "SELECT publish, update_changelog, command "
        "FROM policy WHERE package = $1 AND suite = $2",
        package,
        suite,
    )
    if row:
        return (  # type: ignore
            {k: (v, f) for k, v, f in row['publish']},
            row['update_changelog'],
            row['command']
        )
    return None, None, None


async def iter_publishable_suites(
    conn: asyncpg.Connection,
    package: str
) -> List[
    Tuple[
        str,
    ]
]:
    query = """
SELECT DISTINCT candidate.suite
FROM candidate
INNER JOIN package on package.name = candidate.package
LEFT JOIN policy ON
    policy.package = package.name AND
    policy.suite = candidate.suite
WHERE NOT package.removed AND package.name = $1
"""
    return [
        row[0] for row in await conn.fetch(query, package)
    ]


async def has_cotenants(
    conn: asyncpg.Connection, package: str, url: str
) -> Optional[bool]:
    url = urlutils.split_segment_parameters(url)[0].rstrip("/")
    rows = await conn.fetch(
        "SELECT name FROM package where "
        "branch_url = $1 or "
        "branch_url like $1 || ',branch=%' or "
        "branch_url like $1 || '/,branch=%'",
        url,
    )
    if len(rows) > 1:
        return True
    elif len(rows) == 1 and rows[0][0] == package:
        return False
    else:
        # Uhm, we actually don't really know
        logging.warning("Unable to figure out if %s has cotenants on %s", package, url)
        return None

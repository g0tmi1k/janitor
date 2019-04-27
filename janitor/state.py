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

from debian.changelog import Version
import shlex
import psycopg2


conn = psycopg2.connect(
    database="janitor",
    user="janitor",
    port=5432,
    host="brangwain.vpn.jelmer.uk")
conn.set_client_encoding('UTF8')


def _ensure_package(cur, name, vcs_url, maintainer_email):
    cur.execute(
        "INSERT INTO package (name, branch_url, maintainer_email) "
        "VALUES (%s, %s, %s) ON CONFLICT (name) DO UPDATE SET "
        "branch_url = %s, maintainer_email = %s",
        (name, vcs_url, maintainer_email, vcs_url, maintainer_email))


def store_run(run_id, name, vcs_url, maintainer_email, start_time, finish_time,
              command, description, context, result_code, merge_proposal_url,
              build_version, build_distribution):
    """Store a run.

    :param run_id: Run id
    :param name: Package name
    :param vcs_url: Upstream branch URL
    :param maintainer_email: Maintainer email
    :param start_time: Start time
    :param finish_time: Finish time
    :param command: Command
    :param description: A human-readable description
    :param context: Subworker-specific context
    :param result_code: Result code (as constant string)
    :param merge_proposal_url: Optional merge proposal URL
    :param build_version: Version that was built
    :param build_distribution: Build distribution
    """
    cur = conn.cursor()
    _ensure_package(cur, name, vcs_url, maintainer_email)
    if merge_proposal_url:
        cur.execute(
            "INSERT INTO merge_proposal (url, package, status) "
            "VALUES (%s, %s, 'open') ON CONFLICT (url) DO UPDATE SET "
            "package = %s",
            (merge_proposal_url, name, name))
    else:
        merge_proposal_url = None
    cur.execute(
        "INSERT INTO run (id, command, description, result_code, start_time, "
        "finish_time, package, context, merge_proposal_url, build_version, "
        "build_distribution) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", (
            run_id, ' '.join(command), description, result_code,
            start_time, finish_time, name, context, merge_proposal_url,
            str(build_version) if build_version else None, build_distribution))
    conn.commit()


def iter_packages():
    cur = conn.cursor()
    cur.execute("""
SELECT
  name,
  maintainer_email,
  branch_url
FROM
  package
ORDER BY name ASC
""")
    return cur.fetchall()


def iter_runs(package=None):
    """Iterate over runs.

    Args:
      package: package to restrict to
    Returns:
      iterator over (
        run_id, (start_time, finish_time), command, description,
        package_name, merge_proposal_url, build_version, build_distribution,
        result_code)
    """
    cur = conn.cursor()
    query = """
SELECT
    run.id, command, start_time, finish_time, description, package.name,
    run.merge_proposal_url, build_version, build_distribution, result_code
FROM
    run
LEFT JOIN package ON package.name = run.package
"""
    args = ()
    if package is not None:
        query += " WHERE package.name = %s "
        args += (package,)
    query += "ORDER BY start_time DESC"
    cur.execute(query, args)
    row = cur.fetchone()
    while row:
        yield (row[0],
               (row[2], row[3]),
               row[1], row[4], row[5], row[6],
               Version(row[7]) if row[7] else None, row[8],
               row[9] if row[9] else None)
        row = cur.fetchone()


def get_maintainer_email(vcs_url):
    cur = conn.cursor()
    cur.execute(
        """
SELECT
    maintainer_email
FROM
    package
LEFT JOIN merge_proposal ON merge_proposal.package = package.name
WHERE
    merge_proposal.url = %s""",
        (vcs_url, ))
    row = cur.fetchone()
    if row:
        return row[0]
    return None


def iter_proposals(package=None):
    cur = conn.cursor()
    args = []
    query = """
SELECT
    package, url, status
FROM
    merge_proposal
LEFT JOIN package ON merge_proposal.package = package.name
"""
    if package:
        args.append(package)
        query += " WHERE package = %s"
    cur.execute(query, args)
    return cur.fetchall()


def iter_all_proposals():
    cur = conn.cursor()
    cur.execute("""
SELECT
    merge_proposal.url, merge_proposal.status, package.name
FROM
    merge_proposal
LEFT JOIN package ON merge_proposal.package = package.name
""")
    row = cur.fetchone()
    while row:
        yield row
        row = cur.fetchone()


def iter_queue(limit=None):
    cur = conn.cursor()
    query = """
SELECT
    package.branch_url,
    package.maintainer_email,
    package.name,
    queue.committer,
    queue.command,
    queue.mode,
    queue.context,
    queue.id
FROM
    queue
LEFT JOIN package ON package.name = queue.package
ORDER BY
queue.priority DESC,
queue.id ASC
"""
    if limit:
        query += " LIMIT %d" % limit
    cur.execute(query)
    for row in cur.fetchall():
        (branch_url, maintainer_email, package, committer,
            command, mode, context, queue_id) = row
        env = {
            'PACKAGE': package,
            'MAINTAINER_EMAIL': maintainer_email,
            'COMMITTER': committer or None,
            'CONTEXT': context,
        }
        yield (queue_id, branch_url, mode, env, shlex.split(command))


def drop_queue_item(queue_id):
    cur = conn.cursor()
    cur.execute("DELETE FROM queue WHERE id = %s", (queue_id,))
    conn.commit()


def add_to_queue(vcs_url, mode, env, command, priority=0):
    package = env['PACKAGE']
    maintainer_email = env.get('MAINTAINER_EMAIL')
    context = env.get('CONTEXT')
    committer = env.get('COMMITTER')
    cur = conn.cursor()
    _ensure_package(cur, package, vcs_url, maintainer_email)
    cur.execute(
        "INSERT INTO queue "
        "(package, command, committer, mode, priority, context) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT () DO UPDATE SET "
        "context = %s, priority = %s", (
            package, ' '.join(command), committer, mode,
            priority, context, context, priority))
    conn.commit()
    return True


def set_proposal_status(url, status):
    cur = conn.cursor()
    cur.execute("""
INSERT INTO merge_proposal (url, status) VALUES (%s, %s)
ON CONFLICT (url) DO UPDATE SET status = %s
""", (url, status, status))
    conn.commit()


def queue_length():
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM queue')
    return cur.fetchone()[0]


def iter_published_packages(suite):
    cur = conn.cursor()
    cur.execute("""
select distinct package, build_version from run where build_distribution = %s
""", (suite, ))
    return cur.fetchall()

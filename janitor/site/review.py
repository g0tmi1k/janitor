#!/usr/bin/python3

from asyncio import TimeoutError
from aiohttp import ClientConnectorError, ClientTimeout
import urllib.parse

from janitor import state
from . import (
    get_archive_diff,
    BuildDiffUnavailable,
    DebdiffRetrievalError,
    render_template_for_request,
)
from .common import get_unchanged_run

MAX_DIFF_SIZE = 200 * 1024


async def generate_rejected(conn, suite=None):
    if suite is None:
        suites = None
    else:
        suites = [suite]
    entries = [
        entry
        async for entry in state.iter_publish_ready(
            conn, review_status=["rejected"], needs_review=False, suites=suites, publishable_only=False
        )
    ]

    def entry_key(entry):
        return entry[0].finish_time

    entries.sort(key=entry_key, reverse=True)
    return {"entries": entries, "suite": suite}


async def generate_review(
    conn, request, client, differ_url, vcs_store_url, suites=None
):
    entries = [
        entry
        async for entry in state.iter_publish_ready(
            conn,
            review_status=["unreviewed"],
            needs_review=True,
            limit=10,
            suites=suites,
            publishable_only=True,
        )
    ]
    if not entries:
        return await render_template_for_request("review-done.html", request, {})

    (
        run,
        value,
        maintainer_email,
        uploader_emails,
        changelog_mode,
        command,
        qa_review_policy,
        needs_review,
        unpublished_branches,
    ) = entries.pop(0)

    async def show_diff(role):
        try:
            (remote_name, base_revid, revid) = run.get_result_branch(role)
        except KeyError:
            return ""
        if base_revid == revid:
            return ""
        url = urllib.parse.urljoin(vcs_store_url, "diff/%s/%s" % (run.id, role))
        external_url = "/api/run/%s/diff?role=%s" % (run.id, role)
        try:
            async with client.get(url, timeout=ClientTimeout(30)) as resp:
                if resp.status == 200:
                    diff = (await resp.read()).decode("utf-8", "replace")
                    if len(diff) > MAX_DIFF_SIZE:
                        return "Diff too large (%d). See it at %s" % (
                            len(diff),
                            external_url,
                        )
                    else:
                        return diff
                else:
                    return "Unable to retrieve diff; error %d" % resp.status
        except ClientConnectorError as e:
            return "Unable to retrieve diff; error %s" % e
        except TimeoutError:
            return "Timeout while retrieving diff; see it at %s" % external_url

    async def show_debdiff():
        unchanged_run = await get_unchanged_run(
            conn, run.package, run.main_branch_revision
        )
        if unchanged_run is None:
            return "<p>No control run</p>"
        try:
            text, unused_content_type = await get_archive_diff(
                client,
                differ_url,
                run.id,
                unchanged_run.id,
                kind="debdiff",
                filter_boring=True,
                accept="text/html",
            )
            return text.decode("utf-8", "replace")
        except DebdiffRetrievalError as e:
            return "Unable to retrieve debdiff: %r" % e
        except BuildDiffUnavailable:
            return "<p>No build diff generated</p>"

    kwargs = {
        "show_diff": show_diff,
        "show_debdiff": show_debdiff,
        "package_name": run.package,
        "run_id": run.id,
        "branches": run.result_branches,
        "suite": run.suite,
        "suites": suites,
        "MAX_DIFF_SIZE": MAX_DIFF_SIZE,
        "todo": [
            (entry[0].package, entry[0].id, [rb[0] for rb in entry[0].result_branches])
            for entry in entries
        ],
    }
    return await render_template_for_request("review.html", request, kwargs)

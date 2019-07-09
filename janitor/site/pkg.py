#!/usr/bin/python3

import functools
import os

from janitor import state
from janitor.build import (
    changes_filename,
    get_build_architecture,
)
from janitor.logs import LogFileManager
from janitor.sbuild_log import (
    parse_sbuild_log,
    find_failed_stage,
    find_build_failure_description,
    SBUILD_FOCUS_SECTION,
    strip_useless_build_tail,
)
from janitor.site import (
    changes_get_binaries,
    env,
    format_duration,
    get_changes_path,
    get_local_vcs_repo,
    get_run_diff,
    highlight_diff,
)
from janitor.trace import note
from janitor.vcs import (
    CACHE_URL_BZR,
    CACHE_URL_GIT,
    get_vcs_abbreviation,
)

FAIL_BUILD_LOG_LEN = 15

BUILD_LOG_NAME = 'build.log'
WORKER_LOG_NAME = 'worker.log'


def find_build_log_failure(logf, length):
    offsets = {}
    linecount = 0
    paragraphs = {}
    for title, offset, lines in parse_sbuild_log(logf):
        if title is not None:
            title = title.lower()
        paragraphs[title] = lines
        linecount = max(offset[1], linecount)
        offsets[title] = offset
    highlight_lines = []
    include_lines = None
    failed_stage = find_failed_stage(paragraphs.get('summary', []))
    focus_section = SBUILD_FOCUS_SECTION.get(failed_stage)
    if focus_section not in paragraphs:
        focus_section = None
    if focus_section:
        include_lines = (max(1, offsets[focus_section][1]-length),
                         offsets[focus_section][1])
    elif length < linecount:
        include_lines = (linecount-length, None)
    if focus_section == 'build':
        lines = paragraphs.get(focus_section, [])
        lines = strip_useless_build_tail(lines)
        include_lines = (max(1, offsets[focus_section][0] + len(lines)-length),
                         offsets[focus_section][0] + len(lines))
        offset, unused_line, unused_err = find_build_failure_description(lines)
        if offset is not None:
            highlight_lines = [offsets[focus_section][0] + offset]

    return (linecount, include_lines, highlight_lines)


def in_line_boundaries(i, boundaries):
    if boundaries is None:
        return True
    if boundaries[0] is not None and i < boundaries[0]:
        return False
    if boundaries[1] is not None and i > boundaries[1]:
        return False
    return True


async def generate_run_file(logfile_manager, run):
    (start_time, finish_time) = run.times
    kwargs = {}
    kwargs['run_id'] = run.id
    kwargs['command'] = run.command
    kwargs['description'] = run.description
    kwargs['package'] = run.package
    kwargs['start_time'] = run.times[0]
    kwargs['finish_time'] = run.times[1]
    kwargs['merge_proposal_url'] = run.merge_proposal_url
    kwargs['build_version'] = run.build_version
    kwargs['build_distribution'] = run.build_distribution
    kwargs['result_code'] = run.result_code
    kwargs['result'] = run.result
    kwargs['branch_name'] = run.branch_name
    kwargs['revision'] = run.revision
    kwargs['format_duration'] = format_duration
    kwargs['enumerate'] = enumerate
    kwargs['show_diff'] = lambda: get_run_diff(run).decode('utf-8')
    kwargs['highlight_diff'] = highlight_diff
    kwargs['max'] = max
    kwargs['suite'] = {
        'lintian-brush': 'lintian-fixes',
        'new-upstream': 'fresh-releases',
        'new-upstream --snapshot': 'fresh-snapshots'}.get(run.command)

    def read_file(f):
        return [l.decode('utf-8', 'replace') for l in f.readlines()]
    kwargs['read_file'] = read_file
    if run.build_version:
        kwargs['changes_name'] = changes_filename(
            run.package, run.build_version,
            get_build_architecture())
    else:
        kwargs['changes_name'] = None
    repo = get_local_vcs_repo(run.package)
    if repo:
        kwargs['vcs'] = get_vcs_abbreviation(repo)
    else:
        kwargs['vcs'] = None
    kwargs['cache_url_git'] = CACHE_URL_GIT
    kwargs['cache_url_bzr'] = CACHE_URL_BZR
    kwargs['binary_packages'] = []
    kwargs['in_line_boundaries'] = in_line_boundaries
    if kwargs['changes_name']:
        changes_path = get_changes_path(run, kwargs['changes_name'])
        if changes_path:
            for binary in changes_get_binaries(changes_path):
                kwargs['binary_packages'].append(binary)

    kwargs['get_log'] = functools.partial(
        logfile_manager.get_log, run.package, run.id)
    if logfile_manager.has_log(run.package, run.id, BUILD_LOG_NAME):
        kwargs['build_log_name'] = BUILD_LOG_NAME
        kwargs['earlier_build_log_names'] = []
        i = 1
        while logfile_manager.has_log(
                run.package, run.id, BUILD_LOG_NAME + '.%d' % i):
            log_name = '%s.%d' % (BUILD_LOG_NAME, i)
            kwargs['earlier_build_log_names'].append((i, log_name))
            i += 1

        line_count, include_lines, highlight_lines = find_build_log_failure(
            logfile_manager.get_log(run.package, run.id, BUILD_LOG_NAME),
            FAIL_BUILD_LOG_LEN)
        kwargs['build_log_line_count'] = line_count
        kwargs['build_log_include_lines'] = include_lines
        kwargs['build_log_highlight_lines'] = highlight_lines

    if logfile_manager.has_log(run.package, run.id, WORKER_LOG_NAME):
        kwargs['worker_log_name'] = WORKER_LOG_NAME

    template = env.get_template('run.html')
    text = await template.render_async(**kwargs)
    return text


async def write_run_file(logdirectory, dir, run):
    run_dir = os.path.join(dir, run.package, run.id)
    os.makedirs(run_dir, exist_ok=True)

    log_directory = os.path.join(logdirectory, run.package, run.id)
    build_log_path = os.path.join(log_directory, BUILD_LOG_NAME)
    if (not os.path.exists(os.path.join(run_dir, BUILD_LOG_NAME)) and
            os.path.exists(build_log_path)):
        os.symlink(build_log_path, os.path.join(run_dir, BUILD_LOG_NAME))

    worker_log_path = os.path.join(log_directory, WORKER_LOG_NAME)
    if (not os.path.exists(os.path.join(run_dir, WORKER_LOG_NAME)) and
            os.path.exists(worker_log_path)):
        os.symlink(worker_log_path, os.path.join(run_dir, WORKER_LOG_NAME))

    logfile_manager = LogFileManager(logdirectory)
    with open(os.path.join(run_dir, 'index.html'), 'w') as f:
        f.write(await generate_run_file(logfile_manager, run))
    note('Wrote %s', run_dir)


async def write_run_files(logdirectory, dir):
    runs_by_pkg = {}

    jobs = []
    async for run in state.iter_runs():
        jobs.append(write_run_file(logdirectory, dir, run))
        runs_by_pkg.setdefault(run.package, []).append(run)
    await asyncio.gather(*jobs)

    return runs_by_pkg


async def generate_pkg_file(
        name, merge_proposals, maintainer_email, branch_url, runs):
    kwargs = {}
    kwargs['package'] = name
    kwargs['maintainer_email'] = maintainer_email
    kwargs['vcs_url'] = branch_url
    kwargs['merge_proposals'] = merge_proposals
    kwargs['builds'] = [run for run in runs if run.build_version]
    kwargs['runs'] = runs
    template = env.get_template('package-overview.html')
    return await template.render_async(**kwargs)


async def write_pkg_file(dir, name, merge_proposals, maintainer_email,
                         branch_url, runs):
    pkg_dir = os.path.join(dir, name)
    if not os.path.isdir(pkg_dir):
        os.mkdir(pkg_dir)

    with open(os.path.join(pkg_dir, 'index.html'), 'w') as f:
        f.write(await generate_pkg_file(
            name, merge_proposals, maintainer_email, branch_url, runs))


async def write_pkg_files(dir, runs_by_pkg):
    merge_proposals = {}
    for package, url, status in await state.iter_proposals():
        merge_proposals.setdefault(package, []).append((url, status))

    jobs = []
    packages = []
    for (name, maintainer_email, branch_url) in await state.iter_packages():
        packages.append((name, maintainer_email))
        jobs.append(write_pkg_file(
            dir, name, merge_proposals.get(name, []),
            maintainer_email, branch_url, runs_by_pkg.get(name, [])))

    await asyncio.gather(*jobs)

    return packages


async def generate_pkg_list(packages):
    template = env.get_template('package-name-list.html')
    return await template.render_async(
        packages=[name for (name, maintainer) in packages])


async def generate_maintainer_list(packages):
    template = env.get_template('by-maintainer-package-list.html')
    by_maintainer = {}
    for name, maintainer in packages:
        by_maintainer.setdefault(maintainer, []).append(name)
    return await template.render_async(by_maintainer=by_maintainer)


async def write_pkg_list(dir, packages):
    with open(os.path.join(dir, 'index.html'), 'w') as f:
        f.write(await generate_pkg_list(packages))


async def generate_ready_list(suite):
    template = env.get_template('ready-list.html')
    runs = list(await state.iter_publish_ready(suite=suite))
    return await template.render_async(runs=runs, suite=suite)


if __name__ == '__main__':
    import argparse
    import asyncio
    parser = argparse.ArgumentParser(prog='report-pkg')
    parser.add_argument("logdirectory")
    parser.add_argument("directory")
    args = parser.parse_args()
    if not os.path.isdir(args.directory):
        os.mkdir(args.directory)
    loop = asyncio.get_event_loop()
    runs_by_pkg = loop.run_until_complete(
        write_run_files(args.logdirectory, args.directory))
    packages = loop.run_until_complete(
        write_pkg_files(args.directory, runs_by_pkg))
    loop.run_until_complete(write_pkg_list(args.directory, packages))

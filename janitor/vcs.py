#!/usr/bin/python3
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

from io import BytesIO
import logging
import os
from typing import Optional, List, Tuple, Iterable

import urllib.parse
import breezy.git  # noqa: F401
import breezy.bzr  # noqa: F401
from breezy import urlutils
from breezy.branch import Branch
from breezy.diff import show_diff_trees
from breezy.errors import (
    ConnectionError,
    NotBranchError,
    NoSuchRevision,
    NoRepositoryPresent,
    IncompatibleRepositories,
    InvalidHttpResponse,
)
from breezy.git.remote import RemoteGitError
from breezy.controldir import ControlDir, format_registry
from breezy.repository import InterRepository, Repository
from breezy.transport import Transport, get_transport
from lintian_brush.vcs import (
    determine_browser_url,
    unsplit_vcs_url,
)
from silver_platter.utils import (
    open_branch_containing,
    open_branch,
    full_branch_url,
    BranchMissing,
    BranchUnavailable,
    BranchRateLimited,
    BranchUnsupported,
)


SUPPORTED_VCSES = ["git", "bzr"]


class BranchOpenFailure(Exception):
    """Failure to open a branch."""

    def __init__(self, code: str, description: str):
        self.code = code
        self.description = description


def get_vcs_abbreviation(repository: Repository) -> str:
    vcs = getattr(repository, "vcs", None)
    if vcs:
        return vcs.abbreviation
    return "bzr"


def is_alioth_url(url: str) -> bool:
    return urllib.parse.urlparse(url).netloc in (
        "svn.debian.org",
        "bzr.debian.org",
        "anonscm.debian.org",
        "hg.debian.org",
        "git.debian.org",
        "alioth.debian.org",
    )


def _convert_branch_exception(vcs_url: str, e: Exception) -> Exception:
    if isinstance(e, BranchRateLimited):
        code = "too-many-requests"
    elif isinstance(e, BranchUnavailable):
        if "http code 429: Too Many Requests" in str(e):
            code = "too-many-requests"
        elif is_alioth_url(vcs_url):
            code = "hosted-on-alioth"
        elif "Unable to handle http code 401: Unauthorized" in str(
            e
        ) or "Unexpected HTTP status 401 for " in str(e):
            code = "401-unauthorized"
        elif "Unable to handle http code 502: Bad Gateway" in str(
            e
        ) or "Unexpected HTTP status 502 for " in str(e):
            code = "502-bad-gateway"
        elif str(e).startswith("Subversion branches are not yet"):
            code = "unsupported-vcs-svn"
        elif str(e).startswith("Mercurial branches are not yet"):
            code = "unsupported-vcs-hg"
        elif str(e).startswith("Darcs branches are not yet"):
            code = "unsupported-vcs-darcs"
        else:
            code = "branch-unavailable"
        msg = str(e)
        if e.url not in msg:
            msg = "%s (%s)" % (msg, e.url)
        return BranchOpenFailure(code, msg)
    if isinstance(e, BranchMissing):
        if str(e).startswith(
            "Branch does not exist: Not a branch: " '"https://anonscm.debian.org'
        ):
            code = "hosted-on-alioth"
        else:
            code = "branch-missing"
        msg = str(e)
        if e.url not in msg:
            msg = "%s (%s)" % (msg, e.url)
        return BranchOpenFailure(code, msg)
    if isinstance(e, BranchUnsupported):
        if str(e).startswith("Unsupported protocol for url "):
            if "anonscm.debian.org" in str(e) or "svn.debian.org" in str(e):
                code = "hosted-on-alioth"
            else:
                if "svn://" in str(e):
                    code = "unsupported-vcs-svn"
                elif "cvs+pserver://" in str(e):
                    code = "unsupported-vcs-cvs"
                else:
                    code = "unsupported-vcs-protocol"
        else:
            if str(e).startswith("Subversion branches are not yet"):
                code = "unsupported-vcs-svn"
            elif str(e).startswith("Mercurial branches are not yet"):
                code = "unsupported-vcs-hg"
            elif str(e).startswith("Darcs branches are not yet"):
                code = "unsupported-vcs-darcs"
            else:
                code = "unsupported-vcs"
        msg = str(e)
        if e.url not in msg:
            msg = "%s (%s)" % (msg, e.url)
        return BranchOpenFailure(code, msg)

    return e


def open_branch_ext(
    vcs_url: str, possible_transports: Optional[List[Transport]] = None, probers=None
) -> Branch:
    try:
        return open_branch(vcs_url, possible_transports, probers=probers)
    except (BranchUnavailable, BranchMissing, BranchUnsupported, BranchRateLimited) as e:
        raise _convert_branch_exception(vcs_url, e)


def open_branch_containing_ext(
    vcs_url: str, possible_transports: Optional[List[Transport]] = None, probers=None
) -> Tuple[Branch, str]:
    try:
        return open_branch_containing(vcs_url, possible_transports, probers=probers)
    except (BranchUnavailable, BranchMissing, BranchUnsupported, BranchRateLimited) as e:
        raise _convert_branch_exception(vcs_url, e)


class MirrorFailure(Exception):
    """Branch failed to mirror."""

    def __init__(self, branch_name: str, reason: str):
        self.branch_name = branch_name
        self.reason = reason


def mirror_branches(
    vcs_manager: "VcsManager",
    codebase: str,
    branch_map: Iterable[Tuple[str, Branch, bytes]],
    public_master_branch: Optional[Branch] = None,
) -> None:
    vcses = set(get_vcs_abbreviation(br.repository) for name, br, revid in branch_map)
    if len(vcses) == 0:
        return
    if len(vcses) > 1:
        raise AssertionError("more than one VCS: %r" % branch_map)
    vcs = vcses.pop()
    if vcs == "git":
        path = vcs_manager.get_repository_url(codebase, vcs)
        try:
            vcs_result_controldir = ControlDir.open(path)
        except NotBranchError:
            vcs_result_controldir = ControlDir.create(
                path, format=format_registry.get("git-bare")()
            )
        for (target_branch_name, from_branch, revid) in branch_map:
            # TODO(jelmer): Set depth
            try:
                vcs_result_controldir.push_branch(
                    from_branch,
                    name=target_branch_name,
                    overwrite=True,
                    revision_id=revid,
                )
            except NoSuchRevision as e:
                raise MirrorFailure(target_branch_name, e)
    elif vcs == "bzr":
        path = vcs_manager.get_repository_url(codebase, vcs)
        try:
            vcs_result_controldir = ControlDir.open(path)
        except NotBranchError:
            vcs_result_controldir = ControlDir.create(
                path, format=format_registry.get("bzr")()
            )
        try:
            vcs_result_controldir.open_repository()
        except NoRepositoryPresent:
            vcs_result_controldir.create_repository(shared=True)
        for (target_branch_name, from_branch, revid) in branch_map:
            target_branch_path = vcs_manager.get_branch_url(
                codebase, target_branch_name, vcs
            )
            try:
                target_branch = Branch.open(target_branch_path)
            except NotBranchError:
                target_branch = ControlDir.create_branch_convenience(target_branch_path)
            if public_master_branch:
                try:
                    target_branch.set_stacked_on_url(
                        full_branch_url(public_master_branch)
                    )
                except IncompatibleRepositories:
                    pass
            try:
                from_branch.push(target_branch, overwrite=True, stop_revision=revid)
            except NoSuchRevision as e:
                raise MirrorFailure(target_branch_name, e)
    else:
        raise AssertionError("unsupported vcs %s" % vcs)


def import_branches_git(
    vcs_manager, local_branch, package, suite, log_id, branches, tags
):
    repo_url = vcs_manager.get_repository_url(package, "git")

    # The server is expected to have repositories ready for us, so we don't create
    # the repository if it is missing.
    vcs_result_controldir = ControlDir.open(repo_url)

    repo = vcs_result_controldir.open_repository()

    def get_changed_refs(refs):
        changed_refs = {}
        for (fn, n, br, r) in branches:
            tagname = ("refs/tags/%s/%s" % (log_id, fn)).encode("utf-8")
            changed_refs[tagname] = (repo.lookup_bzr_revision_id(r)[0], r)
            branchname = ("refs/heads/%s/%s" % (suite, fn)).encode("utf-8")
            # TODO(jelmer): Ideally this would be a symref:
            changed_refs[branchname] = changed_refs[tagname]
        for taginfo in tags:
            if len(taginfo) == 3:
                (fn, n, r) = taginfo
            elif len(taginfo) == 2:
                (n, r) = taginfo
            else:
                raise ValueError(taginfo)
            tagname = ("refs/tags/%s" % (n, )).encode("utf-8")
            changed_refs[tagname] = (repo.lookup_bzr_revision_id(r)[0], r)
        return changed_refs

    inter = InterRepository.get(local_branch.repository, repo)
    inter.fetch_refs(get_changed_refs, lossy=False, overwrite=True)


def import_branches_bzr(
    vcs_manager, local_branch, package, suite, log_id, branches, tags
):
    for fn, n, br, r in branches:
        target_branch_path = vcs_manager.get_branch_url(package, suite, "bzr")
        if fn is not None:
            target_branch_path = urlutils.join_segment_parameters(
                target_branch_path,
                {"branch": urlutils.escape(fn, safe='')}).rstrip('/')
        transport = get_transport(target_branch_path)
        transport.create_prefix()
        try:
            target_branch = Branch.open_from_transport(transport)
        except NotBranchError:
            target_branch = ControlDir.create_branch_convenience(
                target_branch_path, possible_transports=[transport])
        try:
            local_branch.push(target_branch, overwrite=True)
        except NoSuchRevision as e:
            raise MirrorFailure(target_branch_path, e)

        target_branch.tags.set_tag(log_id, local_branch.last_revision())

        graph = target_branch.repository.get_graph()
        for name, revision in tags:
            # Only set tags on those branches where the revisions exist
            if graph.is_ancestor(revision, target_branch.last_revision()):
                target_branch.tags.set_tag(name, revision)


def import_branches(vcs_manager, local_branch, package, suite, log_id, branches, tags):
    vcs_type = get_vcs_abbreviation(local_branch.repository)
    if vcs_type == "git":
        import_branches_git(
            vcs_manager, local_branch, package, suite, log_id, branches, tags
        )
    elif vcs_type == "bzr":
        import_branches_bzr(
            vcs_manager, local_branch, package, suite, log_id, branches, tags
        )
    else:
        raise ValueError(vcs_type)


class UnsupportedVcs(Exception):
    """Specified vcs type is not supported."""


def get_cached_repository_url(base_url: str, vcs_type: str, package: str) -> str:
    if vcs_type in SUPPORTED_VCSES:
        return "%s/%s" % (base_url.rstrip("/"), package)
    else:
        raise UnsupportedVcs(vcs_type)


def get_cached_branch_url(
    base_url: str, vcs_type: str, package: str, branch_name: str
) -> str:
    if vcs_type == "git":
        return urlutils.join_segment_parameters("%s/%s" % (
            base_url.rstrip("/"), package), {
                "branch": urlutils.escape(branch_name, safe='')})
    elif vcs_type == "bzr":
        return "%s/%s/%s" % (base_url.rstrip("/"), package, branch_name)
    else:
        raise UnsupportedVcs(vcs_type)


def get_cached_branch(
    base_url: str, vcs_type: str, package: str, branch_name: str
) -> Optional[Branch]:
    try:
        url = get_cached_branch_url(base_url, vcs_type, package, branch_name)
    except UnsupportedVcs:
        return None
    try:
        return Branch.open(url)
    except NotBranchError:
        return None
    except RemoteGitError:
        return None
    except InvalidHttpResponse:
        return None
    except ConnectionError as e:
        logging.info("Unable to reach cache server: %s", e)
        return None


def get_local_vcs_branch_url(
    vcs_directory: str, vcs: str, codebase: str, branch_name: str
) -> Optional[str]:
    if vcs == "git":
        return urlutils.join_segment_parameters(
            "file:%s" % (
                os.path.join(vcs_directory, "git", codebase)), {
                    "branch": urlutils.escape(branch_name, safe='')})
    elif vcs == "bzr":
        return os.path.join(vcs_directory, "bzr", codebase, branch_name)
    else:
        raise AssertionError("unknown vcs type %r" % vcs)


def get_local_vcs_branch(vcs_directory: str, codebase: str, branch_name: str) -> Branch:
    for vcs in SUPPORTED_VCSES:
        if os.path.exists(os.path.join(vcs_directory, vcs, codebase)):
            break
    else:
        return None
    url = get_local_vcs_branch_url(vcs_directory, vcs, codebase, branch_name)
    if url is None:
        return None
    return open_branch(url)


def get_local_vcs_repo_url(vcs_directory: str, package: str, vcs_type: str) -> str:
    return os.path.join(vcs_directory, vcs_type, package)


def get_local_vcs_repo(
    vcs_directory: str, package: str, vcs_type: Optional[str] = None
) -> Optional[Repository]:
    for vcs in SUPPORTED_VCSES if not vcs_type else [vcs_type]:
        path = os.path.join(vcs_directory, vcs, package)
        if not os.path.exists(path):
            continue
        return Repository.open(path)
    return None


class VcsManager(object):
    def get_branch(
        self, codebase: str, branch_name: str, vcs_type: Optional[str] = None
    ) -> Branch:
        raise NotImplementedError(self.get_branch)

    def get_branch_url(
        self, codebase: str, branch_name: str, vcs_type: str
    ) -> Optional[str]:
        raise NotImplementedError(self.get_branch_url)

    def get_repository(
        self, codebase: str, vcs_type: Optional[str] = None
    ) -> Repository:
        raise NotImplementedError(self.get_repository)

    def get_repository_url(self, codebase: str, vcs_type: str) -> str:
        raise NotImplementedError(self.get_repository_url)

    def list_repositories(self, vcs_type: str) -> Iterable[str]:
        raise NotImplementedError(self.list_repositories)


class LocalVcsManager(VcsManager):
    def __init__(self, base_path: str):
        self.base_path = base_path

    def __repr__(self):
        return "%s(%r)" % (type(self).__name__, self.base_path)

    def get_branch(self, codebase, branch_name, vcs_type=None):
        try:
            return get_local_vcs_branch(self.base_path, codebase, branch_name)
        except (BranchUnavailable, BranchMissing):
            return None

    def get_branch_url(self, codebase, branch_name, vcs_type):
        return get_local_vcs_branch_url(self.base_path, vcs_type, codebase, branch_name)

    def get_repository(self, codebase, vcs_type=None):
        return get_local_vcs_repo(self.base_path, codebase, vcs_type)

    def get_repository_url(self, codebase, vcs_type):
        return get_local_vcs_repo_url(self.base_path, codebase, vcs_type)

    def list_repositories(self, vcs_type):
        for entry in os.scandir(os.path.join(self.base_path, vcs_type)):
            yield entry.name


class RemoteVcsManager(VcsManager):
    def __init__(self, git_base_url: str, bzr_base_url: str):
        self.bzr_base_url = bzr_base_url
        self.git_base_url = git_base_url

    @classmethod
    def from_single_url(cls, url: str):
        return cls(urlutils.join(url, 'git'), urlutils.join(url, 'bzr'))

    def __repr__(self):
        return "%s(%r, %r)" % (type(self).__name__, self.git_base_url, self.bzr_base_url)

    def get_branch(self, codebase, branch_name, vcs_type=None):
        if vcs_type == 'git':
            return get_cached_branch(self.git_base_url, codebase, branch_name, vcs_type)
        elif vcs_type == 'bzr':
            return get_cached_branch(self.bzr_base_url, codebase, branch_name, vcs_type)
        for vcs_type, base_url in [('git', self.git_base_url), ('bzr', self.bzr_base_url)]:
            branch = get_cached_branch(base_url, codebase, branch_name, vcs_type)
            if branch:
                return branch
        else:
            return None

    def get_branch_url(self, codebase, branch_name, vcs_type) -> str:
        if vcs_type == 'git':
            return get_cached_branch_url(self.git_base_url, vcs_type, codebase, branch_name)
        elif vcs_type == 'bzr':
            return get_cached_branch_url(self.bzr_base_url, vcs_type, codebase, branch_name)
        raise UnsupportedVcs(vcs_type)

    def get_repository_url(self, codebase: str, vcs_type: str) -> str:
        if vcs_type == 'git':
            return get_cached_repository_url(self.git_base_url, vcs_type, codebase)
        elif vcs_type == 'bzr':
            return get_cached_repository_url(self.bzr_base_url, vcs_type, codebase)
        raise UnsupportedVcs(vcs_type)


def get_run_diff(vcs_manager: VcsManager, run, role) -> bytes:
    f = BytesIO()
    try:
        repo = vcs_manager.get_repository(run.package)
    except NotBranchError:
        repo = None
    if repo is None:
        return b"Local VCS repository for %s temporarily inaccessible" % (
            run.package.encode("ascii")
        )
    for actual_role, _, base_revision, revision in run.result_branches:
        if role == actual_role:
            old_revid = base_revision
            new_revid = revision
            break
    else:
        return b"No branch with role %s" % role.encode()

    try:
        old_tree = repo.revision_tree(old_revid)
    except NoSuchRevision:
        return b"Old revision %s temporarily missing" % (old_revid,)
    try:
        new_tree = repo.revision_tree(new_revid)
    except NoSuchRevision:
        return b"New revision %s temporarily missing" % (new_revid,)
    show_diff_trees(old_tree, new_tree, to_file=f)
    return f.getvalue()


def get_vcs_manager(url: str) -> VcsManager:
    parsed = urlutils.URL.from_string(url)
    if parsed.scheme in ("", "file"):
        return LocalVcsManager(parsed.path)
    return RemoteVcsManager.from_single_url(url)


def bzr_to_browse_url(url: str) -> str:
    # TODO(jelmer): Use browse_url_from_repo_url from upstream_ontologist.vcs ?
    (url, params) = urlutils.split_segment_parameters(url)
    branch = params.get("branch")
    if branch:
        branch = urllib.parse.unquote(branch)
    deb_vcs_url = unsplit_vcs_url(url, branch)
    return determine_browser_url(None, deb_vcs_url)


def is_authenticated_url(url: str):
    return (url.startswith('git+ssh://') or url.startswith('bzr+ssh://'))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("url", type=str)
    args = parser.parse_args()
    branch = open_branch_ext(args.url)

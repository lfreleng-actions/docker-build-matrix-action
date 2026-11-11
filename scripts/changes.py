# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Change-scoped selection: the paths each image reads, and matching.

An image's input paths, relative to path_prefix, are its context, its
Dockerfile, each local ``--build-context`` in its ``build_flags``, and
the entries of its own ``paths`` list. A changed file touches an image
when it is one of those paths or lies below one of them.
"""

from __future__ import annotations

import json
import os
import posixpath
import shlex
import unicodedata
from collections.abc import Sequence
from typing import TYPE_CHECKING, NamedTuple

from scripts import linkpaths
from scripts.gha import ActionError
from scripts.graph import with_dependencies, with_dependents
from scripts.paths import within
from scripts.spec import Image, declared_paths, dockerfile_of, tag_file_of
from scripts.tagfile import FILENAME

if TYPE_CHECKING:
    # settings imports this module to parse changed_files.
    from scripts.settings import Settings

_OCI_LAYOUT = "oci-layout://"
# Sources buildx resolves remotely rather than from the local tree.
_REMOTE_PREFIXES = ("target:", "git@", "github.com/")


def parse_changed_files(raw: str) -> tuple[str, ...] | None:
    """The changed_files input, normalised; None when it is unset."""
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ActionError(
            "changed_files must be a JSON array of strings: paths relative "
            "to the repository root"
        )
    files: list[str] = []
    for item in data:
        shown = json.dumps(item)
        if not item:
            raise ActionError("changed_files holds an empty path")
        if any(unicodedata.category(char) == "Cc" for char in item):
            raise ActionError(
                f"changed_files entry {shown} contains a control character"
            )
        if item.startswith("/"):
            raise ActionError(
                f"changed_files entry {shown} is an absolute path; "
                "give paths relative to the repository root"
            )
        if ".." in item.split("/"):
            raise ActionError(f"changed_files entry {shown} has a '..' segment")
        normalised = posixpath.normpath(item)
        if normalised not in files:
            files.append(normalised)
    return tuple(files)


def invalid_paths(images: list[Image]) -> list[str]:
    """Names whose ``paths`` is neither absent nor a list of strings."""
    return [
        image["name"]
        for image in images
        if not (
            image.get("paths") is None
            or (
                isinstance(image["paths"], list)
                and all(isinstance(item, str) for item in image["paths"])
            )
        )
    ]


def _tokens(flags: object) -> list[str]:
    items = [flags] if isinstance(flags, str) else flags
    if not isinstance(items, list):
        return []
    tokens: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        try:
            tokens.extend(shlex.split(item))
        except ValueError:
            tokens.extend(item.split())
    return tokens


def _local_source(source: str) -> str | None:
    """The directory a build context source reads, if it is local."""
    if source.startswith(_OCI_LAYOUT):
        path = source[len(_OCI_LAYOUT) :].split("@", 1)[0]
        head, sep, last = path.rpartition("/")
        return head + sep + last.split(":", 1)[0]
    # buildx reads a '.git' suffix as a Git source only on a URL, which
    # the '://' test already covers; '../common.git' stays a directory.
    if "://" in source or source.startswith(_REMOTE_PREFIXES):
        return None
    return source


def build_context_paths(image: Image) -> list[str]:
    """Local ``--build-context name=path`` sources in ``build_flags``."""
    tokens = _tokens(image.get("build_flags"))
    values: list[str] = []
    for position, token in enumerate(tokens):
        if token == "--build-context" and position + 1 < len(tokens):
            values.append(tokens[position + 1])
        elif token.startswith("--build-context="):
            values.append(token[len("--build-context=") :])
    paths: list[str] = []
    for value in values:
        _, sep, source = value.partition("=")
        local = _local_source(source) if sep else None
        if local:
            paths.append(local)
    return paths


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _ignore_file(image: Image, root: str) -> list[str]:
    """The Dockerfile's own ignore file, where the context cannot cover it.

    BuildKit reads '<Dockerfile>.dockerignore' beside the Dockerfile in
    place of the context's '.dockerignore'. Beside a Dockerfile inside
    the context it lies inside the context too; beside one outside, it
    is an input of its own, listed even when absent, since adding it
    changes what the build sends. Both sides are compared where they
    lead: a link on the way, or a 'link/..' in the context, can put a
    Dockerfile that lies inside the context as text outside it.
    """
    ignore = f"{dockerfile_of(image)}.dockerignore"
    leads = os.path.realpath(os.path.join(root, ignore))
    context = os.path.realpath(os.path.join(root, image["context"]))
    if _under(leads, context):
        return []
    return [ignore]


def _inputs(image: Image, root: str, tag_files: bool) -> list[str]:
    """The image's input paths as the entry gives them.

    A local build context outside the workspace stays here, so that
    input_targets still finds the links inside the workspace that lead
    to it; input_paths leaves the directory itself out.

    With tag_files, the tag file sets the published tag, so it is an
    input too: a declared one, or the default one where it is a link,
    since a link can lead out of the context that otherwise covers it.
    """
    local = build_context_paths(image)
    tag_file = tag_file_of(image) if tag_files else ""
    default = posixpath.join(image["context"], FILENAME)
    if tag_files and not tag_file and os.path.islink(os.path.join(root, default)):
        tag_file = default
    tagged = [tag_file] if tag_file and not os.path.isabs(tag_file) else []
    return [
        image["context"],
        dockerfile_of(image),
        *_ignore_file(image, root),
        *local,
        *declared_paths(image),
        *tagged,
    ]


def input_paths(
    image: Image, workspace: str, root: str, tag_files: bool = False
) -> list[str]:
    """Everything the image's build reads, relative to path_prefix.

    Each path appears as written and, where a symbolic link makes them
    differ, as resolved. Where collapsing a '..' skips a link, adds
    one, or leads somewhere else, the collapsed form names a directory
    or link the build never reads, so the path appears with its '..'
    kept instead, and a replay of the list still reads what the build
    reads. A local build
    context outside the workspace cannot
    appear in a change, so it is left out rather than refused:
    build_flags predates this and may name a directory an earlier step
    created elsewhere. So is any form that
    resolves outside the workspace, such as 'link/../..' written as
    '..', which contain would refuse when the list is fed back as
    ``paths``; the form resolved inside stays.
    """
    paths: list[str] = []
    for path in _inputs(image, root, tag_files):
        lexical = linkpaths.lexical(path, workspace, root)
        target = linkpaths.resolved(path, workspace, root)
        relinked = set(linkpaths.links(path, workspace, root)) != set(
            linkpaths.links(lexical, workspace, root)
        )
        moved = linkpaths.resolved(lexical, workspace, root) != target
        written = (
            linkpaths.spelled(path, workspace, root) if relinked or moved else lexical
        )
        for entry in (written, target):
            if (
                entry is not None
                and entry not in paths
                and within(workspace, os.path.join(root, entry))
            ):
                paths.append(entry)
    return paths


def input_targets(
    image: Image, workspace: str, root: str, tag_files: bool = False
) -> list[str]:
    """Where the image's input paths lead, relative to the repository root.

    Each input path appears resolved, beside each link resolving it
    follows, since retargeting one changes what the image reads.
    Matching reads these rather than ``paths``: git names a file where
    it lives, and a written form such as 'link/..' collapses, as text,
    onto a directory the build never reads. The links stay out of
    ``paths``: fed back as a declared path, a link to a directory would
    read the whole target directory, not the part the image uses. A
    replay finds them again through the written forms ``paths`` keeps.
    """
    real_workspace = os.path.realpath(workspace)
    real_root = os.path.realpath(root)
    found: list[str] = []
    for path in _inputs(image, root, tag_files):
        for entry in (
            linkpaths.resolved(path, workspace, root),
            *linkpaths.links(path, workspace, root),
        ):
            if entry is None:
                continue
            location = os.path.relpath(
                os.path.join(real_root, entry), real_workspace
            ).replace(os.sep, "/")
            if location not in found:
                found.append(location)
    return found


def relative_to_prefix(changed: str, path_prefix: str) -> str:
    """A repository-root path, re-expressed relative to path_prefix."""
    return posixpath.relpath(changed, posixpath.normpath(path_prefix or "."))


def outside_prefix(relative: str) -> bool:
    """Whether a prefix-relative path lies above path_prefix."""
    return relative == ".." or relative.startswith("../")


def touches(changed: str, paths: list[str]) -> bool:
    """Whether a changed file is one of ``paths``, or lies below or above one.

    Both sides are relative to the repository root, so a context above
    path_prefix, such as ``..``, matches the files below it. A changed
    file above an input path can only be a link or a file replacing a
    directory on the way to it, either of which changes what it reads.
    """
    return any(
        path == "."
        or changed == path
        or changed.startswith(path + "/")
        or path.startswith(changed + "/")
        for path in paths
    )


def refuse_conflicts(settings: Settings) -> None:
    """Inputs that cannot combine with change-scoped selection."""
    if settings.changed_files is None:
        return
    if settings.build_command:
        raise ActionError(
            "changed_files cannot apply with build_command, which builds "
            "the images itself; set only one of them"
        )
    if settings.select:
        raise ActionError(
            "changed_files and select cannot be combined; set only one of them"
        )


class Scope(NamedTuple):
    """Where changed files are matched, all relative to the repository root.

    ``roots`` holds path_prefix as written and as resolved; ``links``
    holds the links path_prefix resolves through, a change to which
    touches every image (see linkpaths.prefixes and prefix_links);
    ``targets`` holds, per image, where its input paths lead and the
    links they pass through (see input_targets).
    """

    roots: Sequence[str]
    links: Sequence[str]
    targets: Sequence[Sequence[str]]


def select_changed(
    settings: Settings,
    images: list[Image],
    deps: Sequence[Sequence[int]] | None,
    messages: list[tuple[str, str]],
    scope: Scope,
) -> tuple[list[int], str]:
    """Indices of the images the changed files touch, and the reason."""
    roots, links = scope.roots, scope.links
    touched: set[int] = set()
    shared: list[str] = []
    for changed in settings.changed_files or ():
        hits = {
            i
            for i, targets in enumerate(scope.targets)
            if touches(changed, list(targets))
        }
        if images and touches(changed, list(links)):
            hits = set(range(len(images)))
            messages.append(
                (
                    "notice",
                    f"changed_files: '{changed}' is a link path_prefix "
                    "resolves through; it touches every image",
                )
            )
        touched |= hits
        inside = any(
            not outside_prefix(relative_to_prefix(changed, prefix)) for prefix in roots
        )
        if not hits and inside:
            shared.append(changed)
    if shared and images:
        for changed in shared:
            messages.append(
                (
                    "notice",
                    f"changed_files: '{changed}' is in no image's paths; "
                    "a shared change selects every image",
                )
            )
        return list(range(len(images))), "shared"
    chosen = touched
    if deps is not None and settings.select_dependents:
        chosen = with_dependents(chosen, deps)
    if deps is not None and settings.select_dependencies:
        chosen = with_dependencies(chosen, deps)
    if chosen:
        return sorted(chosen), "changed"
    if images:
        detail = f"changed_files touched none of the {len(images)} image(s)"
        if not settings.allow_empty:
            raise ActionError(detail)
        messages.append(("notice", f"{detail}; allow_empty permits an empty result"))
    return [], "none"

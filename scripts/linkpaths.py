# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Symbolic links on the way to an input path, and its written forms.

Change selection matches git's paths, which name a file where it lives,
against where an image's input paths lead. These read a path as the
build does, following every link before any '..' collapses.
"""

from __future__ import annotations

import os
import posixpath

from scripts.paths import within


def _under(path: str, base: str) -> bool:
    try:
        return os.path.commonpath([base, path]) == base
    except ValueError:
        return False


def _lead(workspace: str, root: str) -> str:
    """The resolved workspace, relative to the resolved root.

    A replay joins a relative path to root and resolves it, links
    first, so a '..' in it climbs from where root leads, not from
    where path_prefix is written.
    """
    return os.path.relpath(os.path.realpath(workspace), os.path.realpath(root)).replace(
        os.sep, "/"
    )


def _in_workspace(path: str, workspace: str) -> str | None:
    """An absolute path's spelling below the workspace, links kept."""
    for base in (os.path.abspath(workspace), os.path.realpath(workspace)):
        if path == base or path.startswith(base.rstrip(os.sep) + os.sep):
            return path[len(base) :].replace(os.sep, "/")
    return None


def lexical(path: str, workspace: str, root: str) -> str:
    """A path relative to root, symbolic links left as they are written.

    An absolute path keeps its own spelling where it lies under the
    workspace as given or as resolved, so a link it names stays a path
    a change to that link can match. It is made relative to the
    resolved root, which is where a replay reads it from.
    """
    if not os.path.isabs(path):
        return posixpath.normpath(path)
    tail = _in_workspace(os.path.normpath(path), workspace)
    if tail is not None:
        return posixpath.normpath(f"{_lead(workspace, root)}/{tail}")
    return os.path.relpath(os.path.realpath(path), os.path.realpath(root)).replace(
        os.sep, "/"
    )


def spelled(path: str, workspace: str, root: str) -> str | None:
    """A path relative to root as written, its '..' segments kept.

    Collapsing 'link/..' skips the link, so this is the form a replay
    has to read to find it again. None for an absolute path outside
    the workspace.
    """
    if os.path.isabs(path):
        tail = _in_workspace(path, workspace)
        if tail is None:
            return None
        path = f"{_lead(workspace, root)}/{tail}"
    parts = [part for part in path.split("/") if part not in ("", ".")]
    return "/".join(parts) or "."


def resolved(path: str, workspace: str, root: str) -> str | None:
    """Where a path leads once every link and '..' in it is followed.

    The path is resolved as written, before any '..' collapses, since
    'link/..' reads the parent of the link's target, not root. The
    result is relative to the resolved root, or None outside the
    workspace.
    """
    target = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
    if not within(workspace, target):
        return None
    return os.path.relpath(target, os.path.realpath(root)).replace(os.sep, "/")


def links(path: str, workspace: str, root: str) -> list[str]:
    """Each symbolic link inside the workspace that resolving a path follows.

    Retargeting any of them changes what the path reads, and git reports
    that as a change to the link alone, wherever the link lies: above
    the path, inside another link's target, or before a '..'. The result
    is relative to the resolved root, like resolved.
    """
    real_workspace = os.path.realpath(workspace)
    real_root = os.path.realpath(root)
    found: list[str] = []
    seen: set[str] = set()

    def walk(current: str, written: str) -> None:
        for part in written.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                current = os.path.dirname(current)
                continue
            location = os.path.join(current, part)
            if os.path.islink(location) and location not in seen:
                seen.add(location)
                if _under(location, real_workspace):
                    found.append(
                        os.path.relpath(location, real_root).replace(os.sep, "/")
                    )
                target = os.readlink(location)
                walk("/" if os.path.isabs(target) else current, target)
            current = os.path.realpath(location)

    try:
        walk("/" if os.path.isabs(path) else real_root, path)
    except (OSError, ValueError):
        return []
    return found


def prefixes(path_prefix: str, workspace: str, root: str) -> tuple[str, ...]:
    """path_prefix as written and as resolved, relative to the repository root.

    A changed file comes from git, which names a file by where it lives,
    not by a link above it, so a symbolic link prefix has to match
    through its target too. The written form counts only where it leads
    to root: 'link/..' collapses, as text, onto the link's directory,
    which the build never reads.
    """
    written = posixpath.normpath(path_prefix or ".")
    real_root = os.path.realpath(root)
    real = os.path.relpath(real_root, os.path.realpath(workspace)).replace(os.sep, "/")
    if (
        real == written
        or os.path.realpath(os.path.join(workspace, written)) != real_root
    ):
        return (real,)
    return (written, real)


def prefix_links(path_prefix: str, workspace: str) -> tuple[str, ...]:
    """Each symbolic link that resolving path_prefix itself follows.

    These sit above every image's input paths, which links walks from
    the resolved root, so retargeting one changes every image's build
    root without touching any image's paths. They stay out of the
    per-image paths so images_json fed back as images keeps its narrow
    selection. The result is relative to the repository root.
    """
    return tuple(links(path_prefix or ".", workspace, workspace))

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Keep every path the action reads inside the workspace."""

from __future__ import annotations

import os

from scripts.gha import ActionError
from scripts.spec import Image, dockerfile_of


def within(workspace: str, path: str) -> bool:
    """Whether ``path`` resolves, symbolic links included, inside it.

    A path that cannot be resolved at all, such as one carrying a NUL
    from an escaped JSON string, counts as outside.
    """
    try:
        base = os.path.realpath(workspace)
        return os.path.commonpath([base, os.path.realpath(path)]) == base
    except (OSError, ValueError):
        return False


def contain(workspace: str, root: str, images: list[Image]) -> None:
    """Refuse contexts or Dockerfiles resolving outside the workspace.

    The boundary is the workspace, not path_prefix: a context above the
    Dockerfile's directory, such as ../shared, is ordinary Docker usage,
    whereas a path out of the checkout would hand downstream builds and
    the Dockerfile reader files the repository does not own.
    """
    escapes = [
        f"{image['name']} ({field} '{path}')"
        for image in images
        for field, path in (
            ("context", image["context"]),
            ("dockerfile", dockerfile_of(image)),
        )
        if os.path.isabs(path) or not within(workspace, os.path.join(root, path))
    ]
    if escapes:
        raise ActionError(
            f"Image paths must stay inside the workspace: {'; '.join(escapes)}"
        )

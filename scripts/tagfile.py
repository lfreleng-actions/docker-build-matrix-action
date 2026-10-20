# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Per-image tags from container-tag.yaml, as global-jjb reads them.

global-jjb's ``yaml-file`` container tag method tags each image with
the ``.tag`` value of a container-tag.yaml file. The tag is
repository-controlled and reaches ``docker tag`` and ``crane``, so it
is parsed strictly rather than passed through.

The action carries no YAML library, so this reads a deliberate subset
of YAML: an optional leading ``---``, comments, blank lines, and
top-level ``key: value`` lines, one of them ``tag``. Its value is a
plain, single-quoted or double-quoted scalar on one line. Anything
else that could change what ``.tag`` means to a YAML parser (nesting,
continuation lines, flow collections, block scalars, anchors, tags,
escapes, further documents) fails, naming the file.
"""

from __future__ import annotations

import os
import re

from scripts.gha import ActionError
from scripts.paths import within
from scripts.spec import Image, tag_file_of

FILENAME = "container-tag.yaml"
# Docker's tag grammar (reference.TagRegexp, at most 128 characters).
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
# A tag file is a line or two; refuse to read anything much larger.
MAX_BYTES = 64 * 1024

_KEY_LINE = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_.-]*):(?:[ \t]+(.*))?")
_SCALAR = re.compile(r"""(?:'([^']*)'|"([^"\\]*)"|([^'"\s#][^#]*?))(?:[ \t]+#.*)?""")
_NULLS = frozenset({"null", "Null", "NULL", "~", ""})
# Plain scalars cannot start with these indicators.
_INDICATORS = frozenset("-?:,[]{}#&*!|>%@`")


def _line_value(raw: str) -> str | None:
    """The scalar a value holds, or None when it is not a simple one."""
    if raw[:1] in _INDICATORS and not (raw[:1] == "-" and raw[1:2] not in " \t"):
        return None
    match = _SCALAR.fullmatch(raw.rstrip(" \t"))
    if not match:
        return None
    single, double, plain = match.groups()
    if plain is not None:
        return None if plain in _NULLS else plain
    return single if single is not None else double


def parse(text: str, label: str) -> str:
    """The validated ``tag`` value; ``label`` names the file in errors."""
    text = text.removeprefix("\ufeff")
    tag: str | None = None
    seen_content = False
    for number, line in enumerate(text.split("\n"), 1):
        line = line.removesuffix("\r")
        stripped = line.strip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        where = f"{label} line {number}"
        if line.rstrip(" \t") == "---" and not seen_content:
            seen_content = True
            continue
        seen_content = True
        match = _KEY_LINE.fullmatch(line.rstrip(" \t"))
        if not match:
            raise ActionError(
                f"{where}: unsupported YAML; expected a top-level 'key: value' line"
            )
        key, raw = match.groups()
        if key != "tag":
            # A quoted value left open would run on into the next line,
            # which could then be the tag.
            if raw and raw[0] in "'\"" and not _SCALAR.fullmatch(raw.rstrip(" \t")):
                raise ActionError(f"{where}: unsupported quoted value for '{key}'")
            continue
        if tag is not None:
            raise ActionError(f"{where}: duplicate 'tag' key")
        value = _line_value(raw or "")
        if value is None:
            raise ActionError(
                f"{where}: 'tag' must be a single string scalar on one line"
            )
        if not TAG.fullmatch(value):
            raise ActionError(
                f"{where}: '{value}' is not a valid Docker tag "
                "([A-Za-z0-9_][A-Za-z0-9_.-], at most 128 characters)"
            )
        tag = value
    if tag is None:
        raise ActionError(f"{label}: no top-level 'tag' key")
    return tag


def read(path: str, label: str) -> str:
    """Read and parse the tag file at ``path``."""
    if not os.path.isfile(path):
        raise ActionError(f"{label}: not a regular file")
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError as err:
        raise ActionError(f"{label}: cannot read ({err.strerror})") from err
    if len(data) > MAX_BYTES:
        raise ActionError(f"{label}: larger than {MAX_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as err:
        raise ActionError(f"{label}: not valid UTF-8") from err
    return parse(text, label)


def with_tag(workspace: str, root: str, image: Image) -> Image:
    """The entry plus ``tag`` and ``tag_source`` from its tag file.

    A declared ``tag_file`` must exist; the default
    <context>/container-tag.yaml is optional, and its absence leaves
    both fields empty. Either way the file, symbolic links resolved,
    must lie inside the workspace.
    """
    name = image["name"]
    declared = tag_file_of(image)
    relative = declared or f"{image['context']}/{FILENAME}"
    path = os.path.join(root, relative)
    if os.path.isabs(relative) or not within(workspace, path):
        raise ActionError(
            f"{name}: tag file '{relative}' must resolve inside the workspace"
        )
    if not os.path.lexists(path):
        if declared:
            raise ActionError(f"{name}: tag file '{relative}' not found")
        return {**image, "tag": "", "tag_source": ""}
    try:
        tag = read(path, relative)
    except ActionError as err:
        raise ActionError(f"{name}: {err}") from err
    return {**image, "tag": tag, "tag_source": relative}

# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Change-scoped selection: changed_files, select_dependents, paths."""

from __future__ import annotations

import json
import tempfile
import unittest
from typing import Any

from tests.support import DOCKERFILE, Run, TreeTestCase, run_action

D = DOCKERFILE

# test-docker-monorepo's layout: chain-child builds FROM base-alpine.
MONOREPO = {
    "base-alpine/Dockerfile": "FROM alpine:3.22\n",
    "chain-child/Dockerfile": "ARG BASE_IMAGE=base-alpine:verify\nFROM ${BASE_IMAGE}\n",
    "util-echo/Dockerfile": D,
    "Makefile": "all:\n",
}


def names(run: Run) -> list[str]:
    return [image["name"] for image in json.loads(run.outputs["images_json"])]


def matrix(run: Run) -> list[dict[str, Any]]:
    return json.loads(run.outputs["matrix"])["include"]


def changed(*files: str) -> str:
    return json.dumps(list(files))


class ChangeSelectionTest(TreeTestCase):
    """Which images a set of changed files selects."""

    def setUp(self) -> None:
        super().setUp()
        self.tree(MONOREPO)

    def select(self, *files: str, **inputs: str) -> Run:
        run = run_action(self.workspace, changed_files=changed(*files), **inputs)
        self.assertEqual(run.status, 0, run.stdout)
        return run

    def test_feature_off_keeps_todays_outputs(self) -> None:
        plain = run_action(self.workspace)
        empty = run_action(self.workspace, changed_files="")
        self.assertEqual(plain.status, 0, plain.stdout)
        for key in set(plain.outputs) - {"build_id"}:
            self.assertEqual(empty.outputs[key], plain.outputs[key], key)
        self.assertEqual(empty.outputs["build_id"][:12], plain.outputs["build_id"][:12])
        self.assertEqual(empty.annotations, plain.annotations)
        self.assertEqual(plain.outputs["selection_reason"], "all")
        self.assertEqual(names(plain), ["base-alpine", "chain-child", "util-echo"])

    def test_leaf_change_selects_only_the_leaf(self) -> None:
        run = self.select("util-echo/Dockerfile")
        self.assertEqual(names(run), ["util-echo"])
        self.assertEqual(run.outputs["selection_reason"], "changed")

    def test_base_change_selects_its_dependents(self) -> None:
        run = self.select("base-alpine/motd.txt")
        self.assertEqual(names(run), ["base-alpine", "chain-child"])
        alone = self.select("base-alpine/motd.txt", select_dependents="false")
        self.assertEqual(names(alone), ["base-alpine"])

    def test_child_change_selects_its_bases(self) -> None:
        run = self.select("chain-child/Dockerfile")
        self.assertEqual(names(run), ["base-alpine", "chain-child"])
        alone = self.select("chain-child/Dockerfile", select_dependencies="false")
        self.assertEqual(names(alone), ["chain-child"])

    def test_shared_file_selects_everything(self) -> None:
        run = self.select("Makefile", "util-echo/Dockerfile")
        self.assertEqual(names(run), ["base-alpine", "chain-child", "util-echo"])
        self.assertEqual(run.outputs["selection_reason"], "shared")
        self.assertTrue(
            any(
                line.startswith("::notice::") and "'Makefile'" in line
                for line in run.annotations
            ),
            run.annotations,
        )

    def test_empty_list_selects_nothing(self) -> None:
        refused = run_action(self.workspace, changed_files="[]")
        self.assertEqual(refused.status, 1)
        self.assertTrue(
            any("changed_files touched none" in a for a in refused.annotations)
        )
        run = self.select(allow_empty="true")
        self.assertEqual(run.outputs["image_count"], "0")
        self.assertEqual(run.outputs["images_json"], "[]")
        self.assertEqual(run.outputs["selection_reason"], "none")

    def test_changed_files_change_the_build_id_stem(self) -> None:
        plain = run_action(self.workspace)
        scoped = self.select("util-echo/Dockerfile")
        self.assertNotEqual(
            scoped.outputs["build_id"][:12], plain.outputs["build_id"][:12]
        )


class PathPrefixTest(TreeTestCase):
    """Changed files are relative to the repository root."""

    def setUp(self) -> None:
        super().setUp()
        self.tree({f"sub/{path}": text for path, text in MONOREPO.items()})
        self.tree({"other/README.md": "x"})

    def test_paths_map_onto_path_prefix(self) -> None:
        run = run_action(
            self.workspace,
            path_prefix="sub",
            changed_files=changed("sub/util-echo/Dockerfile"),
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(names(run), ["util-echo"])

    def test_change_outside_path_prefix_is_not_shared(self) -> None:
        refused = run_action(
            self.workspace, path_prefix="sub", changed_files=changed("other/README.md")
        )
        self.assertEqual(refused.status, 1, refused.stdout)
        run = run_action(
            self.workspace,
            path_prefix="sub",
            changed_files=changed("other/README.md"),
            allow_empty="true",
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.outputs["image_count"], "0")
        self.assertEqual(run.outputs["selection_reason"], "none")

    def test_context_above_path_prefix_is_an_input(self) -> None:
        self.tree({"sub/app/Dockerfile": D, "lib/x.py": ""})
        images = json.dumps(
            [
                {"name": "app", "context": "../lib", "dockerfile": "app/Dockerfile"},
                {"name": "util-echo", "context": "util-echo"},
            ]
        )
        run = run_action(
            self.workspace,
            path_prefix="sub",
            images=images,
            changed_files=changed("lib/x.py"),
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(names(run), ["app"])

    def test_ancestor_context_overlapping_a_child(self) -> None:
        images = json.dumps(
            [
                {"name": "util-echo", "context": "util-echo"},
                {
                    "name": "wide",
                    "context": "..",
                    "dockerfile": "base-alpine/Dockerfile",
                },
            ]
        )
        for files, expected in (
            (("sub/util-echo/data.txt",), ["util-echo", "wide"]),
            (("other/README.md",), ["wide"]),
        ):
            with self.subTest(files=files):
                run = run_action(
                    self.workspace,
                    path_prefix="sub",
                    images=images,
                    changed_files=changed(*files),
                )
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(names(run), expected)

    def test_absolute_build_context_inside_the_workspace(self) -> None:
        source = self.workspace.resolve() / "other"
        images = json.dumps(
            [
                {
                    "name": "util-echo",
                    "context": "util-echo",
                    "build_flags": f"--build-context lib={source}",
                },
                {"name": "base-alpine", "context": "base-alpine"},
            ]
        )
        run = run_action(
            self.workspace,
            path_prefix="sub",
            images=images,
            changed_files=changed("other/README.md"),
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(names(run), ["util-echo"])
        self.assertIn("../other", matrix(run)[0]["paths"])

    def test_local_build_context_ending_in_git(self) -> None:
        self.tree({"common.git/x": ""})
        images = json.dumps(
            [
                {
                    "name": "util-echo",
                    "context": "util-echo",
                    "build_flags": "--build-context lib=../common.git",
                },
                {"name": "base-alpine", "context": "base-alpine"},
            ]
        )
        run = run_action(
            self.workspace,
            path_prefix="sub",
            images=images,
            changed_files=changed("common.git/x"),
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(names(run), ["util-echo"])

    def test_absolute_build_context_through_a_link(self) -> None:
        (self.workspace / "common-link").symlink_to("other")
        source = self.workspace.resolve() / "common-link"
        images = json.dumps(
            [
                {
                    "name": "util-echo",
                    "context": "util-echo",
                    "build_flags": f"--build-context lib={source}",
                },
                {"name": "base-alpine", "context": "base-alpine"},
            ]
        )
        for files in (("common-link",), ("other/README.md",)):
            with self.subTest(files=files):
                run = run_action(
                    self.workspace,
                    path_prefix="sub",
                    images=images,
                    changed_files=changed(*files),
                )
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(names(run), ["util-echo"])
                self.assertEqual(
                    matrix(run)[0]["paths"][-2:], ["../common-link", "../other"]
                )

    def test_link_to_a_build_context_outside_the_workspace(self) -> None:
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (self.workspace / "common-link").symlink_to(outside.name)
        images = json.dumps(
            [
                {
                    "name": "util-echo",
                    "context": "util-echo",
                    "build_flags": "--build-context lib=../common-link",
                },
                {"name": "base-alpine", "context": "base-alpine"},
            ]
        )
        run = run_action(
            self.workspace,
            path_prefix="sub",
            images=images,
            changed_files=changed("common-link"),
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(names(run), ["util-echo"])
        self.assertNotIn("../common-link", matrix(run)[0]["paths"])

    def test_parent_of_a_link_is_its_targets_parent(self) -> None:
        self.tree({"common/nested/y": "", "common/data.txt": ""})
        (self.workspace / "sub/link").symlink_to("../common/nested")
        for field in ("context", "paths", "build_flags"):
            with self.subTest(field=field):
                image: dict[str, Any] = {"name": "util-echo", "context": "util-echo"}
                if field == "context":
                    image.update(context="link/..", dockerfile="util-echo/Dockerfile")
                elif field == "paths":
                    image["paths"] = ["link/.."]
                else:
                    image["build_flags"] = "--build-context lib=link/.."
                run = run_action(
                    self.workspace,
                    path_prefix="sub",
                    images=json.dumps(
                        [image, {"name": "base-alpine", "context": "base-alpine"}]
                    ),
                    changed_files=changed("common/data.txt"),
                )
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(names(run), ["util-echo"])
                self.assertIn("../common", matrix(run)[0]["paths"])

    def test_parent_of_a_link_does_not_read_the_links_directory(self) -> None:
        self.tree({"common/nested/y": "", "common/data.txt": ""})
        (self.workspace / "sub/link").symlink_to("../common/nested")
        images = json.dumps(
            [
                {"name": "util-echo", "context": "util-echo", "paths": ["link/.."]},
                {"name": "base-alpine", "context": "base-alpine"},
            ]
        )
        plain = run_action(self.workspace, path_prefix="sub", images=images)
        self.assertEqual(plain.status, 0, plain.stdout)
        self.assertNotIn(".", matrix(plain)[0]["paths"])
        for source in (images, plain.outputs["images_json"]):
            for files, expected in (
                (("sub/base-alpine/Dockerfile",), ["base-alpine"]),
                (("common/data.txt",), ["util-echo"]),
                (("sub/link",), ["util-echo"]),
            ):
                with self.subTest(files=files, fed_back=source != images):
                    run = run_action(
                        self.workspace,
                        path_prefix="sub",
                        images=source,
                        changed_files=changed(*files),
                    )
                    self.assertEqual(run.status, 0, run.stdout)
                    self.assertEqual(names(run), expected)

    def test_ignore_file_through_a_linked_dockerfile_directory(self) -> None:
        self.tree({"templates/Dockerfile": D})
        (self.workspace / "sub/util-echo/templates").symlink_to("../../templates")
        images = json.dumps(
            [
                {
                    "name": "util-echo",
                    "context": "util-echo",
                    "dockerfile": "util-echo/templates/Dockerfile",
                },
                {"name": "base-alpine", "context": "base-alpine"},
            ]
        )
        plain = run_action(self.workspace, path_prefix="sub", images=images)
        self.assertEqual(plain.status, 0, plain.stdout)
        paths = matrix(plain)[0]["paths"]
        self.assertIn("util-echo/templates/Dockerfile.dockerignore", paths)
        self.assertIn("../templates/Dockerfile.dockerignore", paths)
        for source in (images, plain.outputs["images_json"]):
            with self.subTest(fed_back=source != images):
                run = run_action(
                    self.workspace,
                    path_prefix="sub",
                    images=source,
                    changed_files=changed("templates/Dockerfile.dockerignore"),
                )
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(names(run), ["util-echo"])
                self.assertEqual(run.outputs["selection_reason"], "changed")

    def test_ignore_file_beside_a_link_dot_dot_context(self) -> None:
        self.tree({"common/nested/y": ""})
        (self.workspace / "sub/link").symlink_to("../common/nested")
        images = json.dumps(
            [
                {
                    "name": "util-echo",
                    "context": "link/..",
                    "dockerfile": "util-echo/Dockerfile",
                },
                {"name": "base-alpine", "context": "base-alpine"},
            ]
        )
        plain = run_action(self.workspace, path_prefix="sub", images=images)
        self.assertEqual(plain.status, 0, plain.stdout)
        self.assertIn("util-echo/Dockerfile.dockerignore", matrix(plain)[0]["paths"])
        for source in (images, plain.outputs["images_json"]):
            with self.subTest(fed_back=source != images):
                run = run_action(
                    self.workspace,
                    path_prefix="sub",
                    images=source,
                    changed_files=changed("sub/util-echo/Dockerfile.dockerignore"),
                )
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(names(run), ["util-echo"])
                self.assertEqual(run.outputs["selection_reason"], "changed")

    def test_retargeting_an_intermediate_link(self) -> None:
        self.tree({"common/templates/Dockerfile": D, "elsewhere/x": ""})
        (self.workspace / "common-link").symlink_to("common")
        for field in ("build_flags", "dockerfile"):
            with self.subTest(field=field):
                image: dict[str, Any] = {"name": "util-echo", "context": "util-echo"}
                if field == "build_flags":
                    image["build_flags"] = (
                        "--build-context lib=../common-link/templates"
                    )
                else:
                    image["dockerfile"] = "../common-link/templates/Dockerfile"
                run = run_action(
                    self.workspace,
                    path_prefix="sub",
                    images=json.dumps(
                        [image, {"name": "base-alpine", "context": "base-alpine"}]
                    ),
                    changed_files=changed("common-link"),
                )
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(names(run), ["util-echo"])
                self.assertEqual(run.outputs["selection_reason"], "changed")

    def test_intermediate_link_round_trip_stays_narrow(self) -> None:
        self.tree({"common/templates/Dockerfile": D, "common/unrelated.txt": ""})
        (self.workspace / "common-link").symlink_to("common")
        images = json.dumps(
            [
                {
                    "name": "util-echo",
                    "context": "util-echo",
                    "dockerfile": "../common-link/templates/Dockerfile",
                },
                {"name": "base-alpine", "context": "base-alpine"},
            ]
        )
        plain = run_action(self.workspace, path_prefix="sub", images=images)
        self.assertEqual(plain.status, 0, plain.stdout)
        self.assertNotIn("../common-link", matrix(plain)[0]["paths"])
        for source in (images, plain.outputs["images_json"]):
            for files, expected in (
                (("common-link",), ["util-echo"]),
                (("common/templates/Dockerfile",), ["util-echo"]),
                (("common/unrelated.txt",), []),
            ):
                with self.subTest(files=files, fed_back=source != images):
                    run = run_action(
                        self.workspace,
                        path_prefix="sub",
                        images=source,
                        changed_files=changed(*files),
                        allow_empty="true",
                    )
                    self.assertEqual(run.status, 0, run.stdout)
                    self.assertEqual(names(run), expected)

    def test_link_before_dot_dot_survives_a_round_trip(self) -> None:
        self.tree({"common/templates/Dockerfile": D, "common/nested/x": ""})
        (self.workspace / "common-link").symlink_to("common/nested")
        images = json.dumps(
            [
                {
                    "name": "util-echo",
                    "context": "util-echo",
                    "dockerfile": "../common-link/../templates/Dockerfile",
                },
                {"name": "base-alpine", "context": "base-alpine"},
            ]
        )
        plain = run_action(self.workspace, path_prefix="sub", images=images)
        self.assertEqual(plain.status, 0, plain.stdout)
        self.assertIn(
            "../common-link/../templates/Dockerfile", matrix(plain)[0]["paths"]
        )
        for source in (images, plain.outputs["images_json"]):
            for files, expected in (
                (("common-link",), ["util-echo"]),
                (("common/templates/Dockerfile",), ["util-echo"]),
                (("common/nested/x",), []),
            ):
                with self.subTest(files=files, fed_back=source != images):
                    run = run_action(
                        self.workspace,
                        path_prefix="sub",
                        images=source,
                        changed_files=changed(*files),
                        allow_empty="true",
                    )
                    self.assertEqual(run.status, 0, run.stdout)
                    self.assertEqual(names(run), expected)

    def test_respelled_link_keeps_its_destination_on_replay(self) -> None:
        self.tree({"common/link/assets/x": "", "common/nested/assets/x": ""})
        (self.workspace / "sub/link").symlink_to("../common/nested")
        images = json.dumps(
            [
                {
                    "name": "util-echo",
                    "context": "util-echo",
                    "paths": ["link/../link/assets"],
                },
                {"name": "base-alpine", "context": "base-alpine"},
            ]
        )
        plain = run_action(self.workspace, path_prefix="sub", images=images)
        self.assertEqual(plain.status, 0, plain.stdout)
        self.assertNotIn("link/assets", matrix(plain)[0]["paths"])
        for source in (images, plain.outputs["images_json"]):
            for files, expected in (
                (("common/link/assets/x",), ["util-echo"]),
                (("common/nested/assets/x",), []),
            ):
                with self.subTest(files=files, fed_back=source != images):
                    run = run_action(
                        self.workspace,
                        path_prefix="sub",
                        images=source,
                        changed_files=changed(*files),
                        allow_empty="true",
                    )
                    self.assertEqual(run.status, 0, run.stdout)
                    self.assertEqual(names(run), expected)

    def test_collapsing_onto_an_added_link_keeps_the_written_form(self) -> None:
        self.tree({"common/link/assets/x": ""})
        (self.workspace / "common/nested").mkdir(parents=True)
        (self.workspace / "common/nested/assets").symlink_to("../link/assets")
        (self.workspace / "sub/link").symlink_to("../common/nested")
        images = json.dumps(
            [
                {
                    "name": "util-echo",
                    "context": "util-echo",
                    "paths": ["link/../link/assets"],
                },
                {"name": "base-alpine", "context": "base-alpine"},
            ]
        )
        plain = run_action(self.workspace, path_prefix="sub", images=images)
        self.assertEqual(plain.status, 0, plain.stdout)
        paths = matrix(plain)[0]["paths"]
        self.assertIn("link/../link/assets", paths)
        self.assertNotIn("link/assets", paths)
        for source in (images, plain.outputs["images_json"]):
            for files, expected in (
                (("common/link/assets/x",), ["util-echo"]),
                (("common/nested/assets",), []),
            ):
                with self.subTest(files=files, fed_back=source != images):
                    run = run_action(
                        self.workspace,
                        path_prefix="sub",
                        images=source,
                        changed_files=changed(*files),
                        allow_empty="true",
                    )
                    self.assertEqual(run.status, 0, run.stdout)
                    self.assertEqual(names(run), expected)

    def test_absolute_build_context_under_a_deeper_prefix_link(self) -> None:
        self.tree(
            {
                "nested/project/a/Dockerfile": D,
                "nested/project/b/Dockerfile": D,
                "nested/common/x": "",
                "common/x": "",
                "elsewhere/y": "",
            }
        )
        (self.workspace / "alias").symlink_to("nested/project")
        (self.workspace / "common-link").symlink_to("common")
        base = self.workspace.resolve()
        images = json.dumps(
            [
                {
                    "name": "a",
                    "context": "a",
                    "build_flags": (
                        f"--build-context lib={base / 'common'} "
                        f"--build-context alt={base / 'common-link'}"
                    ),
                },
                {"name": "b", "context": "b"},
            ]
        )
        plain = run_action(self.workspace, path_prefix="alias", images=images)
        self.assertEqual(plain.status, 0, plain.stdout)
        paths = matrix(plain)[0]["paths"]
        self.assertIn("../../common", paths)
        self.assertIn("../../common-link", paths)
        self.assertNotIn("../common", paths)
        for source in (images, plain.outputs["images_json"]):
            for files, expected in (
                (("common/x",), ["a"]),
                (("common-link",), ["a"]),
                (("nested/common/x",), []),
            ):
                with self.subTest(files=files, fed_back=source != images):
                    run = run_action(
                        self.workspace,
                        path_prefix="alias",
                        images=source,
                        changed_files=changed(*files),
                        allow_empty="true",
                    )
                    self.assertEqual(run.status, 0, run.stdout)
                    self.assertEqual(names(run), expected)

    def test_path_prefix_through_a_link_and_dot_dot(self) -> None:
        self.tree({f"project/{path}": text for path, text in MONOREPO.items()})
        self.tree({"project/nested/y": "", "sub/notes.txt": ""})
        (self.workspace / "sub/link").symlink_to("../project/nested")
        every = ["base-alpine", "chain-child", "util-echo"]
        for files, expected, reason in (
            (("sub/notes.txt",), [], "none"),
            (("project/Makefile",), every, "shared"),
            (("project/util-echo/Dockerfile",), ["util-echo"], "changed"),
            (("sub/link",), every, "changed"),
        ):
            with self.subTest(files=files):
                run = run_action(
                    self.workspace,
                    path_prefix="sub/link/..",
                    changed_files=changed(*files),
                    allow_empty="true",
                )
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(names(run), expected)
                self.assertEqual(run.outputs["selection_reason"], reason)

    def test_retargeting_a_link_inside_a_links_target(self) -> None:
        self.tree({"other/x": ""})
        (self.workspace / "outer").symlink_to("common")
        (self.workspace / "common").mkdir()
        (self.workspace / "common/inner").symlink_to("../other")
        images = json.dumps(
            [
                {
                    "name": "util-echo",
                    "context": "util-echo",
                    "build_flags": "--build-context lib=../outer/inner",
                },
                {"name": "base-alpine", "context": "base-alpine"},
            ]
        )
        for files in (("common/inner",), ("outer",), ("other/x",)):
            with self.subTest(files=files):
                run = run_action(
                    self.workspace,
                    path_prefix="sub",
                    images=images,
                    changed_files=changed(*files),
                )
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(names(run), ["util-echo"])

    def test_symlinked_path_prefix(self) -> None:
        (self.workspace / "alias").symlink_to("sub")
        for files, expected, reason in (
            (("sub/util-echo/data.txt",), ["util-echo"], "changed"),
            (("sub/Makefile",), ["base-alpine", "chain-child", "util-echo"], "shared"),
        ):
            with self.subTest(files=files):
                run = run_action(
                    self.workspace, path_prefix="alias", changed_files=changed(*files)
                )
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(names(run), expected)
                self.assertEqual(run.outputs["selection_reason"], reason)

    def test_retargeting_a_link_path_prefix_resolves_through(self) -> None:
        (self.workspace / "outer").symlink_to("common")
        (self.workspace / "common").mkdir()
        (self.workspace / "common/inner").symlink_to("../sub")
        every = ["base-alpine", "chain-child", "util-echo"]
        plain = run_action(self.workspace, path_prefix="outer/inner")
        self.assertEqual(plain.status, 0, plain.stdout)
        fed_back = {"images": plain.outputs["images_json"]}
        for inputs in ({}, fed_back):
            for files, expected in (
                (("common/inner",), every),
                (("outer",), every),
                (("sub/util-echo/Dockerfile",), ["util-echo"]),
                (("other/README.md",), []),
            ):
                with self.subTest(files=files, fed_back=bool(inputs)):
                    run = run_action(
                        self.workspace,
                        path_prefix="outer/inner",
                        changed_files=changed(*files),
                        allow_empty="true",
                        **inputs,
                    )
                    self.assertEqual(run.status, 0, run.stdout)
                    self.assertEqual(names(run), expected)


class InputPathsTest(TreeTestCase):
    """Dockerfiles, build contexts and declared paths as inputs."""

    def setUp(self) -> None:
        super().setUp()
        self.tree(
            {
                "a/Dockerfile": D,
                "b/Dockerfile": D,
                "dockerfiles/c.Dockerfile": D,
                "common/lib/x": "",
                "scripts/build.sh": "",
            }
        )

    def run_images(self, images: list[dict[str, Any]], *files: str) -> Run:
        run = run_action(
            self.workspace, images=json.dumps(images), changed_files=changed(*files)
        )
        self.assertEqual(run.status, 0, run.stdout)
        return run

    def test_explicit_dockerfile_outside_context(self) -> None:
        images = [
            {"name": "a", "context": "a"},
            {"name": "c", "context": "a", "dockerfile": "dockerfiles/c.Dockerfile"},
        ]
        run = self.run_images(images, "dockerfiles/c.Dockerfile")
        self.assertEqual(names(run), ["c"])

    def test_dockerfile_ignore_file_outside_context(self) -> None:
        images = [
            {"name": "a", "context": "a"},
            {"name": "c", "context": "a", "dockerfile": "dockerfiles/c.Dockerfile"},
        ]
        ignore = "dockerfiles/c.Dockerfile.dockerignore"
        for present in (False, True):
            with self.subTest(present=present):
                if present:
                    self.tree({ignore: "*.md\n"})
                run = self.run_images(images, ignore)
                self.assertEqual(names(run), ["c"])
                self.assertEqual(
                    matrix(run)[0]["paths"],
                    ["a", "dockerfiles/c.Dockerfile", ignore],
                )
        inside = self.run_images(images[:1], "a/Dockerfile.dockerignore")
        self.assertEqual(matrix(inside)[0]["paths"], ["a", "a/Dockerfile"])

    def test_local_build_context(self) -> None:
        for flags in (
            "--build-context lib=common/lib",
            "--build-context=lib=./common/lib/",
            ["--build-context", "lib=common/lib"],
            "--build-context lib=oci-layout://common/lib:v1@sha256:abc",
        ):
            with self.subTest(flags=flags):
                images: list[dict[str, Any]] = [
                    {"name": "a", "context": "a", "build_flags": flags},
                    {"name": "b", "context": "b"},
                ]
                run = self.run_images(images, "common/lib/x")
                self.assertEqual(names(run), ["a"])
                self.assertIn("common/lib", matrix(run)[0]["paths"])

    def test_symlinked_dockerfile_matches_its_target(self) -> None:
        (self.workspace / "alias.Dockerfile").symlink_to("b/Dockerfile")
        images: list[dict[str, Any]] = [
            {"name": "a", "context": "a", "dockerfile": "alias.Dockerfile"},
            {"name": "b", "context": "b"},
        ]
        run = self.run_images(images, "b/Dockerfile")
        self.assertEqual(names(run), ["a", "b"])
        self.assertEqual(
            matrix(run)[0]["paths"],
            [
                "a",
                "alias.Dockerfile",
                "b/Dockerfile",
                "alias.Dockerfile.dockerignore",
            ],
        )
        link = self.run_images(images, "alias.Dockerfile")
        self.assertEqual(names(link), ["a"])

    def test_declared_tag_file_is_an_input(self) -> None:
        self.tree({"b/release.yaml": "tag: 1.0\n", "tags/c.yaml": "tag: 2.0\n"})
        (self.workspace / "tag-link.yaml").symlink_to("tags/c.yaml")
        images: list[dict[str, Any]] = [
            {"name": "a", "context": "a", "tag_file": "b/release.yaml"},
            {"name": "b", "context": "b"},
            {
                "name": "c",
                "context": "dockerfiles",
                "dockerfile": "dockerfiles/c.Dockerfile",
                "tag_file": "tag-link.yaml",
            },
        ]
        for files, expected in (
            (("b/release.yaml",), ["a", "b"]),
            (("tags/c.yaml",), ["c"]),
            (("tag-link.yaml",), ["c"]),
        ):
            with self.subTest(files=files):
                run = run_action(
                    self.workspace,
                    images=json.dumps(images),
                    tag_files="true",
                    changed_files=changed(*files),
                )
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(names(run), expected)
        listed = json.loads(
            run_action(
                self.workspace, images=json.dumps(images), tag_files="true"
            ).outputs["images_json"]
        )
        self.assertEqual(listed[0]["paths"], ["a", "a/Dockerfile", "b/release.yaml"])
        self.assertIn("tags/c.yaml", listed[2]["paths"])
        off = self.run_images(images[:2], "b/release.yaml")
        self.assertEqual(names(off), ["b"])
        plain = run_action(self.workspace, images=json.dumps(images[:2]))
        self.assertEqual(matrix(plain)[0]["paths"], ["a", "a/Dockerfile"])

    def test_default_tag_file_link_is_an_input(self) -> None:
        self.tree({"b/release.yaml": "tag: 1.0\n"})
        (self.workspace / "a/container-tag.yaml").symlink_to("../b/release.yaml")
        images: list[dict[str, Any]] = [
            {"name": "a", "context": "a"},
            {"name": "b", "context": "b"},
        ]
        plain = run_action(self.workspace, images=json.dumps(images), tag_files="true")
        self.assertEqual(plain.status, 0, plain.stdout)
        self.assertIn("b/release.yaml", matrix(plain)[0]["paths"])
        for source in (json.dumps(images), plain.outputs["images_json"]):
            with self.subTest(fed_back=source != json.dumps(images)):
                run = run_action(
                    self.workspace,
                    images=source,
                    tag_files="true",
                    changed_files=changed("b/release.yaml"),
                )
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(names(run), ["a", "b"])
        off = self.run_images(images, "b/release.yaml")
        self.assertEqual(names(off), ["b"])

    def test_declared_paths_narrow_a_shared_change(self) -> None:
        images: list[dict[str, Any]] = [
            {"name": "a", "context": "a"},
            {"name": "b", "context": "b"},
        ]
        shared = self.run_images(images, "scripts/build.sh")
        self.assertEqual(shared.outputs["selection_reason"], "shared")
        self.assertEqual(names(shared), ["a", "b"])
        images[1]["paths"] = ["scripts/"]
        scoped = self.run_images(images, "scripts/build.sh")
        self.assertEqual(scoped.outputs["selection_reason"], "changed")
        self.assertEqual(names(scoped), ["b"])

    def test_paths_output(self) -> None:
        images = [
            {
                "name": "a",
                "context": "a",
                "build_flags": (
                    "--build-context img=docker-image://alpine:3 "
                    "--build-context web=https://example.com/x.tar "
                    "--build-context git=git@github.com:o/r.git "
                    "--build-context tgt=target:base "
                    "--build-context abs=/tmp/elsewhere"
                ),
            },
            {"name": "b", "context": "b", "paths": ["scripts", "a/../common/lib"]},
        ]
        run = run_action(self.workspace, images=json.dumps(images))
        self.assertEqual(run.status, 0, run.stdout)
        listed = json.loads(run.outputs["images_json"])
        self.assertEqual(listed[0]["paths"], ["a", "a/Dockerfile"])
        self.assertEqual(
            listed[1]["paths"], ["b", "b/Dockerfile", "scripts", "common/lib"]
        )
        self.assertEqual(
            [entry["paths"] for entry in matrix(run)],
            [image["paths"] for image in listed],
        )
        # images_json is the input schema, so it feeds back unchanged.
        again = run_action(self.workspace, images=run.outputs["images_json"])
        self.assertEqual(again.outputs["images_json"], run.outputs["images_json"])

    def test_paths_escaping_through_a_link_round_trip(self) -> None:
        self.tree({"common/nested/y": ""})
        (self.workspace / "link").symlink_to("common/nested")
        images = [{"name": "b", "context": "b", "paths": ["link/../.."]}]
        run = run_action(self.workspace, images=json.dumps(images))
        self.assertEqual(run.status, 0, run.stdout)
        paths = json.loads(run.outputs["images_json"])[0]["paths"]
        self.assertNotIn("..", paths)
        self.assertIn(".", paths)
        again = run_action(self.workspace, images=run.outputs["images_json"])
        self.assertEqual(again.status, 0, again.stdout)
        repeated = json.loads(again.outputs["images_json"])[0]["paths"]
        self.assertTrue(set(paths) <= set(repeated), repeated)

    def test_discovered_images_carry_paths(self) -> None:
        run = run_action(self.workspace)
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(
            [entry["paths"] for entry in matrix(run)],
            [["a", "a/Dockerfile"], ["b", "b/Dockerfile"]],
        )

    def test_bad_declared_paths_are_refused(self) -> None:
        for paths in ('"scripts"', "[1]", '["/etc"]', '["../../.."]'):
            with self.subTest(paths=paths):
                run = run_action(
                    self.workspace,
                    images=f'[{{"name":"a","context":"a","paths":{paths}}}]',
                )
                self.assertEqual(run.status, 1, run.stdout)
                self.assertTrue(
                    any(
                        a.startswith("::error::") and "paths" in a
                        for a in run.annotations
                    ),
                    run.annotations,
                )


class RefusalTest(TreeTestCase):
    """Input changed_files cannot take or combine with."""

    def test_refusals(self) -> None:
        self.tree(MONOREPO)
        cases: dict[str, dict[str, str]] = {
            "malformed JSON": {"changed_files": "[not json"},
            "not an array": {"changed_files": '{"a":1}'},
            "non-string entry": {"changed_files": '["a", 1]'},
            "absolute path": {"changed_files": '["/etc/passwd"]'},
            "'..' segment": {"changed_files": '["a/../../b"]'},
            "control character": {"changed_files": '["a\\u0007b"]'},
            "empty path": {"changed_files": '[""]'},
            "build_command": {
                "changed_files": '["a"]',
                "build_command": "make images",
            },
            "select": {"changed_files": '["a"]', "select": "util-echo"},
        }
        for case, inputs in cases.items():
            with self.subTest(case=case):
                run = run_action(self.workspace, **inputs)
                self.assertEqual(run.status, 1, run.stdout)
                errors = [a for a in run.annotations if a.startswith("::error::")]
                self.assertEqual(len(errors), 1, run.annotations)
                self.assertIn("changed_files", errors[0])
                self.assertNotIn("images_json", run.outputs)

    def test_select_dependents_is_a_strict_boolean(self) -> None:
        self.tree(MONOREPO)
        run = run_action(self.workspace, changed_files="[]", select_dependents="maybe")
        self.assertEqual(run.status, 1)
        self.assertIn("select_dependents", run.annotations[0])


if __name__ == "__main__":
    unittest.main()

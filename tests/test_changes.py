# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Change-scoped selection: changed_files, select_dependents, paths."""

from __future__ import annotations

import json
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

    @unittest.expectedFailure
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

    @unittest.expectedFailure
    def test_leaf_change_selects_only_the_leaf(self) -> None:
        run = self.select("util-echo/Dockerfile")
        self.assertEqual(names(run), ["util-echo"])
        self.assertEqual(run.outputs["selection_reason"], "changed")

    @unittest.expectedFailure
    def test_base_change_selects_its_dependents(self) -> None:
        run = self.select("base-alpine/motd.txt")
        self.assertEqual(names(run), ["base-alpine", "chain-child"])
        alone = self.select("base-alpine/motd.txt", select_dependents="false")
        self.assertEqual(names(alone), ["base-alpine"])

    @unittest.expectedFailure
    def test_child_change_selects_its_bases(self) -> None:
        run = self.select("chain-child/Dockerfile")
        self.assertEqual(names(run), ["base-alpine", "chain-child"])
        alone = self.select("chain-child/Dockerfile", select_dependencies="false")
        self.assertEqual(names(alone), ["chain-child"])

    @unittest.expectedFailure
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

    @unittest.expectedFailure
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

    @unittest.expectedFailure
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

    @unittest.expectedFailure
    def test_paths_map_onto_path_prefix(self) -> None:
        run = run_action(
            self.workspace,
            path_prefix="sub",
            changed_files=changed("sub/util-echo/Dockerfile"),
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(names(run), ["util-echo"])

    @unittest.expectedFailure
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

    @unittest.expectedFailure
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

    @unittest.expectedFailure
    def test_explicit_dockerfile_outside_context(self) -> None:
        images = [
            {"name": "a", "context": "a"},
            {"name": "c", "context": "a", "dockerfile": "dockerfiles/c.Dockerfile"},
        ]
        run = self.run_images(images, "dockerfiles/c.Dockerfile")
        self.assertEqual(names(run), ["c"])

    @unittest.expectedFailure
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

    @unittest.expectedFailure
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

    @unittest.expectedFailure
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

    @unittest.expectedFailure
    def test_discovered_images_carry_paths(self) -> None:
        run = run_action(self.workspace)
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(
            [entry["paths"] for entry in matrix(run)],
            [["a", "a/Dockerfile"], ["b", "b/Dockerfile"]],
        )

    @unittest.expectedFailure
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

    @unittest.expectedFailure
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

    @unittest.expectedFailure
    def test_select_dependents_is_a_strict_boolean(self) -> None:
        self.tree(MONOREPO)
        run = run_action(self.workspace, changed_files="[]", select_dependents="maybe")
        self.assertEqual(run.status, 1)
        self.assertIn("select_dependents", run.annotations[0])


if __name__ == "__main__":
    unittest.main()

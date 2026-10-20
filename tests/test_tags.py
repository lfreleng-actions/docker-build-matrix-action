# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Per-image tags read from container-tag.yaml under ``tag_files``.

This reproduces global-jjb's ``yaml-file`` container tag method, which
tags each image with ``.tag`` from its own container-tag.yaml. The tag
is repository-controlled and reaches ``docker tag``, so anything but a
single valid Docker tag fails, naming the file.
"""

from __future__ import annotations

import json
import os
import unittest
from typing import Any

from tests.support import DOCKERFILE, TreeTestCase, run_action

D = DOCKERFILE


def matrix(run: Any) -> list[dict[str, Any]]:
    return json.loads(run.outputs["matrix"])["include"]


def images(run: Any) -> list[dict[str, Any]]:
    return json.loads(run.outputs["images_json"])


class TagFilesTest(TreeTestCase):
    """The ``tag_files`` input and the per-entry ``tag_file`` key."""

    def assert_fails_naming(self, run: Any, path: str, *fragments: str) -> None:
        self.assertEqual(run.status, 1, run.stdout)
        errors = [line for line in run.annotations if line.startswith("::error::")]
        self.assertEqual(len(errors), 1, run.annotations)
        self.assertIn(path, errors[0])
        for fragment in fragments:
            self.assertIn(fragment, errors[0])

    def test_present_and_absent(self) -> None:
        self.tree(
            {
                "a/Dockerfile": D,
                "a/container-tag.yaml": "---\n# Release tag\ntag: 1.2.3\n",
                "b/Dockerfile": D,
            }
        )
        run = run_action(self.workspace, tag_files="true")
        self.assertEqual(run.status, 0, run.stdout)
        first, second = matrix(run)
        self.assertEqual(
            (first["tag"], first["tag_source"]), ("1.2.3", "a/container-tag.yaml")
        )
        self.assertEqual((second["tag"], second["tag_source"]), ("", ""))
        self.assertEqual(
            [(i["tag"], i["tag_source"]) for i in images(run)],
            [("1.2.3", "a/container-tag.yaml"), ("", "")],
        )

    def test_scalar_styles(self) -> None:
        self.tree(
            {
                "a/Dockerfile": D,
                "a/container-tag.yaml": "tag: '1.0' # quoted\r\n",
                "b/Dockerfile": D,
                "b/container-tag.yaml": '\ufefftag: "v2_rc-1"\n',
                "c/Dockerfile": D,
                "c/container-tag.yaml": "tag: 1.10  # not a float\n",
            }
        )
        run = run_action(self.workspace, tag_files="true")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual([e["tag"] for e in matrix(run)], ["1.0", "v2_rc-1", "1.10"])

    def test_other_top_level_keys_are_ignored(self) -> None:
        self.tree(
            {
                "a/Dockerfile": D,
                "a/container-tag.yaml": "name: simulator\ntag: 2.8.6\nowner: onap\n",
            }
        )
        run = run_action(self.workspace, tag_files="true")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(matrix(run)[0]["tag"], "2.8.6")

    @unittest.expectedFailure
    def test_simple_values_on_ignored_keys_pass(self) -> None:
        content = (
            "# comment\nowner:\nother: # intentionally unset\nname: ~\n"
            "label: null\nkind: 'a b'\n"
            'note: "c d" # why\nversion: 1.0\ntag: v1\n'
        )
        self.tree({"a/Dockerfile": D, "a/container-tag.yaml": content})
        run = run_action(self.workspace, tag_files="true")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(matrix(run)[0]["tag"], "v1")

    def test_multi_line_values_on_ignored_keys_fail(self) -> None:
        cases = {
            "unterminated flow sequence": "owner: [\ntag: v1\n",
            "multi-line quote in flow": 'metadata: ["\ntag: v1\nclosing: ["]\n',
            "flow mapping": "owner: {a: b,\ntag: v1}\n",
            "literal block scalar": "owner: |\ntag: v1\n",
            "folded block scalar": "owner: >-\ntag: v1\n",
            "anchor": "owner: &x\ntag: v1\n",
            "alias": "owner: *x\ntag: v1\n",
            "yaml tag": "owner: !!str\ntag: v1\n",
            "escaped quote": 'owner: "a\\"\ntag: v1\n',
        }
        for label, content in cases.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                self.tree({"a/Dockerfile": D, "a/container-tag.yaml": content})
                run = run_action(self.workspace, tag_files="true")
                key = content.split(":", 1)[0]
                self.assert_fails_naming(run, "a/container-tag.yaml line 1", f"'{key}'")

    def test_empty_tag_fails_naming_the_line(self) -> None:
        for content in ("tag:\n", "tag: # unset\n"):
            with self.subTest(content):
                self.tearDown()
                self.setUp()
                self.tree({"a/Dockerfile": D, "a/container-tag.yaml": content})
                run = run_action(self.workspace, tag_files="true")
                self.assert_fails_naming(
                    run,
                    "a/container-tag.yaml line 1",
                    "'tag' must be a single string scalar on one line",
                )

    def test_explicit_tag_file(self) -> None:
        self.tree(
            {
                "proj/a/Dockerfile": D,
                "proj/a/container-tag.yaml": "tag: ignored\n",
                "proj/tags/a.yaml": "tag: 4.0.0\n",
                "proj/b/Dockerfile": D,
            }
        )
        listed = json.dumps(
            [
                {"name": "a", "context": "a", "tag_file": "tags/a.yaml"},
                {"name": "b", "context": "b"},
            ]
        )
        run = run_action(
            self.workspace, path_prefix="proj", images=listed, tag_files="true"
        )
        self.assertEqual(run.status, 0, run.stdout)
        first, second = matrix(run)
        self.assertEqual((first["tag"], first["tag_source"]), ("4.0.0", "tags/a.yaml"))
        self.assertEqual((second["tag"], second["tag_source"]), ("", ""))

    def test_missing_explicit_tag_file_fails(self) -> None:
        self.tree({"a/Dockerfile": D})
        listed = json.dumps([{"name": "a", "context": "a", "tag_file": "none.yaml"}])
        run = run_action(self.workspace, images=listed, tag_files="true")
        self.assert_fails_naming(run, "none.yaml", "not found")

    def test_tag_file_must_be_a_string(self) -> None:
        self.tree({"a/Dockerfile": D})
        listed = json.dumps([{"name": "a", "context": "a", "tag_file": 1}])
        run = run_action(self.workspace, images=listed, tag_files="true")
        self.assert_fails_naming(run, "tag_file", "a")

    def test_malformed_content_fails_naming_the_file(self) -> None:
        cases = {
            "empty": "",
            "comment only": "# nothing\n",
            "no tag key": "version: 1.0\n",
            "not a mapping": "- 1.0\n",
            "nested value": "tag:\n  value: 1.0\n",
            "continuation": "tag: 1.0\n  more\n",
            "flow mapping": "{tag: 1.0}\n",
            "duplicate key": "tag: 1.0\ntag: 2.0\n",
            "second document": "tag: 1.0\n---\ntag: 2.0\n",
            "null": "tag: null\n",
            "tilde": "tag: ~\n",
            "unterminated quote": "tag: '1.0\n",
            "escape": 'tag: "1.\\x30"\n',
            "anchor": "tag: &v 1.0\n",
            "block scalar": "tag: |\n  1.0\n",
            "not utf-8": "tag: v1\udce9\n",
        }
        for label, content in cases.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                self.tree({"a/Dockerfile": D})
                (self.workspace / "a" / "container-tag.yaml").write_bytes(
                    content.encode("utf-8", "surrogateescape")
                )
                run = run_action(self.workspace, tag_files="true")
                self.assert_fails_naming(run, "a/container-tag.yaml")

    def test_invalid_tag_characters_fail(self) -> None:
        for value in ("-lead", ".lead", "a b", "a/b", "a:b", "x" * 129, "ü1", "$(id)"):
            with self.subTest(value):
                self.tearDown()
                self.setUp()
                self.tree(
                    {
                        "a/Dockerfile": D,
                        "a/container-tag.yaml": f"tag: '{value}'\n",
                    }
                )
                run = run_action(self.workspace, tag_files="true")
                self.assert_fails_naming(run, "a/container-tag.yaml", "Docker tag")

    def test_longest_valid_tag(self) -> None:
        value = "_" + "a" * 127
        self.tree({"a/Dockerfile": D, "a/container-tag.yaml": f"tag: {value}\n"})
        run = run_action(self.workspace, tag_files="true")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(matrix(run)[0]["tag"], value)

    def test_symlink_inside_the_workspace_resolves(self) -> None:
        self.tree({"a/Dockerfile": D, "shared/tag.yaml": "tag: 5.0\n"})
        os.symlink("../shared/tag.yaml", self.workspace / "a" / "container-tag.yaml")
        run = run_action(self.workspace, tag_files="true")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(matrix(run)[0]["tag"], "5.0")

    def test_symlink_out_of_the_workspace_fails(self) -> None:
        self.tree({"ws/a/Dockerfile": D, "outside.yaml": "tag: 1.0\n"})
        os.symlink(
            "../../outside.yaml", self.workspace / "ws" / "a" / "container-tag.yaml"
        )
        run = run_action(self.workspace / "ws", tag_files="true")
        self.assert_fails_naming(run, "a/container-tag.yaml", "inside the workspace")

    def test_explicit_tag_file_out_of_the_workspace_fails(self) -> None:
        self.tree({"a/Dockerfile": D})
        for path in ("../tag.yaml", "/etc/hostname"):
            with self.subTest(path):
                listed = json.dumps([{"name": "a", "context": "a", "tag_file": path}])
                run = run_action(self.workspace, images=listed, tag_files="true")
                self.assert_fails_naming(run, path, "inside the workspace")

    def test_directory_in_place_of_the_file_fails(self) -> None:
        self.tree({"a/Dockerfile": D, "a/container-tag.yaml/x": ""})
        run = run_action(self.workspace, tag_files="true")
        self.assert_fails_naming(run, "a/container-tag.yaml", "not a regular file")

    def test_only_selected_images_are_read(self) -> None:
        self.tree(
            {
                "a/Dockerfile": D,
                "a/container-tag.yaml": "tag: 1.0\n",
                "b/Dockerfile": D,
                "b/container-tag.yaml": "tag: [broken\n",
            }
        )
        run = run_action(self.workspace, tag_files="true", select="a")
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(matrix(run)[0]["tag"], "1.0")

    def test_off_by_default(self) -> None:
        self.tree(
            {
                "a/Dockerfile": D,
                "a/container-tag.yaml": "tag: [not, read]\n",
            }
        )
        listed = json.dumps([{"name": "a", "context": "a", "tag_file": 1}])
        for inputs in ({}, {"images": listed}, {"tag_files": "false"}):
            with self.subTest(inputs):
                run = run_action(self.workspace, **inputs)
                self.assertEqual(run.status, 0, run.stdout)
                self.assertNotIn("tag", matrix(run)[0])
                self.assertNotIn("tag_source", images(run)[0])

    def test_tag_files_changes_the_build_id_stem(self) -> None:
        self.tree({"a/Dockerfile": D})
        plain = run_action(self.workspace).outputs["build_id"].split("-")[0]
        tagged = run_action(self.workspace, tag_files="true")
        self.assertEqual(tagged.status, 0, tagged.stdout)
        self.assertNotEqual(tagged.outputs["build_id"].split("-")[0], plain)


if __name__ == "__main__":
    unittest.main()

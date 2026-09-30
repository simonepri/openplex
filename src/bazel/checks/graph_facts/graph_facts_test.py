"""Test parsing of the check graph query and the answers derived from it."""

from __future__ import annotations

import json
import unittest

from graph_facts import Graph, dev_tool_query, graph_query, parse_graph, reached_locks

LOCK = "//:requirements_dev_lock.txt"


def rule(name: str, *inputs: str) -> str:
    return json.dumps({"type": "RULE", "rule": {"name": name, "ruleInput": list(inputs)}})


def source(name: str) -> str:
    return json.dumps({"type": "SOURCE_FILE", "sourceFile": {"name": name}})


def generated(name: str, generating_rule: str) -> str:
    return json.dumps({
        "type": "GENERATED_FILE",
        "generatedFile": {"name": name, "generatingRule": generating_rule},
    })


class GraphFactsTest(unittest.TestCase):
    def test_parse_keeps_source_files_and_edges(self) -> None:
        graph = parse_graph([
            rule("//a:image", "//a:layer.tar"),
            generated("//a:layer.tar", "//a:layer"),
            source("//a:main.py"),
            source("//a:main.py"),
            "",
        ])
        self.assertEqual(graph.source_files, ["//a:main.py"])
        self.assertEqual(graph.deps["//a:image"], ["//a:layer.tar"])
        self.assertEqual(graph.deps["//a:layer.tar"], ["//a:layer"])

    def test_walk_follows_generated_files_to_the_lock(self) -> None:
        graph = parse_graph([
            rule("//a:image", "//a:layer.tar"),
            generated("//a:layer.tar", "//a:layer"),
            rule("//a:layer", LOCK),
            source(LOCK),
        ])
        self.assertEqual(reached_locks(graph, ["//a:image"], ["requirements_dev_lock.txt"]), [LOCK])

    def test_walk_ignores_locks_reached_from_other_targets(self) -> None:
        graph = parse_graph([
            rule("//a:image", "//a:main.py"),
            rule("//tools:lint", LOCK),
            source("//a:main.py"),
            source(LOCK),
        ])
        self.assertEqual(reached_locks(graph, ["//a:image"], ["requirements_dev_lock.txt"]), [])

    def test_unknown_spelling_leaves_the_answer_open(self) -> None:
        graph = Graph(source_files=[LOCK], deps={LOCK: [], "//a:a": []})
        self.assertIsNone(reached_locks(graph, ["//a"], ["requirements_dev_lock.txt"]))

    def test_dev_tool_query_matches_the_gate_spelling(self) -> None:
        self.assertEqual(
            dev_tool_query(["//a:image", "//b:image"], ["requirements_dev_lock.txt", "x/y.txt"]),
            "deps(set(//a:image //b:image )) intersect set(//:requirements_dev_lock.txt //x:y.txt)",
        )

    def test_graph_query_drops_source_files_from_paths(self) -> None:
        self.assertEqual(
            graph_query(["//a:image"], ["requirements_dev_lock.txt"]),
            'kind("source file", deps(//...)) + //...'
            " + let paths = allpaths(set(//a:image), set(//:requirements_dev_lock.txt))"
            ' in $paths except kind("source file", $paths)',
        )


if __name__ == "__main__":
    unittest.main()

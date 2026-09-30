"""Test AST and regex parsing logic for function length enforcement across Python, Go, and Shell sources."""

from __future__ import annotations

import unittest
from pathlib import Path

from check_function_length import (
    find_long_bash_functions,
    find_long_go_functions,
    find_long_python_functions,
    is_test_file,
    scan_file,
)

EXPECTED_LONG_LENGTH = 12
GO_FUNC_START_LINE = 3


class FunctionLengthTest(unittest.TestCase):
    """Verifies function length scanning logic."""

    @staticmethod
    def test_identifies_test_files_for_exclusion() -> None:
        assert is_test_file("pkg/client_test.go")
        assert is_test_file("src/tools/test_helper.py")
        assert is_test_file("src/tools/helper_test.py")
        assert is_test_file("src/web/app.test.ts")
        assert is_test_file("src/infra/definitions/conformance/scripts/run.sh")

        assert not is_test_file("pkg/client.go")
        assert not is_test_file("src/tools/helper.py")
        assert not is_test_file("src/web/app.ts")
        assert not is_test_file("src/infra/deploy.sh")

    @staticmethod
    def test_go_accepts_short_functions() -> None:
        content = "package main\n\nfunc ShortFunction() {\n    println(1)\n}\n"
        violations = find_long_go_functions(content, max_lines=5)
        assert violations == []

    @staticmethod
    def test_go_denies_long_functions() -> None:
        body = "\n".join(f"    stmt{i}()" for i in range(10))
        content = f"package main\n\nfunc (r *Reader) LongFunction() error {{\n{body}\n}}\n"
        violations = find_long_go_functions(content, max_lines=5)
        assert len(violations) == 1
        name, start_line, length = violations[0]
        assert name == "LongFunction"
        assert start_line == GO_FUNC_START_LINE
        assert length == EXPECTED_LONG_LENGTH

    @staticmethod
    def test_python_accepts_short_functions() -> None:
        content = "def short_fn():\n    a = 1\n    return a\n"
        violations = find_long_python_functions(content, "sample.py", max_lines=5)
        assert violations == []

    @staticmethod
    def test_python_denies_long_functions() -> None:
        body = "\n".join(f"    x{i} = {i}" for i in range(10))
        content = f"def long_fn():\n{body}\n    return x0\n"
        violations = find_long_python_functions(content, "sample.py", max_lines=5)
        assert len(violations) == 1
        name, start_line, length = violations[0]
        assert name == "long_fn"
        assert start_line == 1
        assert length == EXPECTED_LONG_LENGTH

    @staticmethod
    def test_bash_accepts_short_functions() -> None:
        content = "setup_env() {\n  export A=1\n}\n"
        violations = find_long_bash_functions(content, max_lines=5)
        assert violations == []

    @staticmethod
    def test_bash_denies_long_functions() -> None:
        body = "\n".join(f"  echo {i}" for i in range(10))
        content = f"function run_job() {{\n{body}\n}}\n"
        violations = find_long_bash_functions(content, max_lines=5)
        assert len(violations) == 1
        name, start_line, length = violations[0]
        assert name == "run_job"
        assert start_line == 1
        assert length == EXPECTED_LONG_LENGTH

    @staticmethod
    def test_scan_file_ignores_test_files() -> None:
        path = Path("src/something/service_test.go")
        violations = scan_file(path, max_lines=5)
        assert violations == []


if __name__ == "__main__":
    unittest.main()

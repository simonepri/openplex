#!/usr/bin/env python3
"""Test directory name normalization, DNS-1123 label projection, and collision avoidance algorithms."""

from __future__ import annotations

import re
import unittest
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

from check_source_names import (
    SourceNameError,
    validate_directories,
)

OVERLONG_NAME_LENGTH = 64


@contextmanager
def _assert_raises(expected_type: type[BaseException], match: str | None = None) -> Iterator[None]:
    try:
        yield
    except expected_type as err:
        if match is not None and not re.search(match, str(err)):
            msg = f"Expected exception matching {match!r}, got {err!r}"
            raise AssertionError(msg) from err
    else:
        msg = f"Expected {expected_type.__name__} but no exception was raised"
        raise AssertionError(msg)


class SourceNameTest(unittest.TestCase):
    @staticmethod
    def test_hyphenated_directory_is_rejected() -> None:
        with _assert_raises(SourceNameError, match=r"bad-name.*snake_case"):
            validate_directories({("bad-name",)})

    @staticmethod
    def test_underscore_hyphen_projection_collision_is_rejected() -> None:
        with _assert_raises(SourceNameError, match=r"not injective.*foo-bar"):
            validate_directories({("foo_bar",), ("foo-bar",)})

    @staticmethod
    def test_overlong_projection_is_rejected() -> None:
        long_name = "a" * OVERLONG_NAME_LENGTH
        with _assert_raises(SourceNameError, match=r"DNS-1123-safe"):
            validate_directories({(long_name,)})


if __name__ == "__main__":
    unittest.main()

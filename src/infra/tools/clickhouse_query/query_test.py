"""Unit tests for the read-only ClickHouse query tool."""

from __future__ import annotations

import io
import unittest
import urllib.error
from unittest import mock

from infra.tools.clickhouse_query.query import (
    execute_clickhouse_query,
    fetch_secret_password,
    main,
    validate_readonly_query,
)


class ValidateReadonlyQueryTest(unittest.TestCase):
    def test_valid_select(self) -> None:
        validate_readonly_query("SELECT 1")
        validate_readonly_query("select count() from signoz_logs.distributed_logs_v2")

    def test_valid_with_clause(self) -> None:
        validate_readonly_query("WITH a AS (SELECT 1) SELECT * FROM a")

    def test_valid_show_and_describe(self) -> None:
        validate_readonly_query("SHOW TABLES IN signoz_logs")
        validate_readonly_query("DESCRIBE TABLE signoz_logs.distributed_logs_v2")
        validate_readonly_query("DESC signoz_logs.distributed_logs_v2")
        validate_readonly_query("EXPLAIN SELECT 1")
        validate_readonly_query("EXISTS TABLE signoz_metrics.distributed_samples_v4")

    def test_valid_comments(self) -> None:
        validate_readonly_query("-- A single line comment\nSELECT 1")
        validate_readonly_query("/* A block comment */ SELECT 1")

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            validate_readonly_query("")
        with self.assertRaises(ValueError):
            validate_readonly_query("   -- only comments\n   ")

    def test_rejects_drop(self) -> None:
        with self.assertRaises(ValueError):
            validate_readonly_query("DROP TABLE signoz_logs.distributed_logs_v2")

    def test_rejects_alter(self) -> None:
        with self.assertRaises(ValueError):
            validate_readonly_query("ALTER TABLE test ADD COLUMN c Int32")

    def test_rejects_insert(self) -> None:
        with self.assertRaises(ValueError):
            validate_readonly_query("INSERT INTO test VALUES (1, 2)")

    def test_rejects_delete(self) -> None:
        with self.assertRaises(ValueError):
            validate_readonly_query("DELETE FROM test WHERE id = 1")

    def test_rejects_truncate(self) -> None:
        with self.assertRaises(ValueError):
            validate_readonly_query("TRUNCATE TABLE test")

    def test_rejects_create(self) -> None:
        with self.assertRaises(ValueError):
            validate_readonly_query("CREATE TABLE test (id UInt32) ENGINE = MergeTree")

    def test_rejects_stacked_mutation(self) -> None:
        with self.assertRaises(ValueError):
            validate_readonly_query("SELECT 1; DROP TABLE test")


class ExecuteClickhouseQueryTest(unittest.TestCase):
    @mock.patch("urllib.request.urlopen")
    def test_executes_query_successfully(self, mock_urlopen: mock.MagicMock) -> None:
        mock_response = mock.MagicMock()
        mock_response.read.return_value = b"1\n"
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        result = execute_clickhouse_query(
            "SELECT 1",
            url="http://127.0.0.1:8123",
            user="admin",
            output_format="TabSeparated",
        )
        self.assertEqual(result, "1\n")

        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_header("X-clickhouse-user"), "admin")
        self.assertNotIn("password", req.full_url)
        self.assertIn("readonly=1", req.full_url)
        self.assertIn("default_format=TabSeparated", req.full_url)
        self.assertEqual(req.data, b"SELECT 1")

    @mock.patch("urllib.request.urlopen")
    def test_bypass_client_guard(self, mock_urlopen: mock.MagicMock) -> None:
        mock_response = mock.MagicMock()
        mock_response.read.return_value = b"OK\n"
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        result = execute_clickhouse_query(
            "INSERT INTO dummy VALUES (1)",
            url="http://127.0.0.1:8123",
            user="admin",
            bypass_client_guard=True,
        )
        self.assertEqual(result, "OK\n")
        req = mock_urlopen.call_args[0][0]
        self.assertIn("readonly=1", req.full_url)

    @mock.patch("urllib.request.urlopen")
    def test_handles_http_error(self, mock_urlopen: mock.MagicMock) -> None:
        mock_error = urllib.error.HTTPError(
            url="http://127.0.0.1:8123",
            code=403,
            msg="Forbidden",
            hdrs=mock.MagicMock(),
            fp=io.BytesIO(b"DB::Exception: Cannot execute query in read-only mode"),
        )
        mock_urlopen.side_effect = mock_error

        with self.assertRaises(RuntimeError) as ctx:
            execute_clickhouse_query("SELECT 1")
        self.assertIn("ClickHouse HTTP 403", str(ctx.exception))
        self.assertIn("Cannot execute query in read-only mode", str(ctx.exception))


class FetchSecretPasswordTest(unittest.TestCase):
    @mock.patch("subprocess.run")
    def test_fetch_secret_password_success(self, mock_run: mock.MagicMock) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.stdout = "c2VjcmV0LXBhc3N3b3Jk\n"  # base64 for "secret-password"
        mock_run.return_value = mock_proc

        pwd = fetch_secret_password(namespace="signoz")
        self.assertEqual(pwd, "secret-password")

    @mock.patch("subprocess.run", side_effect=OSError("kubectl not found"))
    def test_fetch_secret_password_failure(self, _mock_run: mock.MagicMock) -> None:
        pwd = fetch_secret_password(namespace="signoz")
        self.assertEqual(pwd, "")


class MainCliTest(unittest.TestCase):
    @mock.patch("infra.tools.clickhouse_query.query.execute_clickhouse_query")
    @mock.patch("infra.tools.clickhouse_query.query.is_port_open", return_value=True)
    def test_main_with_query_argument(
        self,
        mock_is_port_open: mock.MagicMock,
        mock_exec: mock.MagicMock,
    ) -> None:
        mock_exec.return_value = "count()\n100\n"
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            code = main(["SELECT count() FROM signoz_logs.distributed_logs_v2"])
        self.assertEqual(code, 0)
        self.assertIn("count()", stdout.getvalue())
        mock_exec.assert_called_once()

    def test_main_rejects_empty_query(self) -> None:
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr), mock.patch("sys.stdin.isatty", return_value=True):
            with self.assertRaises(SystemExit):
                main([])
        self.assertIn("No query provided", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

"""Call the Coder API without leaking authenticated sessions through redirects."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import IO, TYPE_CHECKING, override

if TYPE_CHECKING:
    from collections.abc import Sequence
    from http.client import HTTPMessage


class BootstrapError(Exception):
    pass


class RejectAPIRedirects(urllib.request.HTTPRedirectHandler):
    """Prevent an API redirect from forwarding the Coder session header."""

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


class CoderAPI:
    """Call one Coder origin without forwarding sessions through redirects."""

    def __init__(
        self,
        coder_url: str,
        session_token: str,
        *,
        handlers: Sequence[urllib.request.BaseHandler] = (),
    ) -> None:
        self.coder_url = coder_url
        self.session_token = session_token
        self.opener = urllib.request.build_opener(*handlers, RejectAPIRedirects())

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        accepted: set[int],
    ) -> tuple[int, object | None]:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        request = urllib.request.Request(
            urllib.parse.urljoin(f"{self.coder_url.rstrip('/')}/", path.lstrip("/")),
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Coder-Session-Token": self.session_token,
            },
        )
        try:
            response = self.opener.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            if error.code in accepted:
                with error:
                    error.read(1024 * 1024)
                code = error.code
                assert isinstance(code, int)
                return code, None
            raise BootstrapError(f"Coder API {path} returned HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise BootstrapError(f"Coder API {path} was unreachable") from error
        with response:
            status = response.status
            response_body = response.read(1024 * 1024)
        assert isinstance(status, int)
        if status not in accepted:
            raise BootstrapError(f"Coder API {path} returned HTTP {status}")
        if not response_body:
            return status, None
        try:
            parsed: object = json.loads(response_body)
        except json.JSONDecodeError as error:
            raise BootstrapError(f"Coder API {path} returned invalid JSON") from error
        else:
            return status, parsed

"""Exercises Dex, Headlamp, gateway, and Coder snapshot browser flows to verify login, callback binding, authorization boundaries, and session reuse."""

import base64
import contextlib
import hashlib
import json
import os
import pathlib
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from typing import Any, cast, override


class LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action = ""
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form" and not self.action:
            self.action = attributes.get("action") or ""
        if tag in {"button", "input"} and attributes.get("name"):
            name = str(attributes["name"])
            value = attributes.get("value") or ""
            if name not in self.fields or value == "approve":
                self.fields[name] = value


class FollowDexRedirects(urllib.request.HTTPRedirectHandler):
    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not newurl.startswith(issuer):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class FollowAllowedOrigins(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_origins: set[str], history: list[str]) -> None:
        self.allowed_origins = allowed_origins
        self.history = history

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        self.history.append(newurl)
        if url_origin(newurl) not in self.allowed_origins:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def make_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    for ca_path in (
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/ssl/cert.pem",
        "/etc/ssl/local-ca/ca.crt",
        "/etc/local-ca/ca.crt",
    ):
        ca = pathlib.Path(ca_path)
        if ca.is_file() and ca.stat().st_size > 0:
            with contextlib.suppress(ssl.SSLError):
                context.load_verify_locations(cafile=str(ca))
    return context


def make_opener(*handlers: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=make_ssl_context()),
        *handlers,
    )
    opener.addheaders = [("User-Agent", "openplex-conformance-probe/1")]
    return opener


dex_http = make_opener()
issuer = os.environ["DEX_ISSUER"]


def is_password_db_enabled(discovery_doc: dict[str, object]) -> bool:
    env_setting = os.environ.get("ENABLE_PASSWORD_DB")
    if env_setting is not None:
        return env_setting.lower() in {"true", "1", "yes"}
    grant_types = discovery_doc.get("grant_types_supported")
    if isinstance(grant_types, list):
        return "password" in grant_types
    return "local" in issuer or "internal" in issuer


def url_origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def get_json(url: str) -> dict[str, object]:
    with dex_http.open(url, timeout=10) as response:
        payload = json.load(response)
        assert isinstance(payload, dict)
        return cast("dict[str, object]", payload)


def resource_owner_flow(
    endpoint_url: str, client_id: str, client_credential: str, user_credential: str
) -> dict[str, object]:
    basic_auth = base64.b64encode(f"{client_id}:{client_credential}".encode()).decode()
    request = urllib.request.Request(
        endpoint_url,
        data=urllib.parse.urlencode({
            "grant_type": "password",
            "password": user_credential,
            "scope": "openid profile email groups",
            "username": os.environ["DEX_USERNAME"],
        }).encode(),
        headers={
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with dex_http.open(request, timeout=10) as response:
        payload = json.load(response)
        assert isinstance(payload, dict)
        return cast("dict[str, object]", payload)


def authorization_parameters(client_id: str, redirect_uri: str) -> tuple[dict[str, str], str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return (
        {
            "client_id": client_id,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "nonce": secrets.token_urlsafe(24),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid profile email groups",
            "state": secrets.token_urlsafe(24),
        },
        verifier,
    )


def assert_authorization_request_accepts(
    authorization_url: str, client_id: str, redirect_uri: str
) -> None:
    parameters, _ = authorization_parameters(client_id, redirect_uri)
    opener = make_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()),
        FollowDexRedirects(),
    )
    with opener.open(
        f"{authorization_url}?{urllib.parse.urlencode(parameters)}", timeout=10
    ) as response:
        parser = LoginFormParser()
        parser.feed(response.read().decode())
        login_url = response.url
    assert login_url.startswith(f"{issuer}/auth/local")
    assert parser.action


def browser_flow(
    opener: urllib.request.OpenerDirector,
    authorization_url: str,
    endpoint_url: str,
    client_id: str,
    client_credential: str,
    redirect_uri: str,
    present_credentials: bool,
) -> dict[str, object]:
    parameters, verifier = authorization_parameters(client_id, redirect_uri)
    query = urllib.parse.urlencode(parameters)
    try:
        with opener.open(f"{authorization_url}?{query}", timeout=10) as response:
            if not present_credentials:
                raise AssertionError("Dex did not reuse the established browser session")
            parser = LoginFormParser()
            parser.feed(response.read().decode())
            login_url = urllib.parse.urljoin(response.url, parser.action)
        assert parser.action
        parser.fields.update({
            "login": os.environ["DEX_USERNAME"],
            "password": os.environ["DEX_PASSWORD"],
        })
        request = urllib.request.Request(
            login_url,
            data=urllib.parse.urlencode(parser.fields).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with opener.open(request, timeout=10) as response:
                location = response.url
        except urllib.error.HTTPError as error:
            assert 300 <= error.code < 400
            location = error.headers["Location"]
    except urllib.error.HTTPError as error:
        assert not present_credentials
        assert 300 <= error.code < 400
        location = error.headers["Location"]
    callback = urllib.parse.urlsplit(location)
    callback_query = urllib.parse.parse_qs(callback.query)
    code = callback_query.get("code", [""])[0]
    callback_state = callback_query.get("state", [""])[0]
    expected_callback = urllib.parse.urlsplit(redirect_uri)
    assert (callback.scheme, callback.netloc, callback.path) == (
        expected_callback.scheme,
        expected_callback.netloc,
        expected_callback.path,
    )
    assert not callback.fragment
    assert code
    assert callback_state == parameters["state"]
    basic_auth = base64.b64encode(f"{client_id}:{client_credential}".encode()).decode()
    request = urllib.request.Request(
        endpoint_url,
        data=urllib.parse.urlencode({
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }).encode(),
        headers={
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with dex_http.open(request, timeout=10) as response:
        credential_response = json.load(response)
    assert isinstance(credential_response, dict)
    credential_response["_expected_nonce"] = parameters["nonce"]
    return cast("dict[str, object]", credential_response)


def login_form(
    opener: urllib.request.OpenerDirector, protected_url: str
) -> tuple[LoginFormParser, str]:
    with opener.open(protected_url, timeout=10) as response:
        parser = LoginFormParser()
        parser.feed(response.read().decode())
        login_url = response.url
    assert login_url.startswith(f"{issuer}/auth/local")
    assert parser.action
    assert isinstance(login_url, str)
    return parser, login_url


def login_request(parser: LoginFormParser, login_url: str) -> urllib.request.Request:
    fields = dict(parser.fields)
    fields.update({
        "login": os.environ["DEX_USERNAME"],
        "password": os.environ["DEX_PASSWORD"],
    })
    return urllib.request.Request(
        urllib.parse.urljoin(login_url, parser.action),
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def complete_browser_login(
    opener: urllib.request.OpenerDirector,
    parser: LoginFormParser,
    login_url: str,
) -> tuple[int, str]:
    with opener.open(login_request(parser, login_url), timeout=10) as response:
        approval_parser = LoginFormParser()
        approval_parser.feed(response.read().decode())
        approval_url = response.url
        if not approval_url.startswith(f"{issuer}/approval?"):
            assert isinstance(response.status, int)
            assert isinstance(response.url, str)
            return response.status, response.url
    request = urllib.request.Request(
        urllib.parse.urljoin(approval_url, approval_parser.action or approval_url),
        data=urllib.parse.urlencode(approval_parser.fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with opener.open(request, timeout=10) as response:
        response.read()
        assert isinstance(response.status, int)
        assert isinstance(response.url, str)
        return response.status, response.url


def assert_gateway_browser_flow(protected_url: str) -> None:
    cookie_jar = CookieJar()
    history: list[str] = []
    allowed_origins = {url_origin(issuer), url_origin(protected_url)}
    opener = make_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar),
        FollowAllowedOrigins(allowed_origins, history),
    )
    assert not list(cookie_jar)
    parser, login_url = login_form(opener, protected_url)
    csrf_cookies = [cookie for cookie in cookie_jar if cookie.name.startswith("OauthNonce")]
    assert csrf_cookies
    for cookie in csrf_cookies:
        assert cookie.expires is not None
        assert cookie.expires > time.time()
        assert cookie.secure
    status, final_url = complete_browser_login(opener, parser, login_url)
    assert status == 200
    expected = urllib.parse.urlsplit(protected_url)
    final = urllib.parse.urlsplit(final_url)
    assert (final.scheme, final.netloc) == (expected.scheme, expected.netloc)
    assert not final.path.startswith("/oauth2/")
    callbacks = [url for url in history if urllib.parse.urlsplit(url).path == "/oauth2/callback"]
    assert len(callbacks) == 1


def assert_gateway_rejects_callback_without_csrf_cookie(protected_url: str) -> None:
    cookie_jar = CookieJar()
    history: list[str] = []
    opener = make_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar),
        FollowAllowedOrigins({url_origin(issuer)}, history),
    )
    parser, login_url = login_form(opener, protected_url)
    try:
        complete_browser_login(opener, parser, login_url)
    except urllib.error.HTTPError as error:
        assert 300 <= error.code < 400
        callback_url = error.headers["Location"]
    else:
        raise AssertionError("Dex did not return to the protected application")
    assert url_origin(callback_url) == url_origin(protected_url)
    assert urllib.parse.urlsplit(callback_url).path == "/oauth2/callback"
    empty_cookie_opener = make_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    try:
        empty_cookie_opener.open(callback_url, timeout=10)
    except urllib.error.HTTPError as error:
        assert error.code == 401
    else:
        raise AssertionError("Gateway accepted a callback without its CSRF cookie")


def snapshot_authorization(cookie_jar: CookieJar) -> str:
    opener = make_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar), FollowAllowedOrigins(set(), [])
    )
    try:
        opener.open(f"{os.environ['SNAPSHOT_PORTAL_URL']}/oauth/login", timeout=10)
    except urllib.error.HTTPError as error:
        assert error.code == 302
        authorization_url = error.headers["Location"]
    else:
        raise AssertionError("Snapshot portal did not initiate Coder OAuth")
    parsed = urllib.parse.urlsplit(authorization_url)
    query = urllib.parse.parse_qs(parsed.query)
    assert url_origin(authorization_url) == os.environ["CODER_URL"]
    assert parsed.path == "/oauth2/authorize"
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [f"{os.environ['SNAPSHOT_PORTAL_URL']}/oauth/callback"]
    assert query["client_id"] and query["state"] and query["code_challenge"]
    assert isinstance(authorization_url, str)
    return authorization_url


def snapshot_consent(cookie_jar: CookieJar, authorization_url: str) -> str:
    coder_url = os.environ["CODER_URL"]
    opener = make_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar),
        FollowAllowedOrigins({url_origin(issuer), url_origin(coder_url)}, []),
    )
    with opener.open(authorization_url, timeout=10) as response:
        assert response.status == 200
        assert urllib.parse.urlsplit(response.url).path == "/oauth2/authorize"
        parser = LoginFormParser()
        parser.feed(response.read().decode())
    request = urllib.request.Request(
        urllib.parse.urljoin(authorization_url, parser.action or authorization_url),
        data=urllib.parse.urlencode(parser.fields).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": coder_url,
            "Referer": authorization_url,
        },
    )
    try:
        opener.open(request, timeout=10)
    except urllib.error.HTTPError as error:
        assert error.code == 302
        callback_url = error.headers["Location"]
    else:
        raise AssertionError("Coder consent did not redirect to the snapshot portal")
    callback = urllib.parse.urlsplit(callback_url)
    assert url_origin(callback_url) == os.environ["SNAPSHOT_PORTAL_URL"]
    assert callback.path == "/oauth/callback"
    assert urllib.parse.parse_qs(callback.query).get("code")
    assert isinstance(callback_url, str)
    return callback_url


def assert_snapshot_browser_flow() -> None:
    coder_url = os.environ["CODER_URL"]
    portal_url = os.environ["SNAPSHOT_PORTAL_URL"]
    cookie_jar = CookieJar()
    opener = make_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar),
        FollowAllowedOrigins(
            {url_origin(issuer), url_origin(coder_url), url_origin(portal_url)}, []
        ),
    )
    authorization_url = snapshot_authorization(cookie_jar)
    # A missing Coder client produces a Basic-auth 401 before any browser login.
    with opener.open(authorization_url, timeout=10) as response:
        assert response.status == 200
        assert "WWW-Authenticate" not in response.headers
        assert url_origin(response.url) == coder_url
        assert urllib.parse.urlsplit(response.url).path == "/login"
    parser, login_url = login_form(opener, f"{coder_url}/api/v2/users/oidc/callback")
    status, _ = complete_browser_login(opener, parser, login_url)
    assert status == 200
    assert any(cookie.name == "coder_session_token" for cookie in cookie_jar)
    callback_url = snapshot_consent(cookie_jar, authorization_url)
    with opener.open(callback_url, timeout=15) as response:
        assert response.status == 200
        assert response.url == f"{portal_url}/"
        assert "<h1>Workspace Snapshots</h1>" in response.read().decode()
    assert any(cookie.name == "coder_snapshot_session" for cookie in cookie_jar)

    for failure in (
        "missing_state_cookie",
        "missing_state_parameter",
        "mismatched_state",
        "missing_verifier",
        "wrong_verifier",
    ):
        for cookie in list(cookie_jar):
            if cookie.name == "coder_snapshot_session":
                cookie_jar.clear(cookie.domain, cookie.path, cookie.name)
        authorization_url = snapshot_authorization(cookie_jar)
        callback_url = snapshot_consent(cookie_jar, authorization_url)
        callback = urllib.parse.urlsplit(callback_url)
        query = urllib.parse.parse_qs(callback.query)
        if failure == "missing_state_parameter":
            query.pop("state")
        elif failure == "mismatched_state":
            query["state"] = [secrets.token_urlsafe(24)]
        for cookie in list(cookie_jar):
            if (failure == "missing_state_cookie" and cookie.name == "coder_oauth_state") or (
                failure == "missing_verifier" and cookie.name == "coder_oauth_verifier"
            ):
                cookie_jar.clear(cookie.domain, cookie.path, cookie.name)
            elif failure == "wrong_verifier" and cookie.name == "coder_oauth_verifier":
                cookie.value = secrets.token_urlsafe(32)
        callback_url = urllib.parse.urlunsplit(
            callback._replace(query=urllib.parse.urlencode(query, doseq=True))
        )
        try:
            opener.open(callback_url, timeout=15)
        except urllib.error.HTTPError as error:
            assert error.code == (500 if failure == "wrong_verifier" else 400), failure
        else:
            raise AssertionError(f"Snapshot portal accepted {failure}")
        assert not any(cookie.name == "coder_snapshot_session" for cookie in cookie_jar)
    print("Snapshot portal browser login, state binding, and PKCE rejection verified")


def jwt_part(encoded_jwt: str, index: int) -> dict[str, object]:
    encoded = encoded_jwt.split(".")[index]
    encoded += "=" * (-len(encoded) % 4)
    payload = json.loads(base64.urlsafe_b64decode(encoded))
    assert isinstance(payload, dict)
    return cast("dict[str, object]", payload)


def headlamp_browser(cluster: str) -> urllib.request.OpenerDirector:
    headlamp_url = os.environ["HEADLAMP_URL"]
    opener = make_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()),
        FollowAllowedOrigins({url_origin(issuer), url_origin(headlamp_url)}, []),
    )
    parser, login_url = login_form(
        opener, f"{headlamp_url}/oidc?{urllib.parse.urlencode({'cluster': cluster})}"
    )
    with opener.open(login_request(parser, login_url), timeout=10) as response:
        approval_parser = LoginFormParser()
        approval_parser.feed(response.read().decode())
        callback_url = response.url
    if callback_url.startswith(f"{issuer}/approval?"):
        request = urllib.request.Request(
            urllib.parse.urljoin(callback_url, approval_parser.action or callback_url),
            data=urllib.parse.urlencode(approval_parser.fields).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with opener.open(request, timeout=10) as response:
            response.read()
    return opener


discovery = get_json(f"{issuer}/.well-known/openid-configuration")
authorization_url = str(discovery["authorization_endpoint"])
endpoint_url = str(discovery["token_endpoint"])

if is_password_db_enabled(discovery):
    browser = make_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()),
        FollowDexRedirects(),
    )
    verified_clients = []
    client_tokens: dict[str, str] = {}
    for consumer, present_credentials in (("HEADLAMP", True), ("SIGNOZ", False)):
        client_id = os.environ[f"DEX_{consumer}_CLIENT_ID"]
        credential_response = browser_flow(
            browser,
            authorization_url,
            endpoint_url,
            client_id,
            os.environ[f"DEX_{consumer}_CLIENT_SECRET"],
            os.environ[f"DEX_{consumer}_REDIRECT_URI"],
            present_credentials,
        )
        assert "access_token" in credential_response
        assert "id_token" in credential_response
        claims = jwt_part(str(credential_response["id_token"]), 1)
        audience = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        assert claims["iss"] == issuer
        assert claims["nonce"] == credential_response["_expected_nonce"]
        assert client_id in audience
        assert claims["email"] == os.environ["DEX_USERNAME"]
        assert claims["preferred_username"] == os.environ["DEX_EXPECTED_USERNAME"]
        groups = claims["groups"]
        assert isinstance(groups, list)
        assert set(os.environ["DEX_EXPECTED_GROUPS"].split(",")) <= set(groups)
        verified_clients.append(client_id)
        client_tokens[consumer] = str(credential_response["id_token"])

    for cluster in os.environ["HEADLAMP_CLUSTERS"].split(","):
        headlamp_browser_session = headlamp_browser(cluster)
        cluster_url = f"{os.environ['HEADLAMP_URL']}/clusters/{cluster}"
        with headlamp_browser_session.open(
            f"{cluster_url}/api/v1/namespaces?limit=1", timeout=15
        ) as response:
            assert response.status == 200
            assert json.load(response)["kind"] == "NamespaceList"
        try:
            headlamp_browser_session.open(
                f"{cluster_url}/api/v1/namespaces/kube-system/secrets?limit=1", timeout=15
            )
        except urllib.error.HTTPError as error:
            assert error.code == 403
        else:
            raise AssertionError(f"Headlamp allowed cluster secret access on {cluster}")
        wrong_audience_request = urllib.request.Request(
            f"{cluster_url}/api/v1/namespaces?limit=1",
            headers={"Authorization": f"Bearer {client_tokens['SIGNOZ']}"},
        )
        try:
            dex_http.open(wrong_audience_request, timeout=15)
        except urllib.error.HTTPError as error:
            assert error.code == 401
        else:
            raise AssertionError(f"Headlamp accepted another client's OIDC token on {cluster}")
        print(f"Headlamp OIDC browser access and denials verified for {cluster}")

    try:
        resource_owner_flow(
            endpoint_url,
            os.environ["DEX_HEADLAMP_CLIENT_ID"],
            os.environ["DEX_HEADLAMP_CLIENT_SECRET"],
            "incorrect-password",
        )
    except urllib.error.HTTPError as error:
        assert 400 <= error.code < 500
    else:
        raise AssertionError("Dex accepted an invalid local password")

    dragonfly_redirect_uris = [
        os.environ["DEX_DRAGONFLY_REDIRECT_URI"],
    ]
    for redirect_uri in dragonfly_redirect_uris:
        assert_authorization_request_accepts(authorization_url, "dragonfly", redirect_uri)
    try:
        assert_authorization_request_accepts(
            authorization_url,
            "dragonfly",
            "https://unregistered.example.invalid/oauth2/callback",
        )
    except urllib.error.HTTPError as error:
        assert error.code == 400
    else:
        raise AssertionError("Dex accepted an unregistered Dragonfly redirect URI")

    assert_gateway_browser_flow(os.environ["DEX_DRAGONFLY_URL"])
    assert_gateway_rejects_callback_without_csrf_cookie(os.environ["DEX_DRAGONFLY_URL"])
    print("Dragonfly browser login and callback state protection verified")
    assert_snapshot_browser_flow()
else:
    assert discovery.get("issuer") == issuer
    assert discovery.get("authorization_endpoint") == authorization_url
    assert discovery.get("token_endpoint") == endpoint_url
    jwks_uri = str(discovery.get("jwks_uri", f"{issuer}/keys"))
    jwks = get_json(jwks_uri)
    assert "keys" in jwks and isinstance(jwks["keys"], list)
    print("Dex OIDC discovery and JWKS public key endpoints verified")

    invalid_auth = base64.b64encode(b"invalid-conformance-client:invalid-secret").decode()
    unauth_request = urllib.request.Request(
        endpoint_url,
        data=urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": "dummy",
            "redirect_uri": "http://localhost/callback",
        }).encode(),
        headers={
            "Authorization": f"Basic {invalid_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with dex_http.open(unauth_request, timeout=10):
            pass
    except urllib.error.HTTPError as error:
        assert error.code == 401
    else:
        raise AssertionError("Dex accepted invalid client credentials on token endpoint")

    no_auth_request = urllib.request.Request(
        endpoint_url,
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with dex_http.open(no_auth_request, timeout=10):
            pass
    except urllib.error.HTTPError as error:
        assert error.code in {400, 401}
    else:
        raise AssertionError("Dex accepted unauthenticated request on token endpoint")
    print("Dex token endpoint authentication enforcement verified")

    invalid_client_query = urllib.parse.urlencode({
        "client_id": "nonexistent-conformance-client",
        "redirect_uri": "https://example.invalid/callback",
        "response_type": "code",
    })
    try:
        with dex_http.open(f"{authorization_url}?{invalid_client_query}", timeout=10):
            pass
    except urllib.error.HTTPError as error:
        assert error.code == 400
    else:
        raise AssertionError("Dex accepted unregistered client on authorization endpoint")

    invalid_redirect_query = urllib.parse.urlencode({
        "client_id": os.environ.get("DEX_HEADLAMP_CLIENT_ID", "headlamp"),
        "redirect_uri": "https://unregistered.example.invalid/callback",
        "response_type": "code",
    })
    try:
        with dex_http.open(f"{authorization_url}?{invalid_redirect_query}", timeout=10):
            pass
    except urllib.error.HTTPError as error:
        assert error.code == 400
    else:
        raise AssertionError("Dex accepted unregistered redirect URI on authorization endpoint")

    print(
        "Dex authenticated endpoints and client validation verified (password login skipped; enablePasswordDB=false)"
    )

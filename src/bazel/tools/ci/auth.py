"""Configure GitHub credentials in ~/.netrc and the BuildBuddy API key in ~/.bazelrc."""

import base64
import json
import os
import pathlib
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_GITHUB_APP_ID = "5140699"
DEFAULT_GITHUB_APP_INSTALLATION_ID = "166648602"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def main() -> None:
    app_id = os.environ.get("GITHUB_APP_ID", DEFAULT_GITHUB_APP_ID)
    inst_id = os.environ.get("GITHUB_APP_INSTALLATION_ID", DEFAULT_GITHUB_APP_INSTALLATION_ID)
    pem = os.environ.get("GITHUB_APP_PRIVATE_KEY")
    token = os.environ.get("GITHUB_TOKEN")

    if app_id and inst_id and pem:
        now = int(time.time())
        # Normalize literal escaped newlines if passed as single-line string
        pem_clean = pem.replace("\\n", "\n").strip() + "\n"
        hdr = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        pay = _b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
        unsigned = f"{hdr}.{pay}"
        # Write private key to a temporary file so openssl dgst signs the unsigned JWT payload from stdin
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as key_f:
            key_f.write(pem_clean)
            key_file = key_f.name
        try:
            proc = subprocess.run(
                ["openssl", "dgst", "-binary", "-sha256", "-sign", key_file],
                input=unsigned.encode("utf-8"),
                stdout=subprocess.PIPE,
                check=True,
            )
        finally:
            pathlib.Path(key_file).unlink(missing_ok=True)
        jwt = f"{unsigned}.{_b64(proc.stdout)}"
        req = urllib.request.Request(
            f"https://api.github.com/app/installations/{inst_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "BuildBuddy-CI-Auth",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                token = json.loads(resp.read())["token"]
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"GitHub token request failed ({exc.code} {exc.reason}): {body}")
            raise

    if token:
        netrc = pathlib.Path("~/.netrc").expanduser()
        netrc.write_text(
            f"machine github.com login x-access-token password {token}\n"
            f"machine api.github.com login x-access-token password {token}\n",
            encoding="utf-8",
        )
        netrc.chmod(0o600)

    bb_api_key = os.environ.get("BUILDBUDDY_API_KEY")
    home_rc = pathlib.Path("~/.bazelrc").expanduser()
    home_rc_lines = []
    if bb_api_key:
        home_rc_lines.append(f"common --remote_header=x-buildbuddy-api-key={bb_api_key}")
    home_rc_lines.extend(["common --remote_executor=", "build --remote_executor="])
    home_rc.write_text("\n".join(home_rc_lines) + "\n", encoding="utf-8")
    home_rc.chmod(0o600)

    if bb_api_key:
        subprocess.run(["git", "config", "--global", "buildbuddy.api-key", bb_api_key], check=False)


if __name__ == "__main__":
    main()

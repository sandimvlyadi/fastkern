"""Fail if the production nginx config does not pass `nginx -t`.

Runs the nginx image that docker-compose.prod.yml uses, with the same template mount, so the
template is rendered exactly as it is at start. The certificate is a throwaway self-signed one,
since `nginx -t` loads the files `ssl_certificate` names. Needs docker and openssl on the PATH.

    uv run python scripts/check_nginx_config.py
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.prod.yml"
TEMPLATES = ROOT / "deploy" / "nginx" / "templates"
SERVER_NAME = "api.example.com"


def nginx_image() -> str:
    match = re.search(r"^\s*image:\s*(nginx:\S+)\s*$", COMPOSE_FILE.read_text(), re.MULTILINE)
    if match is None:
        raise SystemExit(f"No nginx image found in {COMPOSE_FILE.name}")
    return match.group(1)


def _executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SystemExit(f"{name} is not on the PATH")
    return path


def main() -> int:
    docker, openssl = _executable("docker"), _executable("openssl")
    with tempfile.TemporaryDirectory() as cert_dir:
        live = Path(cert_dir) / "live" / SERVER_NAME
        live.mkdir(parents=True)
        key, cert = live / "privkey.pem", live / "fullchain.pem"
        make_cert = [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1"]
        make_cert += ["-subj", f"/CN={SERVER_NAME}", "-keyout", str(key), "-out", str(cert)]
        subprocess.run(make_cert, check=True, capture_output=True)  # noqa: S603 - fixed arguments
        # A throwaway key: readable to everyone, so the container can read it whatever uid the
        # bind mount maps the host user to.
        key.chmod(0o644)
        check = [docker, "run", "--rm", "-e", f"SERVER_NAME={SERVER_NAME}"]
        check += [
            "-v",
            f"{TEMPLATES}:/etc/nginx/templates:ro",
            "-v",
            f"{cert_dir}:/etc/letsencrypt:ro",
        ]
        check += [nginx_image(), "nginx", "-t"]
        result = subprocess.run(check, capture_output=True, text=True, check=False)  # noqa: S603 - fixed arguments
    output = result.stdout + result.stderr
    if result.returncode != 0:
        print(output, file=sys.stderr)
        return 1
    print("OK: the production nginx config is valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Portable Agent Board network configuration and authenticated transport."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import ssl
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler, ProxyHandler


class NetworkError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}", value):
        raise NetworkError("invalid identifier")
    return value


def loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def validate_url(url, allow_private_http=False):
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        raise NetworkError("endpoint must be an HTTP(S) URL without credentials, query or fragment")
    if parts.scheme == "http" and not loopback(parts.hostname):
        try:
            private = ipaddress.ip_address(parts.hostname).is_private
        except ValueError:
            private = False
        if not (allow_private_http and private):
            raise NetworkError("remote endpoints require HTTPS; private IP HTTP needs explicit allow_private_http")


def read_token(path):
    value = Path(path).expanduser().read_text(encoding="utf-8").strip()
    if len(value) < 24 or any(c.isspace() for c in value):
        raise NetworkError("token file must contain a token of at least 24 characters")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def ssl_context():
    context = ssl.create_default_context()
    # python.org macOS installations may not have run Install Certificates.command.
    # Add the system CA bundle while retaining certificate and hostname verification.
    if Path("/etc/ssl/cert.pem").is_file():
        context.load_verify_locations(cafile="/etc/ssl/cert.pem")
    return context


def request_json(endpoint, path, body=None, method=None, max_bytes=4 * 1024 * 1024, board_errors=False):
    url = endpoint["url"].rstrip("/")
    validate_url(url, endpoint.get("allow_private_http", False))
    headers = {"Accept": "application/json"}
    if endpoint.get("token_file"):
        headers["Authorization"] = "Bearer " + read_token(endpoint["token_file"])
    data = encoded(body) if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url + path, data=data, headers=headers, method=method)
    # Do not route local services or device credentials through system proxies.
    opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=ssl_context()))
    try:
        with opener.open(req, timeout=endpoint.get("timeout", 15)) as response:
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise NetworkError("upstream response too large", 502)
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        # Do not relay upstream bodies: they may contain internal paths or credentials.
        status = exc.code
        message = "upstream HTTP %d" % status
        if board_errors:
            try:
                detail = json.loads(exc.read(8192)).get("error")
                if isinstance(detail, str):
                    message = detail[:2000]
            except (ValueError, AttributeError):
                pass
        exc.close()
        raise NetworkError(message, status) from None
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise NetworkError("upstream unavailable or invalid response (%s)" % type(exc).__name__, 502) from None


def load_config(path):
    path = Path(path).expanduser().resolve()
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or cfg.get("version") != 1:
        raise NetworkError("network config version must be 1")
    def resolve(value):
        p = Path(value).expanduser()
        return str((path.parent / p).resolve()) if not p.is_absolute() else str(p)
    # Workspace paths belong to target devices, so never resolve them on the Hub.
    if "database" in cfg:
        cfg["database"] = resolve(cfg["database"])
    if "runtime_dir" in cfg:
        cfg["runtime_dir"] = resolve(cfg["runtime_dir"])
    for section in ("worker", "coordinator"):
        for field in ("peer_ca_file", "peer_cert_file", "peer_key_file"):
            if cfg.get(section, {}).get(field):
                cfg[section][field] = resolve(cfg[section][field])
    for endpoint in [cfg.get("dagu"), cfg.get("hub")]:
        if endpoint:
            validate_url(endpoint["url"], endpoint.get("allow_private_http", False))
            if endpoint.get("token_file"):
                endpoint["token_file"] = resolve(endpoint["token_file"])
    for principal in cfg.get("principals", {}).values():
        principal["token_file"] = resolve(principal["token_file"])
    for source in cfg.get('credential_refs', {}).values():
        if source.get('kind') in ('file', 'dotenv'):
            source['path'] = resolve(source['path'])
    for resource in cfg.get('resources', {}).values():
        resource['root'] = resolve(resource['root'])
    for source in cfg.get("knowledge_sources", {}).values():
        source["root"] = resolve(source["root"])
        if source.get("url"):
            validate_url(source["url"], source.get("allow_private_http", False))
        if source.get("token_file"):
            source["token_file"] = resolve(source["token_file"])
    for field in ("cert_file", "key_file"):
        if cfg.get("listen", {}).get(field):
            cfg["listen"][field] = resolve(cfg["listen"][field])
    return cfg

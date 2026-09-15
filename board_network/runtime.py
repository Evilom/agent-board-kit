"""Portable Agent Board process packaging: one server role, one client role."""

from __future__ import annotations

import hashlib
import io
import os
import platform
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from .common import NetworkError, loopback, ssl_context


DAGU_VERSION = "2.16.6"


def binary_path(config):
    return Path(config["runtime_dir"]) / "bin" / ("dagu.exe" if platform.system() == "Windows" else "dagu")


def install_runtime(config):
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    arch = {"arm64": "arm64", "aarch64": "arm64", "AMD64": "amd64", "x86_64": "amd64"}.get(platform.machine())
    if not system or not arch:
        raise NetworkError("this platform has no supported Dagu package")
    binary = binary_path(config)
    if binary.exists():
        result = subprocess.run([str(binary), "version"], capture_output=True, text=True, check=True)
        if result.stdout.strip() == DAGU_VERSION:
            return {"binary": str(binary), "version": DAGU_VERSION, "already_installed": True}
        raise NetworkError("existing Dagu binary is a different version; refusing to overwrite it")
    name = "dagu_%s_%s_%s.tar.gz" % (DAGU_VERSION, system, arch)
    base = "https://github.com/dagucloud/dagu/releases/download/v" + DAGU_VERSION + "/"
    with urlopen(base + "checksums.txt", timeout=30, context=ssl_context()) as response:
        checksums = response.read(1024 * 1024).decode()
    expected = next((line.split()[0] for line in checksums.splitlines() if line.split()[-1] == name), None)
    if not expected:
        raise NetworkError("release does not include requested platform checksum")
    with urlopen(base + name, timeout=120, context=ssl_context()) as response:
        archive = response.read(150 * 1024 * 1024 + 1)
    if hashlib.sha256(archive).hexdigest() != expected:
        raise NetworkError("Dagu archive checksum mismatch")
    # Extract exactly one executable, never archive paths or links.
    executable_name = "dagu.exe" if system == "windows" else "dagu"
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as t:
        entry = next(x for x in t.getmembers() if x.isfile() and (x.name == executable_name or x.name.endswith("/" + executable_name)))
        data = t.extractfile(entry).read()
    binary.parent.mkdir(parents=True, exist_ok=True)
    with binary.open("xb") as stream:
        stream.write(data)
    binary.chmod(0o700)
    return {"binary": str(binary), "version": DAGU_VERSION, "archive_sha256": expected}


def peer_flags(settings, host):
    insecure = settings.get("peer_insecure", False)
    if insecure:
        if not loopback(host) and not settings.get("allow_private_peer", False):
            raise NetworkError("remote Dagu connections require mTLS; configure peer certificates")
        return ["--peer.insecure=true"]
    fields = {"peer_ca_file": "--peer.client-ca-file", "peer_cert_file": "--peer.cert-file", "peer_key_file": "--peer.key-file"}
    flags = ["--peer.insecure=false"]
    for field, flag in fields.items():
        path = settings.get(field)
        if not path or not Path(path).is_file():
            raise NetworkError("missing mTLS file: " + field)
        flags.append(flag + "=" + str(path))
    return flags


def commands(config, config_path):
    executable = str(binary_path(config))
    jobs = []
    execution_enabled = config.get("execution_enabled", True) and Path(executable).is_file()
    if config["role"] == "server":
        from .hub import Hub
        Hub(config)  # Fail before launching any processes if grants or storage are invalid.
        jobs.append(("hub", [sys.executable, "-m", "board_network.cli", "--config", str(Path(config_path).resolve()), "serve"]))
    elif config["role"] != "client":
        raise NetworkError("role must be server or client")
    jobs.append(("device", [sys.executable, "-m", "board_network.cli", "--config", str(Path(config_path).resolve()), "device"]))
    if not execution_enabled:
        return jobs
    if config["role"] == "server":
        api = urlsplit(config["dagu"]["url"])
        if api.scheme != "http" or not loopback(api.hostname):
            raise NetworkError("bundled Dagu API must use loopback HTTP")
        coord = config["coordinator"]
        peer = dict(coord)
        peer.setdefault("peer_insecure", loopback(coord["host"]))
        cmd = [executable, "start-all", "--host=" + api.hostname, "--port=" + str(api.port or 8188),
               "--coordinator.host=" + coord["host"], "--coordinator.advertise=" + coord["advertise"],
               "--coordinator.port=" + str(coord["port"])] + peer_flags(peer, coord["host"])
        jobs.append(("center", cmd))
    worker = config["worker"]
    address = worker["coordinator"]
    host = urlsplit("//" + address).hostname
    if not host:
        raise NetworkError("worker coordinator must be host:port")
    cmd = [executable, "worker", "--worker.id=" + worker["device_id"], "--worker.coordinators=" + address,
           "--worker.labels=board_device=%s,board_environment=%s" % (worker["device_id"], worker["environment_id"]),
           "--worker.health-port=0", "--worker.max-active-runs=1"] + peer_flags(worker, host)
    jobs.append(("worker", cmd))
    return jobs


def service(config, config_path):
    jobs = commands(config, config_path)
    children, logs = [], []
    runtime = Path(config["runtime_dir"])
    runtime.mkdir(parents=True, exist_ok=True)
    # One supervisor owns its children. There is no background shell or orphan daemon.
    def stop(signum, frame):
        raise KeyboardInterrupt
    old_handlers = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        for name, cmd in jobs:
            env = os.environ.copy()
            env["DAGU_HOME"] = str(runtime / ("dagu-" + name))
            env["DAGU_AUTH_MODE"] = "none"  # API bound to loopback above; Hub authenticates remote callers.
            package_root = str(Path(__file__).resolve().parent.parent)
            env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
            env["AGENT_BOARD_CONFIG"] = str(Path(config_path).resolve())
            log = (runtime / (name + ".log")).open("ab")
            logs.append(log)
            children.append((name, subprocess.Popen(cmd, env=env, stdout=log, stderr=log)))
        print("Agent Board %s started; logs: %s" % (config["role"], runtime), flush=True)
        while True:
            for name, child in children:
                if child.poll() is not None:
                    raise NetworkError("%s exited (%s); inspect %s.log" % (name, child.returncode, name))
            time.sleep(0.5)
    finally:
        for _, child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for _, child in reversed(children):
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        for log in logs:
            log.close()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)

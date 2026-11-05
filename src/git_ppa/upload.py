# SPDX-License-Identifier: GPL-3.0-or-later
"""upload: put a signed source package into a PPA over sftp, and look at what Launchpad made of it.

The sftp client is paramiko, not dput and not an ssh executable: a strict snap has neither a usable ssh nor
a gpg that dput can reach. The private key is read from a file or standard input and kept in memory.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path

import paramiko
from debian.deb822 import Changes

from . import lp

HOST = "ppa.launchpad.net"
# https://ubuntu.com/docs/launchpad/user/reference/ssh-fingerprints/ (RSA), the server offered the same on 2026-11-04
LAUNCHPAD_FINGERPRINTS = {HOST: {"SHA256:MGq+4hxD7RduVTcfwlwwboZnsgJC6SL/NltM8ye+gNg"}}


class UploadError(Exception):
    pass


@dataclass
class Uploaded:
    ppa: str
    host: str
    incoming: str
    files: list[str]
    version: str
    source: str
    dry_run: bool

    def to_dict(self) -> dict:
        return asdict(self)


def fingerprint(key: paramiko.PKey) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class PinnedHostKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, allowed: set[str]):
        self.allowed = allowed

    def missing_host_key(self, client, hostname, key) -> None:
        fp = fingerprint(key)
        if fp not in self.allowed:
            raise paramiko.SSHException(f"host key of {hostname} is {fp}, expected {' or '.join(sorted(self.allowed))}")


def load_private_key(data: bytes, passphrase: str | None = None) -> paramiko.PKey:
    text = data.decode("utf-8", errors="replace")
    errors = []
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase)
        except paramiko.PasswordRequiredException:
            raise UploadError("the ssh key has a passphrase, set GIT_PPA_SSH_PASSPHRASE") from None
        except paramiko.SSHException as e:
            errors.append(str(e))
    raise UploadError("cannot read the ssh key (OpenSSH or PEM format, no agent): " + errors[-1])


def read_changes(path: Path) -> tuple[str, str, list[str]]:
    """Source name, version and the files of an upload, the .changes last. Checks signature and checksums."""
    raw = path.read_text()
    if not raw.startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
        raise UploadError(f"{path.name} is not signed, Launchpad would reject it, run build with a signing key")
    ch = Changes(raw)
    files = []
    for entry in ch.get("Checksums-Sha256", []):
        f = path.parent / entry["name"]
        if not f.is_file():
            raise UploadError(f"{entry['name']} is listed in {path.name} but is not next to it")
        if hashlib.sha256(f.read_bytes()).hexdigest() != entry["sha256"]:
            raise UploadError(f"{entry['name']} does not match its checksum in {path.name}, build again")
        files.append(entry["name"])
    if not files:
        raise UploadError(f"{path.name} lists no files")
    dsc = next((n for n in files if n.endswith(".dsc")), None)
    if dsc:
        first = (path.parent / dsc).read_text()
        if not first.startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
            raise UploadError(f"{dsc} is not signed")
    return str(ch["Source"]), str(ch["Version"]), files + [path.name]


def incoming_dir(ppa: str) -> str:
    m = re.fullmatch(r"~([^/]+)/ubuntu/([^/]+)", ppa)
    if not m:
        raise UploadError(f"PPA reference {ppa!r} is not ~owner/ubuntu/name")
    return ppa


def upload(changes: Path, ppa: str, user: str, private_key: paramiko.PKey, host: str = HOST, port: int = 22,
           incoming: str | None = None, fingerprints: set[str] | None = None, dry_run: bool = False) -> Uploaded:
    source, version, names = read_changes(changes)
    target = incoming or incoming_dir(ppa)
    allowed = fingerprints or LAUNCHPAD_FINGERPRINTS.get(host)
    if not allowed:
        raise UploadError(f"no known host key for {host}, give --host-fingerprint")
    result = Uploaded(ppa, host, target, names, version, source, dry_run)
    if dry_run:
        return result
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(PinnedHostKey(allowed))
    try:
        client.connect(host, port=port, username=user, pkey=private_key, look_for_keys=False, allow_agent=False,
                       timeout=30, banner_timeout=30, auth_timeout=30)
    except paramiko.AuthenticationException:
        raise UploadError(f"{host} refused the key for {user}, is it registered on the Launchpad account?") from None
    except paramiko.SSHException as e:
        raise UploadError(f"cannot connect to {host}: {e}") from None
    except OSError as e:
        raise UploadError(f"cannot connect to {host}: {e}") from None
    n = ""
    try:
        sftp = client.open_sftp()
        for n in names:
            # the .changes is last, Launchpad starts on a upload when it arrives
            # confirm=False: the Launchpad server cannot stat an uploaded file, put would fail with "size mismatch ... None"
            sftp.put(str(changes.parent / n), f"{target}/{n}", confirm=False)
    except (OSError, paramiko.SSHException) as e:
        raise UploadError(f"upload failed{' at ' + n if n else ''}: {e}") from None
    finally:
        client.close()
    return result


def publication(ppa: str, source: str, version: str) -> dict | None:
    """Publication and builds of one source version in a public PPA, None while Launchpad has not made it."""
    m = re.fullmatch(r"~([^/]+)/ubuntu/([^/]+)", ppa)
    base = f"{lp.API}/~{m[1]}/+archive/ubuntu/{m[2]}"
    q = urllib.parse.urlencode({"ws.op": "getPublishedSources", "source_name": source, "version": version})
    # exact_match=true makes Launchpad return nothing here, so compare the name and version ourselves
    entries = [x for x in json.loads(lp._get(f"{base}?{q}")).get("entries", [])
               if x["source_package_name"] == source and x["source_package_version"] == version]
    if not entries:
        return None
    e = entries[0]
    builds = json.loads(lp._get(e["self_link"] + "?ws.op=getBuilds")).get("entries", [])
    return {
        "status": e["status"], "pocket": e["pocket"], "date": e.get("date_published"),
        "builds": [{"arch": b["arch_tag"], "state": b["buildstate"], "link": b.get("web_link")} for b in builds],
    }


def wait(ppa: str, source: str, version: str, seconds: int, every: int = 15, log=print) -> dict | None:
    """Wait until the source is published and every build is over. Returns the last state seen."""
    end = time.monotonic() + seconds
    last: dict | None = None
    seen = None
    while True:
        try:
            now = publication(ppa, source, version)
        except lp.LaunchpadError as e:
            # a name lookup or a 5xx that outlasted the retries, keep waiting with what was seen
            log(f"Launchpad did not answer ({e}), still waiting")
            now = None
        # Launchpad answers from database replicas that can lag, a publication seen once can be
        # missing from the next answer, it has not gone away
        if now is not None:
            last = now
        state = None if last is None else (last["status"], tuple((b["arch"], b["state"]) for b in last["builds"]))
        if state != seen:
            log("not published yet" if last is None else f"{last['status']}: " + (", ".join(f"{b['arch']} {b['state']}" for b in last["builds"]) or "no build yet"))
            seen = state
        if last and last["builds"] and all(b["state"] not in ("Needs building", "Currently building", "Uploading build", "Dependency wait") for b in last["builds"]):
            return last
        if time.monotonic() >= end:
            return last
        time.sleep(every)

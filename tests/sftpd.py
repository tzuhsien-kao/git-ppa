# SPDX-License-Identifier: GPL-3.0-or-later
"""A small sftp server in this process, with a host key and one allowed user key, for the upload tests."""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import paramiko


class _Handle(paramiko.SFTPHandle):
    def stat(self):
        return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))

    def chattr(self, attr):
        return paramiko.SFTP_OK


class _Stub(paramiko.SFTPServerInterface):
    root = Path("/nonexistent")
    created: list[str] = []

    def _real(self, path: str) -> Path:
        return self.root / self.canonicalize(path).lstrip("/")

    def open(self, path, flags, attr):
        real = self._real(path)
        try:
            fd = os.open(real, flags, 0o644)
            f = os.fdopen(fd, "wb" if flags & os.O_WRONLY else "r+b" if flags & os.O_RDWR else "rb")
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        if flags & os.O_CREAT:
            self.created.append(self.canonicalize(path))
        h = _Handle(flags)
        h.filename, h.readfile, h.writefile = str(real), f, f
        return h

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    lstat = stat


class _Auth(paramiko.ServerInterface):
    def __init__(self, user: str, key: paramiko.PKey):
        self.user, self.key = user, key

    def get_allowed_auths(self, username):
        return "publickey"

    def check_auth_publickey(self, username, key):
        ok = username == self.user and key.asbytes() == self.key.asbytes()
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED


class SftpServer:
    def __init__(self, root: Path, user: str, user_key: paramiko.PKey):
        self.host_key = paramiko.RSAKey.generate(2048)
        self.root, self.user, self.user_key = root, user, user_key
        self.created: list[str] = []
        stub = type("Stub", (_Stub,), {"root": root, "created": self.created})
        self._stub = stub
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._transports: list[paramiko.Transport] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(self.host_key)
            t.set_subsystem_handler("sftp", paramiko.SFTPServer, self._stub)
            self._transports.append(t)
            try:
                t.start_server(server=_Auth(self.user, self.user_key))
            except paramiko.SSHException:
                pass

    def close(self):
        self._sock.close()
        for t in self._transports:
            t.close()

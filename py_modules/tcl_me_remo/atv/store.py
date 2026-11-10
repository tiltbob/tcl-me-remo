"""What the Wi-Fi assist keeps on disk: the client identity and the pairing record.

    client.key    RSA private key (PEM), mode 0600
    client.pem    self-signed certificate (PEM)
    pairing.json  which TV was paired: address, name and certificate fingerprint

all inside one directory of mode 0700.  While a key is being made, a scratch directory
``.new-*`` exists next to them, and ``pairing.json.tmp`` while the record is written; what a
killed process left of those is removed when an identity is created or everything is
forgotten.  Methods block; call them from a worker thread.  A
lock makes creation and removal atomic with respect to each other, including when the task
that asked for one of them was cancelled while its thread was still running.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from . import certs
from .util import clean_name

log = logging.getLogger(__name__)

_RECORD_VERSION = 1
_MAX_RECORD_BYTES = 16 * 1024
_FINGERPRINT = re.compile(r"[0-9a-fA-F]{64}")


@dataclass(frozen=True)
class PairingRecord:
    host: str  # where the TV was when it was paired
    fingerprint: str  # SHA-256 of the TV's certificate, the pin for later connections
    name: str = ""


class Store:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.key_path = self.directory / "client.key"
        self.cert_path = self.directory / "client.pem"
        self.record_path = self.directory / "pairing.json"
        self._record_scratch = self.directory / "pairing.json.tmp"
        self._lock = threading.Lock()

    def has_identity(self) -> bool:
        return self.key_path.is_file() and self.cert_path.is_file()

    def ensure_identity(self, *, common_name: str, openssl: str) -> bool:
        """Create the key and certificate unless both exist.  Returns True if they were created.

        An existing identity is never replaced: the TV's pairing is bound to it.
        """
        with self._lock:
            if self.has_identity():
                self._remove_scratch()
                return False
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.directory, 0o700)
            # Half an identity (a crash between the two renames), a record made for an
            # identity that is gone and the scratch files of a creation that was killed
            # are all useless; start clean.
            self._remove_files()
            certs.create_identity(self.key_path, self.cert_path, common_name=common_name, openssl=openssl)
            return True

    def load_identity(self) -> certs.Identity:
        """Raises FileNotFoundError when there is none, :class:`certs.CertError` when it is damaged."""
        with self._lock:
            return certs.load_identity(self.key_path, self.cert_path)

    def read_record(self) -> PairingRecord | None:
        try:
            with open(self.record_path, "rb") as handle:
                data = json.loads(handle.read(_MAX_RECORD_BYTES))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            host, fingerprint, name = data["host"], data["fingerprint"], data.get("name", "")
            if not (isinstance(host, str) and isinstance(fingerprint, str) and isinstance(name, str)):
                raise ValueError("wrong types")
            if not host or not _FINGERPRINT.fullmatch(fingerprint):
                raise ValueError("wrong values")
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:  # RecursionError: nested too deeply
            log.warning("ignoring unreadable Wi-Fi pairing record %s (%s)", self.record_path, type(exc).__name__)
            return None
        return PairingRecord(host=host[:253], fingerprint=fingerprint.lower(), name=clean_name(name))

    def write_record(self, record: PairingRecord) -> None:
        payload = json.dumps({"version": _RECORD_VERSION, **asdict(record)}, indent=2) + "\n"
        temporary = self._record_scratch
        with self._lock:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.record_path)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise

    def remove(self) -> None:
        """Forget everything: key, certificate, pairing record and any unfinished leftovers."""
        with self._lock:
            self._remove_files()

    def _remove_files(self) -> None:
        for path in (self.record_path, self.cert_path, self.key_path):
            path.unlink(missing_ok=True)
        self._remove_scratch()

    def _remove_scratch(self) -> None:
        """Remove what a killed process left behind.  The lock is held, so nothing is in use.

        A scratch directory of a key creation that never finished holds a private key that
        was never used; it must not outlive "forget everything".
        """
        self._record_scratch.unlink(missing_ok=True)
        try:
            with os.scandir(self.directory) as entries:
                leftovers = [entry for entry in entries if entry.name.startswith(certs.SCRATCH_PREFIX)]
        except FileNotFoundError:
            return
        for entry in leftovers:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)

"""The client identity, and the few things needed from X.509 certificates.

The TV recognises a paired client by its certificate, so the RSA key and self-signed
certificate are created once (by the ``openssl`` tool; the standard library cannot) and kept.
Reading certificates needs no tool: the pairing secret is built from the RSA modulus and
exponent of both sides, which a short walk over the DER encoding yields.

Functions here block (files, a child process); call them from a worker thread.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..system import clean_env
from .util import clean_name

log = logging.getLogger(__name__)

KEY_BITS = 2048
VALID_DAYS = 3650
OPENSSL_TIMEOUT = 60.0
SCRATCH_PREFIX = ".new-"  # scratch directories of create_identity, next to the files they produce

# An explicit request file: `openssl req` otherwise needs the system-wide openssl.cnf.
_REQUEST_CONFIG = """\
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = {name}
[ext]
basicConstraints = CA:TRUE, pathlen:0
"""

_TAG_INTEGER, _TAG_BIT_STRING, _TAG_OID, _TAG_SEQUENCE, _TAG_VERSION = 0x02, 0x03, 0x06, 0x30, 0xA0
_TAG_BMP_STRING = 0x1E
_OID_RSA = bytes.fromhex("2a864886f70d010101")  # 1.2.840.113549.1.1.1 rsaEncryption
_OID_COMMON_NAME = bytes.fromhex("550403")
_OID_DN_QUALIFIER = bytes.fromhex("55042e")
_MAX_KEY_BITS = 16384
_MAC = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


class CertError(ValueError):
    """A certificate cannot be read or created."""


class OpensslMissing(CertError):
    """The openssl command-line tool is not installed."""


@dataclass(frozen=True)
class Identity:
    """What a connection needs: a TLS context presenting our certificate, and that certificate."""

    context: ssl.SSLContext
    certificate: bytes  # DER


def fingerprint(certificate: bytes) -> str:
    """SHA-256 of a DER certificate, lower-case hex."""
    return hashlib.sha256(certificate).hexdigest()


# ---------------------------------------------------------------------------- reading DER


def _element(data: bytes, pos: int, end: int) -> tuple[int, int, int]:
    """``(tag, content start, content end)`` of the element at ``pos``, which must end by ``end``."""
    if end - pos < 2:
        raise CertError("certificate is truncated")
    tag, length = data[pos], data[pos + 1]
    if tag & 0x1F == 0x1F:
        raise CertError("certificate uses an unsupported encoding")
    start = pos + 2
    if length & 0x80:
        count = length & 0x7F
        if not 1 <= count <= 4 or end - start < count:
            raise CertError("certificate has a malformed length")
        length = int.from_bytes(data[start : start + count], "big")
        start += count
    if length > end - start:
        raise CertError("certificate is truncated")
    return tag, start, start + length


def _children(data: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    out = []
    pos = start
    while pos < end:
        element = _element(data, pos, end)
        out.append(element)
        pos = element[2]
    return out


def _sequence(data: bytes, element: tuple[int, int, int], what: str) -> list[tuple[int, int, int]]:
    tag, start, end = element
    if tag != _TAG_SEQUENCE:
        raise CertError(f"certificate has no {what}")
    return _children(data, start, end)


def _tbs_fields(certificate: bytes) -> list[tuple[int, int, int]]:
    """The fields of tbsCertificate from serialNumber on (the optional version is dropped)."""
    outer = _sequence(certificate, _element(certificate, 0, len(certificate)), "outer sequence")
    if not outer:
        raise CertError("certificate is empty")
    fields = _sequence(certificate, outer[0], "body")
    if fields and fields[0][0] == _TAG_VERSION:
        fields = fields[1:]
    if len(fields) < 6:  # serial, signature algorithm, issuer, validity, subject, public key
        raise CertError("certificate is incomplete")
    return fields


def rsa_public_numbers(certificate: bytes) -> tuple[int, int]:
    """``(modulus, public exponent)`` of the RSA key in a DER X.509 certificate."""
    key_info = _sequence(certificate, _tbs_fields(certificate)[5], "public key")
    if len(key_info) < 2:
        raise CertError("certificate has no public key")
    algorithm = _sequence(certificate, key_info[0], "key algorithm")
    if not algorithm or algorithm[0][0] != _TAG_OID or certificate[algorithm[0][1] : algorithm[0][2]] != _OID_RSA:
        raise CertError("certificate does not hold an RSA key")
    tag, start, end = key_info[1]
    if tag != _TAG_BIT_STRING or end - start < 1 or certificate[start] != 0:
        raise CertError("certificate has a malformed public key")
    key = certificate[start + 1 : end]  # RSAPublicKey ::= SEQUENCE { modulus INTEGER, publicExponent INTEGER }
    numbers = _sequence(key, _element(key, 0, len(key)), "RSA key")
    if len(numbers) < 2 or numbers[0][0] != _TAG_INTEGER or numbers[1][0] != _TAG_INTEGER:
        raise CertError("certificate has a malformed RSA key")
    modulus = int.from_bytes(key[numbers[0][1] : numbers[0][2]], "big")
    exponent = int.from_bytes(key[numbers[1][1] : numbers[1][2]], "big")
    if modulus <= 0 or exponent <= 0 or modulus.bit_length() > _MAX_KEY_BITS:
        raise CertError("certificate has an unusable RSA key")
    return modulus, exponent


def subject_attributes(certificate: bytes) -> dict[str, str]:
    """The subject's ``CN`` and ``dnQualifier``, where present."""
    wanted = {_OID_COMMON_NAME: "CN", _OID_DN_QUALIFIER: "dnQualifier"}
    found: dict[str, str] = {}
    for name_set in _sequence(certificate, _tbs_fields(certificate)[4], "subject"):
        for attribute in _children(certificate, name_set[1], name_set[2]):
            parts = _children(certificate, attribute[1], attribute[2])
            if len(parts) < 2 or parts[0][0] != _TAG_OID:
                continue
            key = wanted.get(certificate[parts[0][1] : parts[0][2]])
            if key:
                raw = certificate[parts[1][1] : parts[1][2]]
                found[key] = raw.decode("utf-16-be" if parts[1][0] == _TAG_BMP_STRING else "utf-8", "replace")
    return found


def tv_name(certificate: bytes) -> str:
    """The TV's friendly name as its certificate states it, or "" (best effort).

    Two layouts are documented: ``CN=atvremote/<product>/<device>/<name>/<MAC>`` and
    ``dnQualifier=<product>/<device>/<name>, CN=atvremote/<MAC>``.  Some devices leave the
    MAC address out.
    """
    try:
        subject = subject_attributes(certificate)
    except CertError:
        return ""
    qualifier = subject.get("dnQualifier", "")
    if qualifier:
        return clean_name(qualifier.rsplit("/", 1)[-1])
    parts = subject.get("CN", "").split("/")
    if parts and _MAC.match(parts[-1]):
        parts.pop()
    return clean_name(parts[-1]) if len(parts) > 1 else ""


# ---------------------------------------------------------------------------- our identity


def load_identity(key_path: Path, cert_path: Path) -> Identity:
    """Build the TLS context for both ports.  Raises FileNotFoundError or :class:`CertError`."""
    try:
        certificate = ssl.PEM_cert_to_DER_cert(cert_path.read_text("ascii"))
        rsa_public_numbers(certificate)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # The TV's certificate is self-signed, so there is no chain to verify; the caller
        # pins it instead.  (check_hostname must be cleared before verify_mode.)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert_path, key_path)
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:  # ssl.SSLError is an OSError
        raise CertError(f"the saved key or certificate is damaged ({type(exc).__name__})") from None
    return Identity(context, certificate)


def create_identity(key_path: Path, cert_path: Path, *, common_name: str, openssl: str = "openssl") -> None:
    """Create a new RSA key and self-signed certificate at the given paths.

    Both are produced in a private scratch directory next to their destination and moved
    into place only once the pair is known to work, key first.  The key is never readable by
    anyone but its owner: the scratch directory is 0700 and the file is created 0600 before
    openssl writes to it.
    """
    name = re.sub(r"[^A-Za-z0-9 ._-]", "", common_name).strip()[:64] or "tcl-me-remo"
    work = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=key_path.parent))
    try:
        new_key, new_cert, config = work / "key.pem", work / "cert.pem", work / "request.cnf"
        config.write_text(_REQUEST_CONFIG.format(name=name), "ascii")
        os.close(os.open(new_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        _openssl([openssl, "genrsa", "-out", str(new_key), str(KEY_BITS)])
        _openssl(
            [openssl, "req", "-x509", "-new", "-key", str(new_key), "-sha256", "-days", str(VALID_DAYS)]
            + ["-config", str(config), "-out", str(new_cert)]
        )
        try:
            for path in (new_key, new_cert):
                os.chmod(path, 0o600)
                _sync(path)
            load_identity(new_key, new_cert)
        except FileNotFoundError:
            raise CertError("openssl did not produce a key and certificate") from None
        os.replace(new_key, key_path)
        os.replace(new_cert, cert_path)
        _sync(key_path.parent)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _openssl(argv: list[str]) -> None:
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=OPENSSL_TIMEOUT,
            env=clean_env(),
            umask=0o077,
            check=False,
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        raise OpensslMissing(f"the '{argv[0]}' program was not found") from None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CertError(f"could not run {argv[0]} ({type(exc).__name__})") from None
    if done.returncode != 0:
        log.debug("openssl %s said: %s", argv[1], done.stderr.decode("utf-8", "replace").strip()[-400:])
        raise CertError(f"openssl {argv[1]} failed with exit status {done.returncode}")


def _sync(path: Path) -> None:
    """Flush a file or directory to disk, so a pairing survives a power cut right after it."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)

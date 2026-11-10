"""The client identity, certificate parsing and the on-disk store of the Wi-Fi assist."""

import os
import random
import shutil
import ssl
import stat
import subprocess
import threading

import pytest
from fake_tv import CLIENT_CERT, CLIENT_KEY, RECORD, make_certificate, openssl_rsa_numbers

from tcl_me_remo.atv import AtvAssist, certs
from tcl_me_remo.atv.store import PairingRecord, Store

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl tool")


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def openssl(*args: str, data: bytes | None = None) -> str:
    return subprocess.run(["openssl", *args], input=data, check=True, capture_output=True).stdout.decode()


def der_of(cert_path) -> bytes:
    return ssl.PEM_cert_to_DER_cert(cert_path.read_text())


def make_special(tmp_path, stem: str, *req_args: str, subject: str = "/CN=special") -> bytes:
    """A certificate made with extra ``openssl req`` arguments; returns its DER."""
    cert, key = tmp_path / f"{stem}.pem", tmp_path / f"{stem}.key"
    openssl("req", "-x509", "-nodes", "-days", "2", "-subj", subject, *req_args, "-keyout", str(key), "-out", str(cert))
    return der_of(cert)


# ---------------------------------------------------------------------------- reading certificates


def test_rsa_numbers_match_openssl(tmp_path):
    cert, _ = make_certificate(tmp_path, "tv", "/CN=numbers")
    der = der_of(cert)
    modulus, exponent = certs.rsa_public_numbers(der)
    assert openssl("x509", "-in", str(cert), "-noout", "-modulus").strip() == f"Modulus={modulus:X}"
    assert exponent == 65537 and modulus.bit_length() == 2048
    assert (modulus, exponent) == openssl_rsa_numbers(der)


@pytest.mark.parametrize(
    ("bits", "exponent"),
    [(1024, 3), (1031, 65537), (3072, 17)],  # other lengths of the DER length fields and integers
)
def test_rsa_numbers_for_other_key_shapes(tmp_path, bits, exponent):
    der = make_special(tmp_path, "odd", "-newkey", f"rsa:{bits}", "-pkeyopt", f"rsa_keygen_pubexp:{exponent}")
    modulus, found = certs.rsa_public_numbers(der)
    assert found == exponent and modulus.bit_length() == bits
    assert (modulus, found) == openssl_rsa_numbers(der)


def test_non_rsa_certificate_is_refused(tmp_path):
    der = make_special(tmp_path, "ec", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1")
    with pytest.raises(certs.CertError, match="RSA"):
        certs.rsa_public_numbers(der)


def test_damaged_certificates_raise_only_cert_error(tmp_path):
    cert, _ = make_certificate(tmp_path, "tv", "/CN=numbers")
    der = der_of(cert)
    for cut in range(0, len(der), 3):
        with pytest.raises(certs.CertError):
            certs.rsa_public_numbers(der[:cut])
    rng = random.Random(1)
    for _ in range(400):
        damaged = bytearray(der)
        for _ in range(rng.randrange(1, 4)):
            damaged[rng.randrange(len(damaged))] = rng.randrange(256)
        for reader in (certs.rsa_public_numbers, certs.subject_attributes):
            try:
                reader(bytes(damaged))
            except certs.CertError:
                pass
        assert isinstance(certs.tv_name(bytes(damaged)), str)
    for junk in (b"", b"\x30", b"\x30\x80", b"\x30\x84\xff\xff\xff\xff", b"\x1f\x81\x01\x00", bytes(100)):
        with pytest.raises(certs.CertError):
            certs.rsa_public_numbers(junk)
        assert certs.tv_name(junk) == ""


def tlv(tag: int, *parts: bytes) -> bytes:
    body = b"".join(parts)
    length = bytes([len(body)]) if len(body) < 0x80 else bytes([0x82]) + len(body).to_bytes(2, "big")
    return bytes([tag]) + length + body


def synthetic_certificate(
    modulus: bytes, exponent: bytes = b"\x01\x00\x01", *, fields: int = 6, version: bool = True
) -> bytes:
    """Just enough DER to look like a certificate: the structure, with empty names and dates."""
    rsa = tlv(0x30, tlv(0x06, bytes.fromhex("2a864886f70d010101")), tlv(0x05))
    key = tlv(0x30, rsa, tlv(0x03, b"\x00", tlv(0x30, tlv(0x02, modulus), tlv(0x02, exponent))))
    body = [tlv(0x02, b"\x01"), tlv(0x30), tlv(0x30), tlv(0x30), tlv(0x30), key][6 - fields :]
    if version:
        body.insert(0, tlv(0xA0, tlv(0x02, b"\x02")))
    return tlv(0x30, tlv(0x30, *body), tlv(0x30), tlv(0x03, b"\x00"))


def test_hand_built_certificates():
    assert certs.rsa_public_numbers(synthetic_certificate(b"\x00\xc0\xff\xee\x01")) == (0xC0FFEE01, 65537)
    assert certs.rsa_public_numbers(synthetic_certificate(b"\x0a\xbc", b"\x03", version=False)) == (0x0ABC, 3)
    big = b"\x00" + b"\xff" * 300  # needs the two-byte length form
    assert certs.rsa_public_numbers(synthetic_certificate(big))[0] == int.from_bytes(big, "big")
    for bad in (
        synthetic_certificate(b"\x00"),  # modulus zero
        synthetic_certificate(b"\x01", b"\x00"),  # exponent zero
        synthetic_certificate(b"\x01", fields=5),  # a field short: the "key" is not where it should be
        tlv(0x30),  # an empty certificate
        tlv(0x30, tlv(0x30)),  # an empty body
        tlv(0x04, b"abc"),  # not a sequence at all
    ):
        with pytest.raises(certs.CertError):
            certs.rsa_public_numbers(bad)
    assert certs.tv_name(synthetic_certificate(b"\x01")) == ""


def test_fingerprint_is_sha256_of_the_der(tmp_path):
    cert, _ = make_certificate(tmp_path, "tv", "/CN=numbers")
    reported = openssl("x509", "-in", str(cert), "-noout", "-fingerprint", "-sha256")
    assert certs.fingerprint(der_of(cert)) == reported.split("=", 1)[1].strip().replace(":", "").lower()


@pytest.mark.parametrize(
    ("subject", "name"),
    [
        ("/CN=atvremote\\/darcy\\/darcy\\/SHIELD Android TV\\/AA:BB:CC:DD:EE:FF", "SHIELD Android TV"),
        ("/dnQualifier=fugu\\/fugu\\/Nexus Player/CN=atvremote\\/AA:BB:CC:DD:EE:FF", "Nexus Player"),
        ("/CN=atvremote\\/a\\/b\\/Wohnzimmer TV", "Wohnzimmer TV"),  # no MAC address
        ("/CN=atvremote\\/AA:BB:CC:DD:EE:FF", ""),
        ("/CN=plain", ""),
        ("/O=nothing useful", ""),
    ],
)
def test_tv_name_from_certificate_subject(tmp_path, subject, name):
    assert certs.tv_name(make_special(tmp_path, "named", "-newkey", "rsa:2048", subject=subject)) == name


# ---------------------------------------------------------------------------- creating the identity


def test_create_identity_makes_a_private_usable_pair(tmp_path):
    key, cert = tmp_path / CLIENT_KEY, tmp_path / CLIENT_CERT
    certs.create_identity(key, cert, common_name="tcl-me-remo")
    assert mode(key) == 0o600 and mode(cert) == 0o600
    assert sorted(path.name for path in tmp_path.iterdir()) == [CLIENT_KEY, CLIENT_CERT]  # no scratch files left

    assert "tcl-me-remo" in openssl("x509", "-in", str(cert), "-noout", "-subject")
    modulus, exponent = certs.rsa_public_numbers(der_of(cert))
    assert modulus.bit_length() == 2048 and exponent == 65537
    # The key belongs to the certificate and neither is encrypted.
    assert openssl("rsa", "-in", str(key), "-noout", "-modulus").strip() == f"Modulus={modulus:X}"
    openssl("verify", "-CAfile", str(cert), str(cert))  # self-signed

    identity = certs.load_identity(key, cert)
    assert identity.certificate == der_of(cert)
    assert identity.context.verify_mode == ssl.CERT_NONE and identity.context.check_hostname is False
    assert identity.context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_create_identity_sanitises_the_name(tmp_path):
    key, cert = tmp_path / CLIENT_KEY, tmp_path / CLIENT_CERT
    certs.create_identity(key, cert, common_name="evil\n[req]\nx = $ENV::HOME / # \"name\"")
    subject = openssl("x509", "-in", str(cert), "-noout", "-subject")
    assert "evilreqx  ENVHOME   name" in subject


def test_key_is_never_exposed_while_openssl_runs(tmp_path):
    """A stand-in openssl notes what it finds: the key file must already exist, private."""
    log = tmp_path / "seen.log"
    wrapper = tmp_path / "openssl-spy"
    wrapper.write_text(
        "#!/bin/sh\n"
        'out=""; prev=""\n'
        'for arg in "$@"; do [ "$prev" = "-out" ] && out="$arg"; prev="$arg"; done\n'
        'dir=$(stat -c %a "$(dirname "$out")"); file=$(stat -c %a "$out" 2>/dev/null)\n'
        f'echo "$1 umask=$(umask) dir=$dir file=$file" >> {log}\n'
        'exec openssl "$@"\n'
    )
    wrapper.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    certs.create_identity(state / CLIENT_KEY, state / CLIENT_CERT, common_name="x", openssl=str(wrapper))
    genrsa, req = log.read_text().splitlines()
    assert genrsa == "genrsa umask=0077 dir=700 file=600"  # the key file is created 0600 before openssl writes it
    assert req.startswith("req umask=0077 dir=700")
    assert mode(state / CLIENT_KEY) == 0o600


def test_create_identity_without_openssl(tmp_path):
    with pytest.raises(certs.OpensslMissing, match="no-such-openssl"):
        certs.create_identity(tmp_path / "k", tmp_path / "c", common_name="x", openssl="/nonexistent/no-such-openssl")
    assert list(tmp_path.iterdir()) == []


def test_create_identity_when_openssl_fails_leaves_nothing(tmp_path):
    failing = tmp_path / "bin" / "openssl"
    failing.parent.mkdir()
    failing.write_text("#!/bin/sh\necho 'boom' >&2\nexit 3\n")
    failing.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    with pytest.raises(certs.CertError, match="exit status 3") as raised:
        certs.create_identity(state / CLIENT_KEY, state / CLIENT_CERT, common_name="x", openssl=str(failing))
    assert not isinstance(raised.value, certs.OpensslMissing)
    assert list(state.iterdir()) == []


def test_create_identity_rejects_what_does_not_load(tmp_path):
    """An openssl that "succeeds" without writing a usable pair must not replace anything."""
    useless = tmp_path / "openssl"
    useless.write_text("#!/bin/sh\nexit 0\n")
    useless.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    with pytest.raises(certs.CertError):
        certs.create_identity(state / CLIENT_KEY, state / CLIENT_CERT, common_name="x", openssl=str(useless))
    assert list(state.iterdir()) == []


def test_load_identity_errors(tmp_path):
    key, cert = tmp_path / CLIENT_KEY, tmp_path / CLIENT_CERT
    with pytest.raises(FileNotFoundError):
        certs.load_identity(key, cert)
    other_cert, other_key = make_certificate(tmp_path, "a", "/CN=a")
    mismatched_cert, _ = make_certificate(tmp_path, "b", "/CN=b")
    with pytest.raises(certs.CertError):
        certs.load_identity(other_key, mismatched_cert)  # key of another certificate
    cert.write_text("not a certificate")
    key.write_text("not a key")
    with pytest.raises(certs.CertError) as raised:
        certs.load_identity(key, cert)
    assert "not a" not in str(raised.value)  # file contents never end up in messages
    assert certs.load_identity(other_key, other_cert).certificate == der_of(other_cert)


# ---------------------------------------------------------------------------- the store


def test_store_creates_a_private_directory_and_never_replaces_an_identity(tmp_path):
    directory = tmp_path / "var" / "atv"
    directory.mkdir(parents=True, mode=0o755)  # left too open by somebody else
    store = Store(directory)
    assert not store.has_identity()
    assert store.ensure_identity(common_name="tcl-me-remo", openssl="openssl") is True
    assert mode(directory) == 0o700 and mode(store.key_path) == 0o600
    before = store.key_path.read_bytes(), store.cert_path.read_bytes()

    assert store.ensure_identity(common_name="tcl-me-remo", openssl="openssl") is False
    assert store.ensure_identity(common_name="other", openssl="/nonexistent/openssl") is False  # not even looked for
    assert (store.key_path.read_bytes(), store.cert_path.read_bytes()) == before
    assert store.load_identity().certificate == der_of(store.cert_path)


def test_store_replaces_half_an_identity_and_its_stale_record(tmp_path):
    store = Store(tmp_path / "atv")
    store.ensure_identity(common_name="x", openssl="openssl")
    store.write_record(PairingRecord("192.168.1.9", "ab" * 32, "Old TV"))
    old_key = store.key_path.read_bytes()
    store.cert_path.unlink()  # as after a crash between the two renames
    assert not store.has_identity()
    assert store.ensure_identity(common_name="x", openssl="openssl") is True
    assert store.key_path.read_bytes() != old_key
    assert store.read_record() is None  # it described a pairing made with the lost identity


def test_concurrent_creation_yields_one_matching_pair(tmp_path):
    store = Store(tmp_path / "atv")
    results: list[bool] = []
    threads = [
        threading.Thread(target=lambda: results.append(store.ensure_identity(common_name="x", openssl="openssl")))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, False, False, True]
    store.load_identity()  # key and certificate belong together


def test_store_record_round_trip_and_permissions(tmp_path):
    store = Store(tmp_path / "atv")
    assert store.read_record() is None
    store.directory.mkdir()
    record = PairingRecord(host="192.168.1.50", fingerprint="0f" * 32, name="Living Room TV")
    store.write_record(record)
    assert store.read_record() == record
    assert mode(store.record_path) == 0o600
    assert sorted(path.name for path in store.directory.iterdir()) == [RECORD]
    store.write_record(PairingRecord(host="tv.local", fingerprint="AB" * 32))
    assert store.read_record() == PairingRecord(host="tv.local", fingerprint="ab" * 32, name="")


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[]",
        "{}",
        '{"host": "", "fingerprint": "' + "ab" * 32 + '"}',
        '{"host": "tv", "fingerprint": "xyz"}',
        '{"host": "tv", "fingerprint": "' + "zz" * 32 + '"}',
        '{"host": 5, "fingerprint": "' + "ab" * 32 + '"}',
        '{"host": "tv", "fingerprint": "' + "ab" * 32 + '", "name": ["x"]}',
        "\xff\xfe",
        '"just a string"',
        "12345",
        "null",
        "[" * 16000,  # nested deeper than the JSON reader will go
        '{"host": ' * 3000,
    ],
)
def test_store_ignores_a_damaged_record(tmp_path, content):
    store = Store(tmp_path)
    store.record_path.write_bytes(content.encode("latin-1"))
    assert store.read_record() is None
    # ... and so does the assist built on it: the service must start whatever is in that file.
    assert AtvAssist(tmp_path).status() == {"paired": False, "pairing": False, "pairing_host": ""}


def test_store_remove_forgets_everything(tmp_path):
    store = Store(tmp_path / "atv")
    store.remove()  # nothing there yet: fine
    store.ensure_identity(common_name="x", openssl="openssl")
    store.write_record(PairingRecord("tv", "ab" * 32))
    store.remove()
    assert list(store.directory.iterdir()) == []
    assert not store.has_identity() and store.read_record() is None


def leave_scratch_behind(directory) -> None:
    """What a process killed in the middle of making a key, or of saving the record, leaves."""
    directory.mkdir(parents=True, exist_ok=True)
    for stem in (".new-3g8sh72f", ".new-empty"):
        (directory / stem).mkdir(mode=0o700)
    (directory / ".new-3g8sh72f" / "key.pem").write_text("an orphaned private key")
    (directory / ".new-3g8sh72f" / "key.pem").chmod(0o600)
    (directory / ".new-3g8sh72f" / "request.cnf").write_text("[req]")
    (directory / ".new-stray-file").write_text("x")
    (directory / "pairing.json.tmp").write_text("{")


def test_remove_also_removes_what_a_killed_process_left_behind(tmp_path):
    store = Store(tmp_path / "atv")
    store.ensure_identity(common_name="x", openssl="openssl")
    store.write_record(PairingRecord("tv", "ab" * 32))
    leave_scratch_behind(store.directory)
    (store.directory / "unrelated").write_text("not ours to delete")
    store.remove()
    assert [path.name for path in store.directory.iterdir()] == ["unrelated"]


def test_creating_an_identity_clears_older_scratch_files(tmp_path):
    store = Store(tmp_path / "atv")
    leave_scratch_behind(store.directory)
    assert store.ensure_identity(common_name="x", openssl="openssl") is True
    assert sorted(path.name for path in store.directory.iterdir()) == sorted([CLIENT_KEY, CLIENT_CERT])
    store.load_identity()

    # With an identity in place only the leftovers go; the identity is not touched.
    before = store.key_path.read_bytes(), store.cert_path.read_bytes()
    leave_scratch_behind(store.directory)
    assert store.ensure_identity(common_name="x", openssl="/nonexistent/openssl") is False
    assert sorted(path.name for path in store.directory.iterdir()) == sorted([CLIENT_KEY, CLIENT_CERT])
    assert (store.key_path.read_bytes(), store.cert_path.read_bytes()) == before


def test_scratch_cleanup_does_not_follow_links(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("keep")
    store = Store(tmp_path / "atv")
    store.directory.mkdir()
    (store.directory / ".new-link").symlink_to(outside, target_is_directory=True)
    store.remove()
    assert list(store.directory.iterdir()) == [] and (outside / "precious").read_text() == "keep"

"""Wi-Fi assist: a minimal client for the Android TV Remote protocol v2.

This is the protocol the Google TV phone app speaks.  The assist is opt-in and used to open
a TV input directly and as a fallback for the power keys.  Standard library only, plus the
``openssl`` command-line tool to create the client certificate once.

    proto.py      protobuf wire format and message framing
    messages.py   the message tables of the pairing and remote protocols
    certs.py      the client key and certificate; RSA numbers and names out of X.509
    store.py      what is kept on disk (identity, which TV was paired)
    channel.py    one TLS connection carrying framed messages, with deadlines
    pairing.py    the pairing exchange on TCP 6467 and the secret derived from the TV's code
    remote.py     one short command session on TCP 6466
    discovery.py  finding TVs with mDNS
    assist.py     AtvAssist, which implements interfaces.WifiAssist
    util.py       cleaning names that came from the network, wording of system errors

Written from descriptions of the protocol: the AOSP "Polo" pairing reference, the Aymkdn
wiki, and the behaviour of louis49/androidtv-remote and tronikos/androidtvremote2.
"""

from .assist import AtvAssist

__all__ = ["AtvAssist"]

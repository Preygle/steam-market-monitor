"""
Steam login by QR code -- the same flow as the QR on Steam's own login page,
approved in the Steam mobile app. No password is typed anywhere.

The session is requested as the Steam *mobile app* platform, because for
that platform the access token is itself a valid steamLoginSecure cookie
(the WebBrowser platform needs an extra cookie-transfer dance). Each CI run
turns the long-lived refresh token (~200 days) into a short-lived access
token with GenerateAccessTokenForApp.

Wire format: IAuthenticationService takes a protobuf request, base64'd into
the `input_protobuf_encoded` form field, and answers in protobuf with the
result code in the x-eresult header -- as node-steam-session's
WebApiTransport does. Field numbers are from Valve's
steammessages_auth.steamclient.proto (SteamDatabase/Protobufs).

The refresh token is account access. It is only ever stored sealed by
Vault (key: the STATE_KEY repository secret) and is never printed.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import struct
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

API = "https://api.steampowered.com/IAuthenticationService/{}/v1/"
DEVICE_NAME = "steammkt bot (GitHub Actions)"   # shown in your Steam app
# What node-steam-session sends for its MobileApp platform.
MOBILE_HEADERS = {
    "User-Agent": "okhttp/4.9.2",
    "Cookie": "mobileClient=android; mobileClientVersion=777777 3.10.3",
}
PLATFORM_MOBILE_APP = 3          # EAuthTokenPlatformType
OS_ANDROID_UNKNOWN = -500        # EOSType
GAMING_DEVICE_TYPE = 528         # what the app sends; meaning unknown
ERESULT_OK = 1


class SteamAuthError(Exception):
    pass


# ---------------------------------------------------------------- protobuf
def _varint(n: int) -> bytes:
    n &= (1 << 64) - 1          # negative int32s go out as 64-bit two's complement
    out = bytearray()
    while True:
        low, n = n & 0x7F, n >> 7
        if n:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def pb_int(no: int, v: int) -> bytes:
    return _varint(no << 3) + _varint(v)


def pb_bytes(no: int, v: bytes) -> bytes:
    return _varint(no << 3 | 2) + _varint(len(v)) + v


def pb_str(no: int, v: str) -> bytes:
    return pb_bytes(no, v.encode())


def pb_fixed64(no: int, v: int) -> bytes:
    return _varint(no << 3 | 1) + struct.pack("<Q", v)


def pb_decode(buf: bytes) -> dict[int, list]:
    """Field number -> values. Varints come back as ints, length-delimited
    fields as bytes, fixed64 as an int, fixed32 as its 4 raw bytes."""
    out: dict[int, list] = {}
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        no, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _read_varint(buf, i)
        elif wire == 1:
            v = struct.unpack_from("<Q", buf, i)[0]
            i += 8
        elif wire == 2:
            n, i = _read_varint(buf, i)
            v = buf[i:i + n]
            i += n
        elif wire == 5:
            v = buf[i:i + 4]
            i += 4
        else:
            raise SteamAuthError(f"unexpected protobuf wire type {wire}")
        out.setdefault(no, []).append(v)
    return out


# ---------------------------------------------------------------- transport
def _post(url: str, body: bytes) -> tuple[int, Optional[str], bytes]:
    r = requests.post(url, headers=MOBILE_HEADERS, timeout=20, files={
        "input_protobuf_encoded": (None, base64.b64encode(body).decode())})
    return r.status_code, r.headers.get("x-eresult"), r.content


def call(method: str, body: bytes, post: Optional[Callable] = None) -> dict[int, list]:
    status, eresult, content = (post or _post)(API.format(method), body)
    if status != 200 or (eresult is not None and int(eresult) != ERESULT_OK):
        raise SteamAuthError(f"{method} failed (HTTP {status}, eresult {eresult})")
    return pb_decode(content)


# ---------------------------------------------------------------- QR login
@dataclass
class QrSession:
    client_id: int
    challenge_url: str
    request_id: bytes
    interval: float = 5.0
    rotated: bool = False       # Steam issued a new challenge URL: re-send the QR


@dataclass
class LoginResult:
    refresh_token: str
    account_name: str = ""


def begin_qr(post: Optional[Callable] = None) -> QrSession:
    details = (pb_str(1, DEVICE_NAME) + pb_int(2, PLATFORM_MOBILE_APP)
               + pb_int(3, OS_ANDROID_UNKNOWN) + pb_int(4, GAMING_DEVICE_TYPE))
    r = call("BeginAuthSessionViaQR", pb_bytes(3, details), post)
    if not all(k in r for k in (1, 2, 3)):
        raise SteamAuthError("BeginAuthSessionViaQR: incomplete response")
    interval = struct.unpack("<f", r[4][0])[0] if 4 in r else 5.0
    return QrSession(r[1][0], r[2][0].decode(), r[3][0], max(interval, 1.0))


def poll(session: QrSession, post: Optional[Callable] = None) -> Optional[LoginResult]:
    """None while waiting for approval in the app; a LoginResult once approved."""
    r = call("PollAuthSessionStatus",
             pb_int(1, session.client_id) + pb_bytes(2, session.request_id), post)
    if 1 in r:
        session.client_id = r[1][0]
    if 2 in r:
        session.challenge_url = r[2][0].decode()
        session.rotated = True
    if 3 not in r:
        return None
    return LoginResult(r[3][0].decode(), r[6][0].decode() if 6 in r else "")


def qr_png(url: str) -> bytes:
    import segno
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=8, border=4)
    return buf.getvalue()


# ---------------------------------------------------------------- tokens
def jwt_claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def generate_access_token(refresh_token: str, post: Optional[Callable] = None) -> str:
    steamid = int(jwt_claims(refresh_token)["sub"])
    r = call("GenerateAccessTokenForApp",
             pb_str(1, refresh_token) + pb_fixed64(2, steamid) + pb_int(3, 0), post)
    if 1 not in r:
        raise SteamAuthError("GenerateAccessTokenForApp: no access token")
    return r[1][0].decode()


def community_cookie(steamid, access_token: str) -> str:
    """steamLoginSecure for a MobileApp session: '<steamid>||<token>', URL-encoded."""
    return f"{steamid}%7C%7C{access_token}"


# ---------------------------------------------------------------- storage
class Vault:
    """Encrypts secrets kept in the SQLite state.

    That state lives in the Actions cache, which pull requests from forks
    can restore on a public repo. Repository secrets never reach those runs,
    so with the key in STATE_KEY the cached login is only ciphertext to them.
    """

    def __init__(self, passphrase: str, salt: bytes):
        from cryptography.fernet import Fernet
        key = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, 200_000)
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def seal(self, text: str) -> str:
        return self._fernet.encrypt(text.encode()).decode()

    def open(self, sealed: str) -> Optional[str]:
        from cryptography.fernet import InvalidToken
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except InvalidToken:
            return None


def open_vault(store, passphrase: str) -> Optional[Vault]:
    if not passphrase:
        return None
    salt = store.get_meta("vault_salt")
    if not salt:
        salt = os.urandom(16).hex()
        store.set_meta("vault_salt", salt)
    return Vault(passphrase, bytes.fromhex(salt))


def save_login(store, vault: Vault, result: LoginResult) -> None:
    store.set_meta("steam_login", vault.seal(json.dumps(
        {"refresh_token": result.refresh_token, "account_name": result.account_name})))
    with store.tx() as c:
        c.execute("DELETE FROM meta WHERE key='steam_access'")


def load_login(store, vault: Optional[Vault]) -> Optional[dict]:
    sealed = store.get_meta("steam_login")
    if not sealed or vault is None:
        return None
    raw = vault.open(sealed)
    return json.loads(raw) if raw else None


def forget_login(store) -> None:
    with store.tx() as c:
        c.execute("DELETE FROM meta WHERE key IN ('steam_login', 'steam_access')")


@dataclass
class SteamSession:
    steamid: int
    cookie: str


def use_login(store, vault: Optional[Vault], post: Optional[Callable] = None,
              now: Optional[float] = None) -> Optional[SteamSession]:
    """The stored login as a community cookie, minting a new access token
    when the cached one is within an hour of expiry. None if there is no
    login or Steam rejects it (revoked, or past its ~200 days)."""
    login = load_login(store, vault)
    if not login:
        return None
    now = now or time.time()
    sealed = store.get_meta("steam_access")
    token = vault.open(sealed) if sealed else None
    if not token or jwt_claims(token).get("exp", 0) - now < 3600:
        try:
            token = generate_access_token(login["refresh_token"], post)
        except (SteamAuthError, requests.RequestException) as e:
            print(f"steam login: could not refresh the session ({e})")
            return None
        store.set_meta("steam_access", vault.seal(token))
    steamid = int(jwt_claims(login["refresh_token"])["sub"])
    return SteamSession(steamid, community_cookie(steamid, token))

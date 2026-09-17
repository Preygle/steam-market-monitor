"""Steam QR login: the wire format, the token handling, and that the login
never sits in the (fork-readable) Actions cache as plaintext."""
import base64
import json
import struct
from types import SimpleNamespace

import pytest
from fakes import FakeApi, FakeClient
from steammkt import steamauth as sa
from steammkt.alerts import AlertRouter
from steammkt.bot import TelegramBot
from steammkt.events import EventCalendar
from steammkt.fees import WalletConfig
from steammkt.monitor import Monitor
from steammkt.reports import status_report
from steammkt.store import Store
from steammkt.strategy import Strategy

CFG = WalletConfig()
STEAMID = 76561198000000000


def jwt(**claims):
    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'EdDSA'})}.{enc(claims)}.sig"


REFRESH = jwt(sub=str(STEAMID), exp=4102444800)


class FakeSteam:
    """Answers IAuthenticationService the way Steam does: a protobuf body,
    the result code in x-eresult."""

    def __init__(self, approve_after=0, eresult="1"):
        self.calls, self.polls = [], 0
        self.approve_after, self.eresult = approve_after, eresult

    def __call__(self, url, body):
        method = url.split("/")[-3]
        self.calls.append((method, sa.pb_decode(body)))
        if method == "BeginAuthSessionViaQR":
            resp = (sa.pb_int(1, 1234) + sa.pb_str(2, "https://s.team/q/1/abc")
                    + sa.pb_bytes(3, b"\x01\x02")
                    + sa._varint(4 << 3 | 5) + struct.pack("<f", 5.0))
        elif method == "PollAuthSessionStatus":
            self.polls += 1
            resp = (b"" if self.polls <= self.approve_after
                    else sa.pb_str(3, REFRESH) + sa.pb_str(6, "preygle_acct"))
        else:   # GenerateAccessTokenForApp
            resp = sa.pb_str(1, jwt(sub=str(STEAMID), exp=4102444800))
        return 200, self.eresult, resp


# ---------------------------------------------------------------- wire format
def test_varints_match_protobuf():
    assert sa._varint(300) == b"\xac\x02"
    # int32 -500 (EOSType AndroidUnknown) goes out as 64-bit two's complement
    assert sa.pb_decode(sa.pb_int(3, -500))[3] == [(1 << 64) - 500]


def test_qr_login_is_requested_as_a_desktop_client():
    steam = FakeSteam()
    s = sa.begin_qr(post=steam)
    assert (s.client_id, s.challenge_url, s.request_id, s.interval) == \
        (1234, "https://s.team/q/1/abc", b"\x01\x02", 5.0)
    details = sa.pb_decode(steam.calls[0][1][3][0])
    assert details[1] == [sa.DEVICE_NAME.encode()]
    assert details[2] == [sa.PLATFORM_STEAM_CLIENT]


def test_poll_waits_then_returns_the_login():
    steam = FakeSteam(approve_after=1)
    s = sa.begin_qr(post=steam)
    assert sa.poll(s, post=steam) is None
    res = sa.poll(s, post=steam)
    assert (res.refresh_token, res.account_name) == (REFRESH, "preygle_acct")


def test_steam_errors_carry_the_result_code():
    with pytest.raises(sa.SteamAuthError, match="eresult 9"):
        sa.begin_qr(post=FakeSteam(eresult="9"))


# ---------------------------------------------------------------- storage
def test_vault_round_trip_and_wrong_key(tmp_path):
    store = Store(tmp_path / "m.db")
    v = sa.open_vault(store, "correct horse battery staple")
    sealed = v.seal("secret-token")
    assert "secret-token" not in sealed
    assert v.open(sealed) == "secret-token"
    assert sa.open_vault(store, "wrong key").open(sealed) is None
    assert sa.open_vault(store, "") is None


def test_stored_login_becomes_a_community_cookie(tmp_path):
    store = Store(tmp_path / "m.db")
    vault = sa.open_vault(store, "k")
    sa.save_login(store, vault, sa.LoginResult(REFRESH, "acct"))
    steam = FakeSteam()
    s = sa.use_login(store, vault, post=steam)
    assert s.steamid == STEAMID and s.cookie.startswith(f"{STEAMID}%7C%7C")
    sa.use_login(store, vault, post=steam)       # cached access token reused
    assert [m for m, _ in steam.calls].count("GenerateAccessTokenForApp") == 1
    dump = json.dumps([dict(r) for r in store.q("SELECT * FROM meta")])
    assert REFRESH not in dump and "acct" not in dump


def test_no_state_key_means_no_login(tmp_path):
    store = Store(tmp_path / "m.db")
    vault = sa.open_vault(store, "k")
    sa.save_login(store, vault, sa.LoginResult(REFRESH, "acct"))
    assert sa.use_login(store, None) is None


# ---------------------------------------------------------------- the bot
@pytest.fixture
def mon(tmp_path):
    return Monitor(Store(tmp_path / "m.db"), FakeClient(), Strategy(CFG),
                   AlertRouter([]), EventCalendar("config/events.yaml"), CFG)


def login_bot(mon, steam, with_vault=True, text="/login"):
    api = FakeApi([{"update_id": 1, "message": {"chat": {"id": 42}, "text": text}}])
    auth = SimpleNamespace(begin_qr=lambda: sa.begin_qr(post=steam),
                           poll=lambda s: sa.poll(s, post=steam))
    uploads = []
    bot = TelegramBot("t", 42, mon, api=api, auth=auth,
                      upload=lambda *a: uploads.append(a) or {"ok": True},
                      vault=sa.open_vault(mon.store, "k") if with_vault else None)
    return bot, api, uploads


def texts(api):
    return [p["text"] for m, p in api.calls if m == "sendMessage"]


def test_login_sends_a_qr_then_stores_the_login_sealed(mon, monkeypatch):
    monkeypatch.setattr(sa, "qr_png", lambda url: b"PNG:" + url.encode())
    bot, api, uploads = login_bot(mon, FakeSteam(approve_after=1))
    bot.listen(0)
    method, fields, field, filename, png = uploads[0]
    assert method == "sendPhoto" and png == b"PNG:https://s.team/q/1/abc"
    assert sa.DEVICE_NAME in fields["caption"]
    assert "Logged in to Steam as preygle_acct" in texts(api)[-1]
    assert sa.load_login(mon.store, bot.vault)["refresh_token"] == REFRESH
    assert "Steam login  active" in status_report(mon)
    # Regression: a finished login must not hold the listen window open for
    # the rest of the QR's lifetime (that spun this test for 4 minutes).
    assert len(api.calls) < 10


def test_login_is_refused_without_state_key(mon):
    bot, api, uploads = login_bot(mon, FakeSteam(), with_vault=False)
    bot.listen(0)
    assert uploads == [] and "STATE_KEY" in texts(api)[0]


def test_an_unapproved_qr_expires(mon, monkeypatch):
    monkeypatch.setattr(sa, "qr_png", lambda url: b"png")
    monkeypatch.setattr("steammkt.bot.LOGIN_WINDOW_S", -1)
    bot, api, uploads = login_bot(mon, FakeSteam(approve_after=99))
    bot.listen(0)
    assert "expired" in texts(api)[-1]
    assert sa.load_login(mon.store, bot.vault) is None


def test_logout_forgets_the_login(mon):
    bot, api, _ = login_bot(mon, FakeSteam(), text="/logout")
    sa.save_login(mon.store, bot.vault, sa.LoginResult(REFRESH, "acct"))
    bot.listen(0)
    assert sa.load_login(mon.store, bot.vault) is None
    assert "authorizeddevices" in texts(api)[0]

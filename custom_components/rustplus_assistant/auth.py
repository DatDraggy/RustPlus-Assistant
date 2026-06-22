"""Self-contained Rust+ credential acquisition: Steam QR login -> Rust+ token -> FCM register.

No browser, no extension, no third-party site. Steam QR via the IAuthenticationService
WebAPI (tiny hand-rolled protobuf), the Facepunch companion OpenID flow over plain HTTP,
and FCM registration via push_receiver's AndroidFCM — the same library the integration
already listens with, so the token receives plaintext data messages it can decode.
Produces credentials in the exact shape the existing push_receiver listener consumes:

    {"expo_push_token": str,
     "fcm_credentials": {"fcm": {"token": str}, "gcm": {"androidId": str, "securityToken": str}},
     "rustplus_auth_token": str}

Everything here is blocking (`requests` + push_receiver); the whole flow is meant to be
driven from the config flow via ``hass.async_add_executor_job``.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import struct
import time
import uuid
from urllib.parse import unquote, urljoin

import requests

_LOGGER = logging.getLogger(__name__)

# --- Steam ---
STEAM_API = "https://api.steampowered.com/IAuthenticationService"
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
DEVICE_FRIENDLY_NAME = "Home Assistant Rust+"

# --- Facepunch companion ---
FP = "https://companion-rust.facepunch.com"

# --- FCM / Expo (Facepunch companion app, from the official app's google-services) ---
FCM_PROJECT_ID = "rust-companion-app"
FCM_APP_ID = "1:976529667804:android:d6f1ddeb4403b338fea619"
FCM_API_KEY = "AIzaSyB5y2y-Tzqb4-I4Qnlsh_9naYv_TD8pCvY"
FCM_SENDER_ID = "976529667804"
ANDROID_PACKAGE = "com.facepunch.rust.companion"
ANDROID_CERT = "E28D05345FB78A7A1A63D70F4A302DBF426CA5AD"
EXPO_PROJECT_ID = "49451aca-a822-41e6-ad59-955718d0ff9c"
EXPO_URL = "https://exp.host/--/api/v2/push/getExpoPushToken"
FP_PUSH_REGISTER = "https://companion-rust.facepunch.com/api/push/register"


class RustPlusAuthError(Exception):
    """Raised when credential acquisition fails."""


# --------------------------------------------------------------------------- #
# Minimal protobuf (only what the Steam auth WebAPI needs)
# --------------------------------------------------------------------------- #
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_varint(buf: bytes, i: int):
    shift = res = 0
    while True:
        b = buf[i]
        i += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            return res, i
        shift += 7


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _p_str(field: int, s) -> bytes:
    b = s.encode() if isinstance(s, str) else s
    return _tag(field, 2) + _varint(len(b)) + b


def _p_vint(field: int, n: int) -> bytes:
    return _tag(field, 0) + _varint(n)


def _p_msg(field: int, inner: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(inner)) + inner


def _pb_decode(buf: bytes) -> dict:
    out: dict = {}
    i, n = 0, len(buf)
    while i < n:
        key, i = _read_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _read_varint(buf, i)
        elif wire == 2:
            ln, i = _read_varint(buf, i)
            val = bytes(buf[i:i + ln])
            i += ln
        elif wire == 5:
            val = bytes(buf[i:i + 4])
            i += 4
        elif wire == 1:
            val = bytes(buf[i:i + 8])
            i += 8
        else:
            raise RustPlusAuthError(f"bad protobuf wire type {wire}")
        out.setdefault(field, []).append(val)
    return out


# --------------------------------------------------------------------------- #
# The flow
# --------------------------------------------------------------------------- #
class RustPlusQRAuth:
    """Drives: begin() -> (poll() until token) -> complete() -> full credentials."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = WEB_UA
        self._client_id: int | None = None
        self._request_id: bytes | None = None

    # ---- Steam QR ---------------------------------------------------------- #
    def begin(self) -> str:
        """Start a Steam QR auth session. Returns the challenge URL to encode as a QR."""
        device_details = _p_str(1, DEVICE_FRIENDLY_NAME) + _p_vint(2, 2)  # platform_type=WebBrowser
        req = _p_msg(3, device_details)
        r = self.session.post(
            f"{STEAM_API}/BeginAuthSessionViaQR/v1/",
            data={"input_protobuf_encoded": base64.b64encode(req).decode()},
            timeout=30,
        )
        r.raise_for_status()
        d = _pb_decode(r.content)
        self._client_id = d[1][0]
        self._request_id = d[3][0]
        return d[2][0].decode()

    def poll(self) -> str | None:
        """Poll once. Returns the Steam refresh token once approved, else None."""
        if self._client_id is None:
            raise RustPlusAuthError("begin() must be called before poll()")
        req = _p_vint(1, self._client_id) + _p_str(2, self._request_id)
        r = self.session.post(
            f"{STEAM_API}/PollAuthSessionStatus/v1/",
            data={"input_protobuf_encoded": base64.b64encode(req).decode()},
            timeout=30,
        )
        r.raise_for_status()
        d = _pb_decode(r.content)
        return d[3][0].decode() if 3 in d else None

    # ---- Steam web session ------------------------------------------------- #
    @staticmethod
    def _steamid_from_jwt(token: str) -> str:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))["sub"]

    def _load_web_cookies(self, refresh_token: str, steamid: str) -> None:
        """finalizelogin + per-domain settoken -> steamLoginSecure cookies in the session jar."""
        sessionid = secrets.token_hex(12)
        r = self.session.post(
            "https://login.steampowered.com/jwt/finalizelogin",
            files={"nonce": (None, refresh_token), "sessionid": (None, sessionid),
                   "redir": (None, "https://steamcommunity.com/login/home/?goto=")},
            headers={"Origin": "https://steamcommunity.com", "Referer": "https://steamcommunity.com/"},
            timeout=30,
        )
        r.raise_for_status()
        info = r.json()
        if "transfer_info" not in info:
            raise RustPlusAuthError("Steam finalizelogin returned no transfer_info")
        for ti in info["transfer_info"]:
            body = {"steamID": (None, steamid)}
            for k, v in ti["params"].items():
                body[k] = (None, str(v))
            self.session.post(ti["url"], files=body, timeout=30)
        self.session.cookies.set("sessionid", sessionid, domain="steamcommunity.com")

    # ---- Facepunch OpenID -> Rust+ token ----------------------------------- #
    def _get_rust_token(self) -> str:
        s = self.session
        r = s.get(FP + "/login", timeout=30)
        m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r.text)
        rvt = m.group(1) if m else ""
        r = s.post(FP + "/login", data={"returnUrl": "/", "__RequestVerificationToken": rvt},
                   allow_redirects=False, timeout=30)
        loc = r.headers.get("location")
        referer = FP + "/login"
        for _ in range(16):
            if not loc:
                break
            if "openid/login" in loc and loc.startswith("https://steamcommunity.com"):
                r = s.get(loc, headers={"Referer": referer}, allow_redirects=False, timeout=30)
                body = r.text
                fm = re.search(r'<form\b[^>]*openidForm[\s\S]*?</form>', body, re.I) or \
                    re.search(r'<form\b[^>]*action="[^"]*openid/login[\s\S]*?</form>', body, re.I)
                scope = fm.group(0) if fm else body
                inputs = dict(re.findall(r'<input\b[^>]*name="([^"]+)"[^>]*?value="([^"]*)"', scope))
                am = re.search(r'<form\b[^>]*action="([^"]+)"', body)
                action = urljoin(loc, am.group(1)) if am else "https://steamcommunity.com/openid/login"
                # consent form is multipart/form-data (its enctype) — urlencoded gives "Invalid Params"
                files = {k: (None, v) for k, v in inputs.items()}
                r = s.post(action, files=files,
                           headers={"Origin": "https://steamcommunity.com", "Referer": loc},
                           allow_redirects=False, timeout=30)
                _l = r.headers.get("location")
                loc = urljoin(action, _l) if _l else None
                referer = action
                continue
            r = s.get(loc, headers={"Referer": referer}, allow_redirects=False, timeout=30)
            _l = r.headers.get("location")
            nloc = urljoin(loc, _l) if _l else ""
            for cand in (nloc, loc):
                tm = re.search(r"[?&#]token=([^&\s\"'<>]{16,})", cand)
                if tm:
                    return unquote(tm.group(1))
            if r.status_code >= 500:
                raise RustPlusAuthError(f"Facepunch callback error {r.status_code}")
            referer, loc = loc, nloc
        raise RustPlusAuthError("Rust+ token not found in the OpenID redirect chain")

    # ---- FCM registration (push_receiver AndroidFCM = same lib as the listener) -- #
    @staticmethod
    def _android_fcm_register(attempts: int = 8) -> dict:
        """Register an Android-app FCM token for the Rust+ companion app.

        Uses push_receiver's own AndroidFCM — the same library the integration listens
        with — so the token receives *plaintext* data messages push_receiver can decode.
        (A firebase_messaging/web-push registration delivers ECE-encrypted payloads that
        push_receiver can't decrypt, which shows up as empty notifications.) Google's GCM
        register is flaky (PHONE_REGISTRATION_ERROR), so retry with backoff. Returns
        {"gcm": {"androidId", "securityToken"}, "fcm": {"token"}}.
        """
        from push_receiver.android_fcm_register import AndroidFCM

        last: Exception | None = None
        for i in range(attempts):
            try:
                return AndroidFCM.register(
                    FCM_API_KEY, FCM_PROJECT_ID, FCM_SENDER_ID, FCM_APP_ID,
                    ANDROID_PACKAGE, ANDROID_CERT,
                )
            except Exception as e:  # noqa: BLE001 - transient Google GCM errors
                last = e
                _LOGGER.debug("FCM register attempt %d/%d failed: %s", i + 1, attempts, e)
                time.sleep(min(2 + i * 2, 15))
        raise RustPlusAuthError(f"FCM registration failed after {attempts} attempts: {last}")

    def _fcm_register(self, rust_token: str, device_id: str | None = None) -> dict:
        """Register for push notifications and assemble the final credentials.

        ``device_id`` should be stable per HA install (so re-auth replaces this install's
        own push slot) and distinct between installs (so instances don't invalidate each
        other on Facepunch — registrations are keyed by DeviceId). Falls back to a random
        uuid if the caller doesn't supply one.
        """
        fcm_credentials = self._android_fcm_register()
        fcm_token = fcm_credentials["fcm"]["token"]
        device_id = device_id or str(uuid.uuid4())
        s = self.session

        er = s.post(EXPO_URL, json={
            "type": "fcm", "deviceId": device_id, "development": False,
            "appId": ANDROID_PACKAGE, "deviceToken": fcm_token, "projectId": EXPO_PROJECT_ID,
        }, timeout=30)
        er.raise_for_status()
        expo_token = er.json()["data"]["expoPushToken"]

        fr = s.post(FP_PUSH_REGISTER, json={
            "AuthToken": rust_token, "DeviceId": device_id, "PushKind": 3, "PushToken": expo_token,
        }, timeout=30)
        if fr.status_code >= 400:
            raise RustPlusAuthError(f"Facepunch push register failed: {fr.status_code}")

        return {
            "expo_push_token": expo_token,
            "fcm_credentials": fcm_credentials,
            "rustplus_auth_token": rust_token,
        }

    # ---- Orchestration ----------------------------------------------------- #
    def complete(self, refresh_token: str, device_id: str | None = None) -> dict:
        """After approval: Steam web session -> Rust+ token -> FCM register -> full credentials.

        ``device_id`` is threaded through to the Facepunch push registration so each HA
        install keeps its own slot (see :meth:`_fcm_register`).
        """
        steamid = self._steamid_from_jwt(refresh_token)
        self._load_web_cookies(refresh_token, steamid)
        rust_token = self._get_rust_token()
        return self._fcm_register(rust_token, device_id)

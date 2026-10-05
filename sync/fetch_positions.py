#!/usr/bin/env python3
"""Fetch Trading 212 positions and publish them encrypted for the Stock Watch app.

Runs on GitHub Actions. Reads T212_API_KEY, T212_API_SECRET and APP_PASSPHRASE
from the environment, writes data/positions.enc (AES-256-GCM, key from
PBKDF2-SHA256 of the passphrase). Nothing unencrypted is written anywhere.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

BASE = os.environ.get("T212_BASE", "https://live.trading212.com/api/v0")
KEY = os.environ.get("T212_API_KEY", "").strip()
SECRET = os.environ.get("T212_API_SECRET", "").strip()
PASS = os.environ.get("APP_PASSPHRASE", "")
OUT = os.environ.get("OUT", "site/data/positions.enc")
ITER = 310000

KNOWN_ISIN = {
    "IE00BFMXXD54": "VUAA",  # Vanguard S&P 500 UCITS ETF (USD) Acc
    "IE00B5BMR087": "CSPX",  # iShares Core S&P 500 UCITS ETF Acc
    "IE00BK5BQT80": "VWCE",  # Vanguard FTSE All-World UCITS ETF Acc
}


def auth_headers(mode):
    if mode == "basic" and SECRET:
        tok = base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
        return {"Authorization": f"Basic {tok}"}
    return {"Authorization": KEY}


def get(path, mode):
    req = urllib.request.Request(BASE + path, headers={**auth_headers(mode), "Accept": "application/json", "User-Agent": "stock-watch-sync"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch(path):
    """Try Basic auth (current docs) then raw key (older API); retry once on 429."""
    last = None
    for mode in ("basic", "raw"):
        for attempt in range(2):
            try:
                return get(path, mode)
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 429:
                    time.sleep(3)
                    continue
                if e.code in (401, 403):
                    break  # try the other auth mode
                raise
    raise last


# Trading 212 keeps some legacy tickers; map them to the symbols the app and Finnhub use.
TICKER_ALIAS = {"FB": "META"}


def sym_from(ticker, isin):
    if isin and isin in KNOWN_ISIN:
        return KNOWN_ISIN[isin]
    base = (ticker or "").split("_")[0]
    base = re.sub(r"[a-z]+$", "", base)  # T212 marks non-US listings with a lowercase suffix letter
    return TICKER_ALIAS.get(base, base) or ticker


def normalize(p):
    inst = p.get("instrument") or {}
    wallet = p.get("walletImpact") or {}
    ticker = inst.get("ticker") or p.get("ticker") or ""
    isin = inst.get("isin") or p.get("isin")
    qty = p.get("quantity")
    cur_px = p.get("currentPrice")
    avg = p.get("averagePricePaid", p.get("averagePrice"))
    value = wallet.get("currentValue")
    if value is None and qty is not None and cur_px is not None:
        value = qty * cur_px
    return {
        "sym": sym_from(ticker, isin),
        "ticker": ticker,
        "isin": isin,
        "name": inst.get("name") or p.get("name"),
        "currency": inst.get("currency") or p.get("currency"),
        "qty": qty,
        "avgPrice": avg,
        "currentPrice": cur_px,
        "value": value,
        "valueCurrency": wallet.get("currency"),
        "ppl": wallet.get("unrealizedProfitLoss", p.get("ppl")),
        "fxPpl": wallet.get("fxImpact", p.get("fxPpl")),
        "qtyInPies": p.get("quantityInPies", p.get("pieQuantity")),
    }


def encrypt(obj):
    salt = os.urandom(16)
    iv = os.urandom(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER)
    key = kdf.derive(PASS.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, json.dumps(obj, separators=(",", ":")).encode(), None)
    b = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": ITER, "salt": b(salt), "iv": b(iv), "ct": b(ct)}


def write_status(**kw):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(os.path.join(os.path.dirname(OUT), "status.json"), "w") as f:
        json.dump({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **kw}, f)


def main():
    missing = [n for n, v in (("T212_API_KEY", KEY), ("APP_PASSPHRASE", PASS)) if not v]
    if missing:
        print(f"{', '.join(missing)} missing; skipping sync.")
        write_status(ok=False, reason="secrets_missing", missing=missing)
        return 0
    positions = fetch("/equity/positions")
    if isinstance(positions, dict):
        positions = positions.get("items") or positions.get("positions") or []
    account = None
    for path in ("/equity/account/summary", "/equity/account/cash"):
        try:
            account = fetch(path)
            break
        except Exception as e:  # noqa: BLE001
            print(f"{path}: {e}")
    payload = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "positions": [normalize(p) for p in positions],
        "account": account,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(encrypt(payload), f)
    write_status(ok=True, count=len(payload["positions"]), synced_at=payload["at"])
    print(f"Wrote {OUT}: {len(payload['positions'])} positions at {payload['at']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as e:
        print(f"Trading 212 API error {e.code}: {e.reason}")
        write_status(ok=False, reason="t212_http_error", code=e.code)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"Sync failed: {type(e).__name__}: {e}")
        write_status(ok=False, reason="error", detail=type(e).__name__)
        sys.exit(1)

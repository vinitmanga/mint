import os
import json
import asyncio
import requests
import websockets
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
import uvicorn
import threading

# ── FASTAPI (UPTIME) SETUP ─────────────────────────────────────────────────
fast_app = FastAPI()

@fast_app.get("/")
async def root():
    return {"status": "OK"}

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(fast_app, host="0.0.0.0", port=port)


# ── Telegram & Solana Monitor Setup ────────────────────────────────────────
BOT_TOKEN   = "7757376408:AAFn99qPZNSGtfRZsskOVvV4L_LoWJyYJx4"
USER_ID     = "7683338204"
RPC_WS      = "wss://api.mainnet-beta.solana.com/"
RPC_HTTP    = "https://api.mainnet-beta.solana.com/"

WALLETS     = [
    "dUJNHh9Nm9rsn7ykTViG7N7BJuaoJJD9H635B8BVifa",
    "9B1fR2Z38ggjqmFuhYBEsa7fXaBR1dkC7BamixjmWZb4"
]
THRESHOLD   = int(40 * 1e9)             # 40 SOL
SWAP_WINDOW = timedelta(minutes=15)     # 15-minute window

# ── Internal State ─────────────────────────────────────────────────────────
subs           = {}   # sub_id -> ("account", wallet) or ("logs", None)
balances       = {}   # wallet -> last lamport balance
pending_swaps  = {}   # tx_sig -> timestamp of SOL-outflow

# ── Utils ─────────────────────────────────────────────────────────────────
def now():
    return datetime.now(timezone.utc)

def ts():
    return now().isoformat()

def notify_telegram(msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": USER_ID, "text": msg},
            timeout=5
        )
        print(f"[{ts()}] Telegram → {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[{ts()}] 🔴 Telegram call failed:", e)


# ─── DEBUG STARTUP PING ────────────────────────────────────────────────────
print(f"[{ts()}] 🍏 Sending in-process startup test…")
try:
    notify_telegram("🚀 Monitor script loaded and notify_telegram() is here!")
except Exception as e:
    print(f"[{ts()}] 🔴 Startup ping failed:", e)


# ── Cleanup & False-Transfer Alert ─────────────────────────────────────────
async def cleanup_swaps():
    """
    For any pending swap older than SWAP_WINDOW:
    send a false-transfer alert, then remove it.
    """
    cutoff = now() - SWAP_WINDOW
    for sig, t in list(pending_swaps.items()):
        if t < cutoff:
            notify_telegram(
                f"❌ False transfer detected @ {ts()}\n"
                f"No token purchase seen for transaction:\n"
                f"https://solscan.io/tx/{sig}"
            )
            print(f"[{ts()}] False transfer: no token buy in {SWAP_WINDOW}")
            pending_swaps.pop(sig)

async def periodic_cleanup():
    """Run cleanup every minute to catch expired swaps."""
    while True:
        await cleanup_swaps()
        await asyncio.sleep(60)


# ── Improved Subscription Helpers ──────────────────────────────────────────
async def subscribe_accounts(ws):
    for i, w in enumerate(WALLETS, start=1):
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": i,
            "method": "accountSubscribe",
            "params": [w, {"encoding": "base64"}]
        }))
        # wait only for our subscribe-result
        while True:
            raw  = await ws.recv()
            resp = json.loads(raw)
            if resp.get("id") == i and "result" in resp:
                sub_id = resp["result"]
                subs[sub_id] = ("account", w)
                balances[w] = None
                break
    print(f"[{ts()}] ✅ Subscribed to {len(WALLETS)} accounts")

async def subscribe_logs(ws):
    req_id = len(WALLETS) + 1
    await ws.send(json.dumps({
        "jsonrpc": "2.0", "id": req_id,
        "method": "logsSubscribe",
        "params": [
            {"mentions": WALLETS},
            {"encoding": "jsonParsed"}
        ]
    }))
    while True:
        raw  = await ws.recv()
        resp = json.loads(raw)
        if resp.get("id") == req_id and "result" in resp:
            sub_id = resp["result"]
            subs[sub_id] = ("logs", None)
            break
    print(f"[{ts()}] ✅ Subscribed to program logs")


# ── Handlers ────────────────────────────────────────────────────────────────
async def handle_account(wallet, old, new):
    diff = new - old
    if diff < 0 and abs(diff) >= THRESHOLD:
        sol = abs(diff) / 1e9
        notify_telegram(
            f"🚨 {sol:.1f} SOL sent from {wallet} @ {ts()}\n"
            f"https://solscan.io/account/{wallet}"
        )
        print(f"[{ts()}] flagged: –{sol} SOL from {wallet}")

        # grab latest signature
        r = requests.post(RPC_HTTP, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [wallet, {"limit": 1}]
        }).json()
        sig = r["result"][0]["signature"]
        pending_swaps[sig] = now()

        # drop any expired entries
        await cleanup_swaps()

async def lookup_swap_tokens(sig):
    resp = requests.post(RPC_HTTP, json={
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [sig, "jsonParsed"]
    }).json()
    meta = resp.get("result", {}).get("meta", {})
    pre  = {p["accountIndex"]: p for p in meta.get("preTokenBalances", [])}
    for p in meta.get("postTokenBalances", []):
        idx    = p["accountIndex"]
        before = pre.get(idx, {"uiTokenAmount": {"uiAmount": 0}})["uiTokenAmount"]["uiAmount"]
        after  = p["uiTokenAmount"]["uiAmount"]
        if after > before:
            return p["mint"]
    return None

async def handle_logs(params):
    sig = params["result"]["value"]["signature"]
    if sig not in pending_swaps:
        return
    mint = await lookup_swap_tokens(sig)
    if mint:
        notify_telegram(
            f"🛒 Swap detected @ {ts()}\n"
            f"Token bought: {mint}\n"
            f"https://solscan.io/tx/{sig}"
        )
        print(f"[{ts()}] Swap: bought {mint} in {sig}")
        pending_swaps.pop(sig, None)


# ── Main WS Loop ──────────────────────────────────────────────────────────
async def listen():
    async with websockets.connect(RPC_WS, ping_interval=30) as ws:
        await subscribe_accounts(ws)
        await subscribe_logs(ws)

        async for raw in ws:
            msg    = json.loads(raw)
            sub_id = msg.get("params", {}).get("subscription")
            if sub_id not in subs:
                continue

            kind, wallet = subs[sub_id]
            if kind == "account" and msg["method"] == "accountNotification":
                lam = msg["params"]["result"]["value"]["lamports"]
                old = balances[wallet]
                if old is None:
                    balances[wallet] = lam
                else:
                    balances[wallet] = lam
                    await handle_account(wallet, old, lam)

            elif kind == "logs" and msg["method"] == "logsNotification":
                await handle_logs(msg["params"])


# ── Runner with Exponential Backoff ───────────────────────────────────────
async def run():
    # start periodic cleanup
    asyncio.create_task(periodic_cleanup())

    backoff = 1
    while True:
        try:
            print(f"[{ts()}] ▶️ Starting monitor…")
            await listen()
        except Exception as e:
            print(f"[{ts()}] ⚠️ Error {e!r}, retry in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        else:
            backoff = 1


if __name__ == "__main__":
    # final startup alert
    notify_telegram(f"🟢 Monitor starting at {ts()}")
    # start HTTP server thread for uptime checks
    threading.Thread(target=run_web_server, daemon=True).start()
    # run the Solana monitor
    asyncio.run(run())
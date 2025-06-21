import os
import json
import asyncio
import requests
import websockets
import threading
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
import uvicorn

# ─── FASTAPI UPTIME SERVER SETUP ─────────────────────────────────────────────
fast_app = FastAPI()

@fast_app.get("/")
async def root():
    return {"status": "OK"}

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(fast_app, host="0.0.0.0", port=port)


# ─── TELEGRAM & SOLANA CONFIGURATION ──────────────────────────────────────────
BOT_TOKEN   = "7757376408:AAFn99qPZNSGtfRZsskOVvV4L_LoWJyYJx4"
USER_ID     = "7683338204"
RPC_WS      = "wss://api.mainnet-beta.solana.com/"
RPC_HTTP    = "https://api.mainnet-beta.solana.com/"

# We monitor a list of wallets.
# The first wallet in this list is defined as the MAIN_WALLET.
WALLETS     = [
    "dUJNHh9Nm9rsn7ykTViG7N7BJuaoJJD9H635B8BVifa",  # Main wallet
    "9B1fR2Z38ggjqmFuhYBEsa7fXaBR1dkC7BamixjmWZb4"   # Example secondary wallet
]
MAIN_WALLET = WALLETS[0]

# Set the threshold for a significant transaction on any wallet (40 SOL or above)
THRESHOLD   = int(40 * 1e9)  # 40 SOL in lamports

# SWAP_WINDOW: how long the bot will wait for a token purchase event before clearing it.
# Increased to 2 hours - so even if funds sit idle for a long time, the purchase will be caught.
SWAP_WINDOW = timedelta(hours=2)


# ─── INTERNAL STATE ───────────────────────────────────────────────────────────
subs           = {}  # Mapping: sub_id -> ("account", wallet) or ("logs", None)
balances       = {}  # Mapping: wallet -> last known lamport balance
pending_swaps  = {}  # For non-main-wallet events (tracked by signature -> timestamp)


# ─── UTILITY FUNCTIONS ─────────────────────────────────────────────────────────
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
        print(f"[{ts()}] Error in notify_telegram: {e}")

# DEBUG: Immediately send a startup ping to test Telegram notifications.
print(f"[{ts()}] 🍏 Sending startup debug ping…")
notify_telegram("🚀 Monitor script loaded; notify_telegram() check.")


# ─── CLEANUP & FALSE-TRANSFER ALERT ─────────────────────────────────────────
async def cleanup_swaps():
    """
    For any pending swap older than SWAP_WINDOW, send a false-transfer alert,
    then remove it from pending_swaps.
    """
    cutoff = now() - SWAP_WINDOW
    for sig, t in list(pending_swaps.items()):
        if t < cutoff:
            notify_telegram(
                f"❌ False transfer detected @ {ts()}\n"
                f"No token purchase seen for transaction:\n"
                f"https://solscan.io/tx/{sig}"
            )
            print(f"[{ts()}] False transfer: transaction {sig} not matched in {SWAP_WINDOW}.")
            pending_swaps.pop(sig)

async def periodic_cleanup():
    while True:
        await cleanup_swaps()
        await asyncio.sleep(60)


# ─── SUBSCRIPTION HELPERS ─────────────────────────────────────────────────────
async def subscribe_accounts(ws):
    for i, w in enumerate(WALLETS, start=1):
        req = {
            "jsonrpc": "2.0", "id": i,
            "method": "accountSubscribe",
            "params": [w, {"encoding": "base64"}]
        }
        await ws.send(json.dumps(req))
        # Wait until the subscription confirmation is received.
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
    req = {
        "jsonrpc": "2.0", "id": req_id,
        "method": "logsSubscribe",
        "params": [
            {"mentions": WALLETS},
            {"encoding": "jsonParsed"}
        ]
    }
    await ws.send(json.dumps(req))
    while True:
        raw  = await ws.recv()
        resp = json.loads(raw)
        if resp.get("id") == req_id and "result" in resp:
            sub_id = resp["result"]
            subs[sub_id] = ("logs", None)
            break
    print(f"[{ts()}] ✅ Subscribed to program logs")


# ─── CHAIN-TRACING FUNCTION ───────────────────────────────────────────────────
async def trace_transfer_chain(tx_sig, chain, current_depth=0, max_depth=4):
    try:
        resp = requests.post(
            RPC_HTTP,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [tx_sig, "jsonParsed"]
            }
        ).json()
        result = resp.get("result")
        if not result:
            print(f"[{ts()}] No transaction details for {tx_sig}")
            return chain

        message = result.get("transaction", {}).get("message", {})
        instructions = message.get("instructions", [])
        new_destinations = []
        # Look for system transfer instructions that include a destination.
        for instr in instructions:
            if "parsed" in instr and instr.get("program") == "system":
                parsed = instr["parsed"]
                if parsed.get("type") == "transfer":
                    info = parsed.get("info", {})
                    dest = info.get("destination")
                    if dest:
                        new_destinations.append(dest)
        if not new_destinations:
            print(f"[{ts()}] No destinations in tx {tx_sig}. Chain so far: {chain}")
            return chain

        for dest in new_destinations:
            new_chain = chain + [dest]
            print(f"[{ts()}] Chain updated: {' -> '.join(new_chain)}")
            # If the funds return to the main wallet, alert that no token was bought.
            if dest == MAIN_WALLET:
                notify_telegram(f"Chain returned to main wallet. No token purchased. Chain: {' -> '.join(new_chain)}")
                return new_chain
            # Check the destination's latest transaction.
            sig_resp = requests.post(
                RPC_HTTP,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [dest, {"limit": 1}]
                }
            ).json()
            sigs = sig_resp.get("result", [])
            if sigs:
                next_sig = sigs[0].get("signature")
                # Check if this transaction demonstrates a token purchase.
                token = await lookup_swap_tokens(next_sig)
                if token:
                    notify_telegram(f"Token purchase detected! Token mint: {token}. Chain: {' -> '.join(new_chain)}")
                    return new_chain
                # Recursively continue the chain if we haven’t reached max depth.
                if current_depth < max_depth:
                    sub_chain = await trace_transfer_chain(next_sig, new_chain, current_depth + 1, max_depth)
                    if sub_chain:
                        return sub_chain
        return chain
    except Exception as e:
        print(f"[{ts()}] Error in trace_transfer_chain: {e}")
        return chain


# ─── HELPER TO CHECK FOR TOKEN PURCHASE IN A TRANSACTION ─────────────────────
async def lookup_swap_tokens(tx_sig):
    try:
        resp = requests.post(
            RPC_HTTP,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [tx_sig, "jsonParsed"]
            }
        ).json()
        meta = resp.get("result", {}).get("meta", {})
        pre = {p["accountIndex"]: p for p in meta.get("preTokenBalances", [])}
        for p in meta.get("postTokenBalances", []):
            idx = p["accountIndex"]
            before = pre.get(idx, {"uiTokenAmount": {"uiAmount": 0}})["uiTokenAmount"]["uiAmount"]
            after = p["uiTokenAmount"]["uiAmount"]
            if after > before:
                return p["mint"]
        return None
    except Exception as e:
        print(f"[{ts()}] Error in lookup_swap_tokens: {e}")
        return None


# ─── HANDLER FOR ACCOUNT NOTIFICATIONS ─────────────────────────────────────────
async def handle_account(wallet, old, new):
    diff = new - old
    print(f"[{ts()}] DEBUG: Wallet {wallet}, old: {old}, new: {new}, diff: {diff}")
    if diff < 0 and abs(diff) >= THRESHOLD:
        sol = abs(diff) / 1e9
        notify_telegram(
            f"🚨 {sol:.1f} SOL sent from {wallet} @ {ts()}\nhttps://solscan.io/account/{wallet}"
        )
        print(f"[{ts()}] ALERT: Detected transfer of –{sol} SOL from {wallet}")
        try:
            response = requests.post(
                RPC_HTTP,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [wallet, {"limit": 1}]
                }
            ).json()
            print(f"[{ts()}] getSignaturesForAddress Response: {response}")
            sig = response["result"][0]["signature"]
        except Exception as e:
            print(f"[{ts()}] Error fetching signature: {e}")
            return

        # If this is the MAIN_WALLET, only trigger chain tracing if this transaction shows a significant outflow.
        if wallet == MAIN_WALLET:
            chain_result = await trace_transfer_chain(sig, [MAIN_WALLET])
            if chain_result and chain_result[-1] == MAIN_WALLET:
                notify_telegram(f"Chain returned to main wallet. No token purchased. Chain: {' -> '.join(chain_result)}")
        else:
            # For non-main wallets, fallback to pending swap logic.
            pending_swaps[sig] = now()
            await cleanup_swaps()


# ─── HANDLER FOR LOGS NOTIFICATIONS ───────────────────────────────────────────
async def handle_logs(params):
    try:
        sig = params["result"]["value"]["signature"]
        print(f"[{ts()}] DEBUG: Received logs for sig: {sig}")
        if sig not in pending_swaps:
            print(f"[{ts()}] DEBUG: Signature {sig} not in pending_swaps.")
            return
        token = await lookup_swap_tokens(sig)
        if token:
            notify_telegram(
                f"🛒 Swap detected @ {ts()}\nToken bought: {token}\nhttps://solscan.io/tx/{sig}"
            )
            print(f"[{ts()}] ALERT: Swap detected, token: {token}, sig: {sig}")
            pending_swaps.pop(sig, None)
    except Exception as e:
        print(f"[{ts()}] Error in handle_logs: {e}")


# ─── MAIN WEBSOCKET LISTENER LOOP ─────────────────────────────────────────────
async def listen():
    async with websockets.connect(RPC_WS, ping_interval=30) as ws:
        await subscribe_accounts(ws)
        await subscribe_logs(ws)
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception as e:
                print(f"[{ts()}] Error parsing message: {e}")
                continue

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


# ─── MAIN RUNNER WITH EXPONENTIAL BACKOFF ─────────────────────────────────────
async def run():
    # Start periodic cleanup task.
    asyncio.create_task(periodic_cleanup())
    backoff = 1
    while True:
        try:
            print(f"[{ts()}] ▶️ Starting monitor...")
            await listen()
        except Exception as e:
            print(f"[{ts()}] ⚠️ Error {e!r}, retry in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        else:
            backoff = 1

if __name__ == "__main__":
    notify_telegram(f"🟢 Monitor starting at {ts()}")
    threading.Thread(target=run_web_server, daemon=True).start()
    asyncio.run(run())

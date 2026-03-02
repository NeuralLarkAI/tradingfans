"""
telegram_remote.py — Telegram alerts + remote control.

Enable:
  - TELEGRAM_BOT_TOKEN=123:ABC...

Optional hardening:
  - TELEGRAM_PAIR_PIN=some-secret (requires /pair <pin>)

Pairing:
  - If no chat is authorized yet, send /pair (or /pair <pin>) from your Telegram.
  - The bot stores the authorized chat_id locally (gitignored) so it persists across restarts.

Conservative by design:
  - Only bounded param edits through the tuner.
  - Pause/resume supported.
  - Emits status into STATE.remote for the dashboard.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import aiohttp

from .state import STATE
from .tuner import SPECS, persist_current, set_param


def _api_base(token: str) -> str:
    return f"https://api.telegram.org/bot{token}"


def _log_remote_event(kind: str, detail: str, *, chat_id: int | None = None) -> None:
    try:
        STATE.remote_events.appendleft({
            "ts_epoch": time.time(),
            "kind": kind,
            "detail": detail[:200],
            "chat_id": int(chat_id) if isinstance(chat_id, int) else None,
        })
    except Exception:
        pass


async def _tg_call(session: aiohttp.ClientSession, token: str, method: str, payload: dict) -> Any:
    url = f"{_api_base(token)}/{method}"
    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=25)) as r:
        data = await r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {data}")
        return data["result"]


async def _send(session: aiohttp.ClientSession, token: str, chat_id: int, text: str) -> None:
    await _tg_call(session, token, "sendMessage", {"chat_id": chat_id, "text": text})


def _parse_cmd(text: str) -> tuple[str, list[str]]:
    t = (text or "").strip()
    if not t.startswith("/"):
        return "", []
    parts = t.split()
    cmd = parts[0].lstrip("/").lower()
    return cmd, parts[1:]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _config_path() -> Path:
    return _project_root() / "config" / "remote.json"


def _load_cfg() -> dict:
    try:
        p = _config_path()
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cfg(cfg: dict) -> None:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def _status_text() -> str:
    btc = STATE.btc
    eth = STATE.eth
    btc_s = "OK" if (btc and btc.fresh) else "STALE"
    eth_s = "OK" if (eth and eth.fresh) else "STALE"

    p = STATE.tuner_params or {}
    return (
        "TradingFans\n"
        f"mode={'DRY' if STATE.dry_run else 'LIVE'} paused={STATE.paused}\n"
        f"scans={STATE.scan_count} trades={STATE.trade_count}\n"
        f"spot: BTC={btc_s} ETH={eth_s}\n"
        f"min_edge={STATE.min_edge:.3f} max_size={STATE.max_size:.2f} poll={STATE.poll_interval:.1f}s\n"
        f"min_order={STATE.min_order_size:.2f} max_tte={STATE.max_time_to_expiry:.0f}s\n"
        f"tuner={'ON' if STATE.tuner_enabled else 'OFF'} interval={p.get('tuner.interval_sec', 30):.0f}s"
    )


async def telegram_loop() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    pair_pin = os.environ.get("TELEGRAM_PAIR_PIN", "").strip()

    cfg = _load_cfg()
    authorized_chat_id = cfg.get("telegram_chat_id")

    remote = STATE.remote.setdefault("telegram", {})
    remote.update({
        "enabled": bool(token),
        "status": "OFF" if not token else ("PAIRING" if not isinstance(authorized_chat_id, int) else "ON"),
        "allowed_chat_id": int(authorized_chat_id) if isinstance(authorized_chat_id, int) else None,
        "last_seen_epoch": None,
        "last_error": "",
    })

    if not token:
        return

    last_update_id: int | None = None
    last_trade_sent: float = 0.0
    last_tuner_sent: float = 0.0

    async with aiohttp.ClientSession() as session:
        if remote.get("allowed_chat_id") is not None:
            await _send(session, token, int(remote["allowed_chat_id"]), "TradingFans bot connected. Send /help")
            _log_remote_event("startup", "connected", chat_id=int(remote["allowed_chat_id"]))

        while True:
            try:
                payload: dict[str, Any] = {"timeout": 25}
                if last_update_id is not None:
                    payload["offset"] = last_update_id + 1
                updates = await _tg_call(session, token, "getUpdates", payload)

                # Alerts: executed trades + tuner events (only once paired)
                now = time.time()
                if remote.get("allowed_chat_id") is not None and now - last_trade_sent >= 2.0:
                    for s in list(STATE.recent_signals)[:80]:
                        if not s.order_id:
                            continue
                        if s.ts_epoch <= last_trade_sent:
                            continue
                        if STATE.dry_run and s.order_id != "dry-run":
                            continue
                        msg = (
                            f"TRADE {s.signal} {s.symbol}\n"
                            f"size=${s.size_usdc:.2f} edge={s.edge*100:+.2f}% impl={s.implied_yes*100:.1f}%\n"
                            f"{s.question[:120]}"
                        )
                        await _send(session, token, int(remote["allowed_chat_id"]), msg)
                        last_trade_sent = s.ts_epoch
                        break

                if remote.get("allowed_chat_id") is not None and now - last_tuner_sent >= 2.0:
                    for ev in list(STATE.tuner_events)[:20]:
                        ts = float(ev.get("ts_epoch", 0.0))
                        if ts <= last_tuner_sent:
                            continue
                        msg = f"TUNER {ev.get('key')}: {ev.get('old')} -> {ev.get('new')} ({ev.get('reason')})"
                        await _send(session, token, int(remote["allowed_chat_id"]), msg)
                        last_tuner_sent = ts
                        break

                # Commands
                for u in updates:
                    last_update_id = int(u["update_id"])
                    msg = u.get("message") or {}
                    incoming_chat_id = int(msg.get("chat", {}).get("id", 0))
                    text = msg.get("text", "")
                    cmd, args = _parse_cmd(text)
                    remote["last_seen_epoch"] = time.time()

                    # Pairing mode
                    if remote.get("allowed_chat_id") is None:
                        remote["status"] = "PAIRING"
                        if cmd == "pair":
                            if pair_pin and (not args or args[0] != pair_pin):
                                await _send(session, token, incoming_chat_id, "Pair PIN required. Use /pair <pin>")
                                _log_remote_event("pair", "pin_required", chat_id=incoming_chat_id)
                                continue
                            cfg["telegram_chat_id"] = incoming_chat_id
                            _save_cfg(cfg)
                            remote["allowed_chat_id"] = incoming_chat_id
                            remote["status"] = "ON"
                            await _send(session, token, incoming_chat_id, "Paired. You are now authorized. Send /help")
                            _log_remote_event("pair", "ok", chat_id=incoming_chat_id)
                        else:
                            await _send(
                                session, token, incoming_chat_id,
                                "TradingFans bot is not paired yet.\n"
                                "Send /pair to authorize this chat."
                                + (" (PIN required)" if pair_pin else "")
                            )
                        continue

                    if incoming_chat_id != int(remote["allowed_chat_id"]):
                        continue

                    if cmd in ("start", "help"):
                        keys = ", ".join(sorted(SPECS.keys()))
                        await _send(
                            session, token, int(remote["allowed_chat_id"]),
                            "Commands:\n"
                            "/ping\n"
                            "/status\n"
                            "/pause | /resume\n"
                            "/tuner on|off\n"
                            "/set <key> <value>\n"
                            f"keys: {keys}"
                        )
                        _log_remote_event("cmd", "help", chat_id=incoming_chat_id)
                    elif cmd == "ping":
                        await _send(session, token, int(remote["allowed_chat_id"]), "pong")
                        _log_remote_event("cmd", "ping", chat_id=incoming_chat_id)
                    elif cmd == "status":
                        await _send(session, token, int(remote["allowed_chat_id"]), _status_text())
                        _log_remote_event("cmd", "status", chat_id=incoming_chat_id)
                    elif cmd == "pause":
                        STATE.paused = True
                        await _send(session, token, int(remote["allowed_chat_id"]), "Paused trading loop.")
                        _log_remote_event("cmd", "pause", chat_id=incoming_chat_id)
                    elif cmd in ("resume", "unpause"):
                        STATE.paused = False
                        await _send(session, token, int(remote["allowed_chat_id"]), "Resumed trading loop.")
                        _log_remote_event("cmd", "resume", chat_id=incoming_chat_id)
                    elif cmd == "tuner" and args:
                        val = args[0].lower()
                        STATE.tuner_enabled = val in ("1", "on", "true", "yes")
                        await _send(session, token, int(remote["allowed_chat_id"]), f"Tuner {'ON' if STATE.tuner_enabled else 'OFF'}.")
                        _log_remote_event("cmd", f"tuner {val}", chat_id=incoming_chat_id)
                    elif cmd == "set" and len(args) >= 2:
                        key = args[0]
                        try:
                            value = float(args[1])
                        except ValueError:
                            await _send(session, token, int(remote["allowed_chat_id"]), "Value must be numeric.")
                            continue
                        ok, old, new, msg_txt = set_param(key, value, source="telegram")
                        if ok:
                            persist_current()
                            await _send(session, token, int(remote["allowed_chat_id"]), f"OK {key}: {old} -> {new}")
                            _log_remote_event("cmd", f"set {key} {new}", chat_id=incoming_chat_id)
                        else:
                            await _send(session, token, int(remote["allowed_chat_id"]), f"ERR {msg_txt}")
                            _log_remote_event("cmd", f"set_fail {key} ({msg_txt})", chat_id=incoming_chat_id)
                    else:
                        if cmd:
                            await _send(session, token, int(remote["allowed_chat_id"]), "Unknown command. Send /help")
                            _log_remote_event("cmd", f"unknown {cmd}", chat_id=incoming_chat_id)
            except Exception as exc:
                remote["status"] = "ERR"
                remote["last_error"] = str(exc)[:200]
                await _sleep(2.0)
                remote["status"] = "ON" if remote.get("allowed_chat_id") is not None else "PAIRING"


async def _sleep(sec: float) -> None:
    import asyncio

    await asyncio.sleep(max(0.25, float(sec)))

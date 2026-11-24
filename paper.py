#!/usr/bin/env python3
"""
Portefeuille FICTIF : applique automatiquement les alertes avec de l'argent virtuel.

- À chaque signal d'achat, "achète" virtuellement (taille = risque 1 % du portefeuille),
  puis revend au stop (-2,5 %) ou à l'objectif (+5 %). Frais Coinbase Advanced UE déduits
  (0,50 % à l'achat, 0,50 % à la vente, ordres au marché).
- Prévient sur Telegram à chaque trade fictif clôturé.
- Envoie un bilan quotidien vers 21 h (heure de Paris).
- L'état est gardé dans paper_state.json (sauvegardé dans le dépôt par GitHub).

AUCUN ORDRE RÉEL N'EST PASSÉ. Tout est simulé.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import alertes_crypto as A

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_state.json")
START_CAPITAL = float(os.environ.get("PAPER_CAPITAL", "109") or 109)
FEE_PCT = 0.50           # % par ordre (taker Coinbase Advanced UE)
REPORT_HOUR = 21         # heure de Paris du bilan quotidien
PARIS = ZoneInfo("Europe/Paris")
URL = "https://api.exchange.coinbase.com/products/{p}/candles?granularity=3600"


def fetch_candles(product):
    """Bougies horaires TERMINÉES [t, low, high, open, close], de la plus ancienne à la plus récente."""
    req = urllib.request.Request(URL.format(p=product), headers={"User-Agent": "paper/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    now = time.time()
    rows = sorted((c for c in data if c[0] + 3600 <= now), key=lambda c: c[0])
    return [(int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])) for c in rows]


def ema_series(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out[period - 1] = e
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def new_state(now_paris):
    return {
        "start_capital": START_CAPITAL,
        "cash": START_CAPITAL,
        "start_date": now_paris.date().isoformat(),
        "positions": {},          # sym -> {entry, qty, cost, stop, target, opened}
        "last_candle": {},        # sym -> timestamp de la dernière bougie traitée
        "closed": [],             # historique des trades clôturés
        "last_report": "",
        "last_prices": {},
    }


def load_state(now_paris):
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return new_state(now_paris)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)


def equity(state):
    val = state["cash"]
    for sym, p in state["positions"].items():
        price = state["last_prices"].get(sym, p["entry"])
        val += p["qty"] * price * (1 - FEE_PCT / 100)
    return val


def close_position(state, sym, price, reason, ts, messages):
    p = state["positions"].pop(sym)
    proceeds = p["qty"] * price * (1 - FEE_PCT / 100)
    state["cash"] += proceeds
    pnl = proceeds - p["cost"]
    state["closed"].append({
        "sym": sym, "entry": p["entry"], "exit": price, "pnl": round(pnl, 2),
        "pct": round(pnl / p["cost"] * 100, 2), "reason": reason,
        "opened": p["opened"], "closed": ts,
    })
    icon = "✅" if pnl > 0 else "❌"
    messages.append(
        f"{icon} Trade FICTIF clôturé — {sym} ({reason})\n"
        f"Achat {A.fmt(p['entry'])} → vente {A.fmt(price)}\n"
        f"Résultat : {pnl:+.2f} € ({pnl / p['cost'] * 100:+.1f} %, frais inclus)\n"
        f"Portefeuille fictif : {equity(state):.2f} €"
    )


def open_position(state, sym, price, ts):
    eq = equity(state)
    risk_eur = eq * A.RISK_PCT / 100
    size = min(risk_eur / (A.STOP_PCT / 100), state["cash"])
    if size < 5:  # pas assez de liquidités fictives disponibles
        return False
    qty = size * (1 - FEE_PCT / 100) / price
    state["cash"] -= size
    state["positions"][sym] = {
        "entry": price, "qty": qty, "cost": size,
        "stop": price * (1 - A.STOP_PCT / 100), "target": price * (1 + A.TP_PCT / 100),
        "opened": ts,
    }
    return True


def process_symbol(state, sym, candles, messages):
    closes = [c[4] for c in candles]
    rsi = A.compute_rsi(closes, A.RSI_PERIOD)
    ema = ema_series(closes, A.EMA_TREND)
    last = state["last_candle"].get(sym, candles[-2][0] if len(candles) > 1 else 0)
    for i, (t, lo, hi, _o, close) in enumerate(candles):
        if t <= last:
            continue
        # 1) sortie d'une position ouverte (stop prioritaire si les deux sont touchés : prudent)
        p = state["positions"].get(sym)
        if p and t > p["opened"]:
            if lo <= p["stop"]:
                close_position(state, sym, p["stop"], "stop", t, messages)
            elif hi >= p["target"]:
                close_position(state, sym, p["target"], "objectif", t, messages)
        # 2) entrée sur signal (mêmes règles que les alertes)
        if sym not in state["positions"] and i > 0 and rsi[i] is not None and rsi[i - 1] is not None \
                and ema[i] is not None and rsi[i - 1] < A.RSI_LOW <= rsi[i] and close > ema[i]:
            open_position(state, sym, close, t)
        last = t
    state["last_candle"][sym] = last
    state["last_prices"][sym] = closes[-1]


def daily_report(state, now_paris):
    today = now_paris.date().isoformat()
    day_n = (now_paris.date() - datetime.fromisoformat(state["start_date"]).date()).days
    eq = equity(state)
    start = state["start_capital"]
    day_start = datetime(now_paris.year, now_paris.month, now_paris.day, tzinfo=PARIS).timestamp()
    today_trades = [c for c in state["closed"] if c["closed"] >= day_start]
    wins = sum(1 for c in state["closed"] if c["pnl"] > 0)
    lines = [
        f"📊 Jour {day_n} — portefeuille FICTIF",
        f"Valeur : {eq:.2f} € ({(eq / start - 1) * 100:+.1f} % depuis le départ à {start:.0f} €)",
        f"Trades clôturés aujourd'hui : {len(today_trades)}"
        + (" (" + ", ".join(f"{c['sym']} {c['pnl']:+.2f} €" for c in today_trades) + ")" if today_trades else ""),
        f"Total : {len(state['closed'])} trades, {wins} gagnants",
    ]
    if state["positions"]:
        pos = []
        for sym, p in state["positions"].items():
            price = state["last_prices"].get(sym, p["entry"])
            pos.append(f"{sym} {(price / p['entry'] - 1) * 100:+.1f} %")
        lines.append("En cours : " + ", ".join(pos))
    else:
        lines.append("Aucune position en cours.")
    lines.append("Simulation, argent fictif, frais inclus.")
    state["last_report"] = today
    return "\n".join(lines)


def main():
    now_paris = datetime.now(timezone.utc).astimezone(PARIS)
    first_run = not os.path.exists(STATE_FILE)
    state = load_state(now_paris)
    messages = []
    for sym in A.CRYPTOS:
        try:
            candles = fetch_candles(f"{sym}-{A.QUOTE}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"{sym}: erreur ({e})")
            continue
        if len(candles) < A.EMA_TREND + 2:
            continue
        process_symbol(state, sym, candles, messages)
        time.sleep(0.3)
    if first_run:
        messages.append(
            f"🧪 Portefeuille FICTIF lancé avec {state['start_capital']:.0f} € virtuels.\n"
            f"Il suivra tes alertes tout seul, frais inclus. Bilan chaque soir vers {REPORT_HOUR} h."
        )
    if now_paris.hour >= REPORT_HOUR and state["last_report"] != now_paris.date().isoformat():
        messages.append(daily_report(state, now_paris))
    save_state(state)
    for m in messages:
        print(m.replace("\n", " | "))
        A.send_telegram(m)
    print(f"Portefeuille fictif : {equity(state):.2f} € | liquidités {state['cash']:.2f} € | "
          f"positions : {', '.join(state['positions']) or 'aucune'}")


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    main()

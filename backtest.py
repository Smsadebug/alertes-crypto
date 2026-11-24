#!/usr/bin/env python3
"""
Backtest de la stratégie d'alertes sur l'historique Coinbase (bougies 1 h, paires EUR).

Simule : entrée au prix de clôture de la bougie qui donne le signal, sortie au stop
ou à l'objectif (si les deux sont touchés dans la même bougie, on compte le stop :
hypothèse prudente). Une seule position à la fois par crypto. Frais déduits.
Ce n'est qu'une simulation sur le passé : ça ne garantit rien pour la suite.
"""

import json
import sys
import time
import urllib.error
import urllib.request

import alertes_crypto as A

DAYS = 365
FEE_ROUNDTRIP = 1.0      # % : achat au marché (0,50 %) + vente au stop (0,50 %), Coinbase Advanced UE
URL = "https://api.exchange.coinbase.com/products/{p}/candles?granularity=3600&start={s}&end={e}"


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def fetch_history(product, days=DAYS):
    """Bougies horaires [t, low, high, open, close] triées, sur `days` jours."""
    end = int(time.time()) // 3600 * 3600
    start = end - days * 86400
    out = {}
    t = start
    while t < end:
        t2 = min(t + 300 * 3600, end)
        for attempt in range(4):
            try:
                req = urllib.request.Request(URL.format(p=product, s=iso(t), e=iso(t2)),
                                             headers={"User-Agent": "backtest/1.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode())
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return []
                time.sleep(2 + attempt * 2)
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(2 + attempt * 2)
        else:
            data = []
        for c in data:
            out[c[0]] = (c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]))
        t = t2
        time.sleep(0.15)
    return [out[k] for k in sorted(out)]


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


def simulate(candles, rsi_low=30, stop=2.5, target=5.0, trend=True, fee=FEE_ROUNDTRIP):
    closes = [c[4] for c in candles]
    rsi = A.compute_rsi(closes, 14)
    ema = ema_series(closes, 200)
    trades = []
    i = 1
    n = len(candles)
    while i < n - 1:
        if rsi[i] is None or rsi[i - 1] is None or ema[i] is None:
            i += 1
            continue
        cross = rsi[i - 1] < rsi_low <= rsi[i]
        if cross and (not trend or closes[i] > ema[i]):
            entry = closes[i]
            sl, tp = entry * (1 - stop / 100), entry * (1 + target / 100)
            result = None
            j = i + 1
            while j < n:
                lo, hi = candles[j][1], candles[j][2]
                if lo <= sl:
                    result = -stop
                    break
                if hi >= tp:
                    result = target
                    break
                j += 1
            if result is None:  # position encore ouverte à la fin : clôture au dernier prix
                result = (closes[-1] / entry - 1) * 100
            trades.append(result - fee)
            i = j + 1
            continue
        i += 1
    return trades


def summary(trades):
    if not trades:
        return 0, 0.0, 0.0, 0.0
    wins = sum(1 for t in trades if t > 0)
    eq, peak, dd = 1.0, 1.0, 0.0
    for t in trades:  # mise fixe de 100 % de la position, résultats composés
        eq *= 1 + t / 100
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
    return len(trades), wins / len(trades) * 100, (eq - 1) * 100, dd * 100


VARIANTS = [
    ("Actuelle : RSI 30, tendance, -2.5/+5", dict(rsi_low=30, stop=2.5, target=5.0, trend=True)),
    ("Sans filtre de tendance", dict(rsi_low=30, stop=2.5, target=5.0, trend=False)),
    ("RSI 35 (plus d'alertes)", dict(rsi_low=35, stop=2.5, target=5.0, trend=True)),
    ("Stop/objectif larges -4/+8", dict(rsi_low=30, stop=4.0, target=8.0, trend=True)),
    ("Stop/objectif larges -5/+10", dict(rsi_low=30, stop=5.0, target=10.0, trend=True)),
    ("Actuelle, frais maker 0.5 %", dict(rsi_low=30, stop=2.5, target=5.0, trend=True, fee=0.5)),
    ("Actuelle, SANS frais (référence)", dict(rsi_low=30, stop=2.5, target=5.0, trend=True, fee=0.0)),
]


def main():
    data = {}
    for sym in A.CRYPTOS:
        p = f"{sym}-EUR"
        c = fetch_history(p)
        if len(c) < 500:
            print(f"{p}: pas assez de données ({len(c)} bougies), ignoré")
            continue
        data[sym] = c
        hold = (c[-1][4] / c[0][4] - 1) * 100
        print(f"{p}: {len(c)} bougies, du {iso(c[0][0])[:10]} au {iso(c[-1][0])[:10]}, achat-conservation {hold:+.1f} %")
    print()
    print(f"Frais par aller-retour par défaut : {FEE_ROUNDTRIP} % (Coinbase Advanced UE, ordres au marché)")
    print()
    print(f"{'Variante':38s} {'Trades':>6s} {'Gagnants':>9s} {'Moy/trade':>10s} {'Total composé':>14s}")
    for name, kw in VARIANTS:
        all_trades = []
        per = []
        for sym, c in data.items():
            t = simulate(c, **kw)
            all_trades += t
            n, wr, tot, dd = summary(t)
            per.append(f"{sym}:{n}/{tot:+.0f}%")
        n, wr, tot, dd = summary(all_trades)
        avg = sum(all_trades) / n if n else 0.0
        print(f"{name:38s} {n:6d} {wr:8.0f}% {avg:+9.2f}% {tot:+13.1f}%")
        print("    par crypto (trades/total) : " + "  ".join(per))
    print()
    print("Lecture : 'Moy/trade' = gain ou perte moyen par trade, frais déduits.")
    print("'Total composé' = résultat si chaque trade engageait la même part du capital, l'un après l'autre (toutes cryptos mélangées).")


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    main()

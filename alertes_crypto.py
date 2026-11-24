#!/usr/bin/env python3
"""
Alertes crypto -> Telegram, à partir des prix publics de Coinbase (paires en EUR).

Ce script NE PASSE AUCUN ORDRE. Il t'envoie une notification quand tes règles
se déclenchent ; c'est toi qui décides d'acheter ou non sur Coinbase Advanced.
Ce n'est pas un conseil financier : les signaux peuvent être faux, teste-les
d'abord sans argent réel.

Règles (modifiables dans CONFIG) :
  - Bougies d'1 heure.
  - ALERTE ACHAT : le RSI(14) repasse au-dessus de 30 après être passé dessous
    (rebond après une baisse), ET le prix est au-dessus de sa moyenne mobile
    exponentielle 200 (tendance de fond haussière).
  - ALERTE SORTIE : le RSI repasse sous 70 après être passé au-dessus
    (essoufflement après une hausse). Utile seulement si tu détiens la crypto.
  - Chaque alerte d'achat donne : prix d'entrée, stop-loss (-2,5 %),
    objectif (+5 %), et la taille de position pour ne risquer que 1 % du capital.

Lancement :
  python3 alertes_crypto.py           -> tourne en continu (vérifie toutes les 5 min)
  python3 alertes_crypto.py --once    -> une seule vérification puis s'arrête
  python3 alertes_crypto.py --test    -> envoie un message test sur Telegram
  python3 alertes_crypto.py --status  -> affiche RSI et tendance de chaque crypto

Uniquement la bibliothèque standard Python 3 : rien à installer.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ============================== CONFIG ==============================

# Telegram et capital : remplis automatiquement par "1_CONFIGURER.bat" (fichier config.json).
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Les grosses cryptos à surveiller (celles sans paire EUR sur Coinbase sont ignorées).
CRYPTOS = ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "AVAX", "DOT", "LTC"]
QUOTE = "EUR"

GRANULARITY = 3600      # taille des bougies en secondes (3600 = 1 heure)
RSI_PERIOD = 14
RSI_LOW = 30            # monte à 35 pour avoir plus de signaux (mais moins fiables)
RSI_HIGH = 70
EMA_TREND = 200         # filtre de tendance de fond

STOP_PCT = 2.5          # stop-loss en % sous le prix d'entrée
TP_PCT = 5.0            # objectif en % au-dessus (2x le risque)

CAPITAL_EUR = 100.0     # ton capital total sur Coinbase, en euros
RISK_PCT = 1.0          # % du capital que tu acceptes de perdre sur un trade
if os.environ.get("CAPITAL_EUR"):
    CAPITAL_EUR = float(os.environ["CAPITAL_EUR"].replace(",", "."))

CHECK_EVERY_SEC = 300   # fréquence de vérification (5 minutes)
COOLDOWN_HOURS = 6      # pas deux alertes du même type pour la même crypto dans ce délai

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "alertes_state.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

COINBASE_URL = "https://api.exchange.coinbase.com/products/{product}/candles?granularity={g}"
USER_AGENT = "alertes-crypto/1.0"

# ============================ INDICATEURS ============================


def compute_rsi(closes, period=RSI_PERIOD):
    """RSI de Wilder. Renvoie une liste de même longueur que closes (None au début)."""
    if len(closes) < period + 1:
        return [None] * len(closes)
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / period, losses / period

    def to_rsi(g, l):
        return 100.0 if l == 0 else 100.0 - 100.0 / (1.0 + g / l)

    out = [None] * period + [to_rsi(avg_gain, avg_loss)]
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
        out.append(to_rsi(avg_gain, avg_loss))
    return out


def compute_ema(values, period=EMA_TREND):
    """Dernière valeur de la moyenne mobile exponentielle, ou None si pas assez de données."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


# ============================== RÉSEAU ==============================


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def fetch_closes(product):
    """Clôtures des bougies TERMINÉES, de la plus ancienne à la plus récente."""
    data = http_get_json(COINBASE_URL.format(product=product, g=GRANULARITY))
    # Format Coinbase : [time, low, high, open, close, volume], plus récent en premier.
    now = time.time()
    candles = sorted(data, key=lambda c: c[0])
    candles = [c for c in candles if c[0] + GRANULARITY <= now]  # retire la bougie en cours
    return [float(c[4]) for c in candles]


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram non configuré] " + text.replace("\n", " | "))
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except urllib.error.URLError as e:
        print(f"Erreur Telegram : {e}")
        return False


# ============================== LOGIQUE ==============================


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fmt(price):
    if price >= 100:
        return f"{price:,.2f} €".replace(",", " ")
    if price >= 1:
        return f"{price:.3f} €"
    return f"{price:.5f} €"


def analyse(closes):
    """Renvoie (signal, infos). signal ∈ {'ACHAT', 'SORTIE', None}."""
    rsi = compute_rsi(closes)
    ema = compute_ema(closes)
    if len(rsi) < 2 or rsi[-1] is None or rsi[-2] is None or ema is None:
        return None, None
    price = closes[-1]
    info = {"price": price, "rsi": rsi[-1], "ema": ema, "uptrend": price > ema}
    if rsi[-2] < RSI_LOW <= rsi[-1] and price > ema:
        return "ACHAT", info
    if rsi[-2] > RSI_HIGH >= rsi[-1]:
        return "SORTIE", info
    return None, info


def build_message(symbol, signal, info):
    p = info["price"]
    if signal == "ACHAT":
        stop = p * (1 - STOP_PCT / 100)
        tp = p * (1 + TP_PCT / 100)
        risk_eur = CAPITAL_EUR * RISK_PCT / 100
        size = min(risk_eur / (STOP_PCT / 100), CAPITAL_EUR)
        return (
            f"🟢 ALERTE ACHAT — {symbol}/{QUOTE}\n"
            f"Prix : {fmt(p)}  |  RSI : {info['rsi']:.1f}  |  tendance de fond haussière\n"
            f"Stop-loss : {fmt(stop)} (-{STOP_PCT} %)\n"
            f"Objectif : {fmt(tp)} (+{TP_PCT} %)\n"
            f"Taille max : {size:.2f} € (risque {risk_eur:.2f} € = {RISK_PCT} % du capital)\n"
            f"Si tu entres : pose le stop et l'objectif tout de suite sur Coinbase Advanced. "
            f"Frais non inclus. Signal à vérifier, pas un conseil."
        )
    return (
        f"🟠 ALERTE SORTIE — {symbol}/{QUOTE}\n"
        f"Prix : {fmt(p)}  |  RSI : {info['rsi']:.1f} (repasse sous {RSI_HIGH})\n"
        f"La hausse s'essouffle. Si tu détiens {symbol}, envisage de sécuriser tes gains "
        f"ou de remonter ton stop. Sinon, ignore."
    )


def check_all(state, verbose=False, send=True):
    now = time.time()
    for sym in CRYPTOS:
        product = f"{sym}-{QUOTE}"
        try:
            closes = fetch_closes(product)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                if verbose:
                    print(f"{product} : pas de paire sur Coinbase, ignoré")
                continue
            print(f"{product} : erreur HTTP {e.code}")
            continue
        except (urllib.error.URLError, ValueError, OSError) as e:
            print(f"{product} : erreur réseau ({e})")
            continue

        signal, info = analyse(closes)
        if verbose and info:
            trend = "haussière" if info["uptrend"] else "baissière"
            print(f"{product:10s} prix {fmt(info['price']):>14s}  RSI {info['rsi']:5.1f}  tendance {trend}")
        if not signal or not send:
            continue

        key = f"{product}:{signal}"
        if now - state.get(key, 0) < COOLDOWN_HOURS * 3600:
            continue
        msg = build_message(sym, signal, info)
        print(f"[{datetime.now():%H:%M}] {msg.splitlines()[0]}")
        send_telegram(msg)
        state[key] = now
        save_state(state)
        time.sleep(0.3)  # reste poli avec l'API
    return state


def load_config():
    global TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, CAPITAL_EUR
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return False
    TELEGRAM_TOKEN = TELEGRAM_TOKEN or cfg.get("telegram_token", "")
    TELEGRAM_CHAT_ID = TELEGRAM_CHAT_ID or str(cfg.get("telegram_chat_id", ""))
    CAPITAL_EUR = float(cfg.get("capital_eur", CAPITAL_EUR))
    return True


def find_chat_id(token):
    """Récupère le numéro de chat du dernier message envoyé au bot."""
    data = http_get_json(f"https://api.telegram.org/bot{token}/getUpdates")
    if not data.get("ok"):
        return None
    for upd in reversed(data.get("result", [])):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat", {})
        if "id" in chat:
            return str(chat["id"])
    return None


def setup():
    print("=" * 60)
    print(" CONFIGURATION DES ALERTES CRYPTO")
    print("=" * 60)
    print("\nÉtape 1 : sur Telegram, ouvre une conversation avec @BotFather,")
    print("envoie /newbot, choisis un nom, et copie le TOKEN qu'il te donne")
    print("(ça ressemble à 123456789:ABCdef...).\n")
    while True:
        token = input("Colle ton token ici puis Entrée : ").strip()
        me = None
        try:
            me = http_get_json(f"https://api.telegram.org/bot{token}/getMe")
        except (urllib.error.URLError, ValueError, OSError):
            pass
        if me and me.get("ok"):
            bot_name = me["result"].get("username")
            print(f"OK, bot trouvé : @{bot_name}")
            break
        print("Token invalide, réessaie.")
    print(f"\nÉtape 2 : dans Telegram, ouvre @{bot_name}, appuie sur DÉMARRER")
    print("et envoie-lui n'importe quel message (par exemple : salut).")
    chat_id = None
    while not chat_id:
        input("Quand c'est fait, appuie sur Entrée... ")
        try:
            chat_id = find_chat_id(token)
        except (urllib.error.URLError, ValueError, OSError):
            chat_id = None
        if not chat_id:
            print("Je ne vois pas encore ton message. Envoie-le au bot puis réessaie.")
    print("OK, chat trouvé.")
    while True:
        cap = input("\nÉtape 3 : combien d'euros comptes-tu mettre sur Coinbase ? (ex : 100) ").strip()
        try:
            capital = float(cap.replace(",", ".").replace("€", "").strip())
            if capital > 0:
                break
        except ValueError:
            pass
        print("Entre juste un nombre.")
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"telegram_token": token, "telegram_chat_id": chat_id, "capital_eur": capital}, f, indent=2)
    global TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, CAPITAL_EUR
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, CAPITAL_EUR = token, chat_id, capital
    ok = send_telegram("✅ Les alertes crypto sont bien branchées. Tu recevras les signaux ici.")
    print("\nMessage de test envoyé sur Telegram !" if ok else "\nLe message de test n'est pas passé.")
    print("Configuration terminée. Lance maintenant 2_LANCER_ALERTES.bat")


def main():
    args = sys.argv[1:]
    if "--setup" in args:
        setup()
        return
    load_config()
    if "--test" in args:
        ok = send_telegram("✅ Test : les alertes crypto sont bien branchées.")
        print("Message envoyé." if ok else "Échec : vérifie TELEGRAM_TOKEN et TELEGRAM_CHAT_ID.")
        return
    state = load_state()
    if "--status" in args:
        check_all({}, verbose=True, send=False)
        return
    if "--once" in args:
        check_all(state)
        return
    if not TELEGRAM_TOKEN:
        print("Telegram n'est pas configuré : les alertes s'afficheront seulement dans cette fenêtre.")
        print("Lance 1_CONFIGURER.bat pour les recevoir sur ton téléphone.\n")
    print(f"Surveillance de {len(CRYPTOS)} cryptos toutes les {CHECK_EVERY_SEC // 60} min, capital {CAPITAL_EUR:.0f} €.")
    print("Laisse cette fenêtre ouverte. Ferme-la (ou Ctrl+C) pour arrêter.\n")
    check_all({}, verbose=True, send=False)
    print()
    while True:
        state = check_all(state)
        time.sleep(CHECK_EVERY_SEC)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # accents et emojis dans la console Windows
        except (AttributeError, ValueError):
            pass
    try:
        main()
    except KeyboardInterrupt:
        print("\nArrêté.")

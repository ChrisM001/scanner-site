"""
Push-Alarme fuer den Scanner: schickt eine Telegram-Nachricht, wenn ein NEUER
Eintrag (Coin/Aktie) in der Liste auftaucht. Serverlos (laeuft im GitHub-Action).

Neuerkennung per State-File (zurueckcommittet): ein Symbol gilt als "neu", wenn es
seit > ALERT_COOLDOWN_H Stunden nicht in der Liste war. So feuert ein dauerhaft
gelisteter Coin nicht bei jedem Lauf, aber ein Wieder-Eintritt nach Pause schon.
Erster Lauf (kein State) seedet still -> kein Initial-Flut-Alarm.

Secrets (GitHub) / Env:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  -- ohne diese: no-op (kein Alarm)
  ALERT_COOLDOWN_H (default 6)          -- Wiedereintritts-Fenster in Stunden
"""
import os, json, time, html
import requests

DIR = os.path.dirname(os.path.abspath(__file__))
COOLDOWN_H = float(os.getenv("ALERT_COOLDOWN_H", "6"))
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")


def enabled():
    return bool(TG_TOKEN and TG_CHAT)


def tg_send(text):
    if not enabled():
        print("[alert] TELEGRAM_BOT_TOKEN/CHAT_ID fehlt -> Alarm uebersprungen")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"},
            timeout=15)
        if r.status_code != 200:
            print(f"[alert] telegram HTTP {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        print(f"[alert] telegram error: {e}")
        return False


def new_entries(kind, symbols):
    """Set-basierte Neuerkennung -> gibt (neue_symbole, changed) zurueck.

    'neu' = im aktuellen Set, war NICHT im zuletzt gespeicherten Set, und kein
    Flicker-Wiedereintritt < COOLDOWN_H. Das ist commit-frequenz-unabhaengig:
    der State wird NUR geschrieben, wenn sich das Set aendert (changed=True) ->
    der Workflow committet auch nur dann. State: {"set":[...], "left":{sym:epoch}}.
    Alt-/fehlendes Format -> stiller Seed (kein Alarm)."""
    path = os.path.join(DIR, f"alert_state_{kind}.json")
    now = time.time(); cd = COOLDOWN_H * 3600
    try:
        state = json.load(open(path))
    except Exception:
        state = {}
    cur = list(dict.fromkeys(str(s) for s in symbols))   # de-dup, Reihenfolge halten
    curset = set(cur)

    def _save(obj):
        try:
            json.dump(obj, open(path, "w"))
        except Exception as e:
            print(f"[alert] state save failed: {e}")

    if "set" not in state:                 # Erstlauf / Legacy-Format -> still seeden
        _save({"set": sorted(curset), "left": {}})
        return [], True

    prev = set(state.get("set", []))
    left = {s: float(t) for s, t in (state.get("left") or {}).items()}
    new = [s for s in cur if s not in prev and (now - left.get(s, 0) > cd)]
    changed = curset != prev
    if changed:
        for s in (prev - curset):          # gerade rausgefallene Coins merken (Flicker-Sperre)
            left[s] = now
        left = {s: t for s, t in left.items() if s not in curset and now - t <= cd}
        _save({"set": sorted(curset), "left": left})
    return new, changed

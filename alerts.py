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
  ALERT_PCT_THRESH (default 3)          -- pp-Bewegung eines Werts, die einen
                                           Seiten-Refresh (Commit) erzwingt
"""
import os, json, time, html
import requests

PCT_THRESH = float(os.getenv("ALERT_PCT_THRESH", "3"))

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


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def diff(kind, items):
    """Set- UND wert-basierte Auswertung -> (neue_symbole, commit).

    items: Iterable von (symbol, pct). 'neu' = im aktuellen Set, war NICHT im
    zuletzt gespeicherten Set, kein Flicker-Wiedereintritt < COOLDOWN_H (-> Alarm).
    commit=True, wenn sich das SET aendert ODER ein angezeigter Wert um
    >= PCT_THRESH pp gewandert ist (damit die Seite live-nah bleibt, ohne in
    ruhigen Phasen zu committen). State: {"set":[...],"left":{sym:epoch},"vals":{sym:pct}}.
    Alt-/fehlendes Format -> stiller Seed bzw. einmalige Migration (kein Alarm)."""
    path = os.path.join(DIR, f"alert_state_{kind}.json")
    now = time.time(); cd = COOLDOWN_H * 3600

    cur, seen = [], set()
    for s, p in items:                     # de-dup je Symbol, Reihenfolge halten
        s = str(s)
        if s not in seen:
            seen.add(s); cur.append((s, _f(p)))
    curset = {s for s, _ in cur}
    curvals = {s: p for s, p in cur}

    def _save():
        try:
            json.dump({"set": sorted(curset), "left": left, "vals": curvals}, open(path, "w"))
        except Exception as e:
            print(f"[alert] state save failed: {e}")

    try:
        state = json.load(open(path))
    except Exception:
        state = {}

    left = {s: float(t) for s, t in (state.get("left") or {}).items()}
    if "set" not in state:                 # Erstlauf -> still seeden
        _save();  return [], True
    migrate = "vals" not in state          # Format ohne vals -> einmalig committen, um vals zu fuellen

    prev = set(state.get("set", []))
    pv = state.get("vals") or {}
    new = [s for s, _ in cur if s not in prev and (now - left.get(s, 0) > cd)]
    set_changed = curset != prev
    vals_drift = any(
        pv.get(s) is not None and curvals.get(s) is not None
        and abs(curvals[s] - pv[s]) >= PCT_THRESH
        for s in curset
    )
    commit = set_changed or vals_drift or migrate
    if commit:
        for s in (prev - curset):          # gerade rausgefallene Coins merken (Flicker-Sperre)
            left[s] = now
        left = {s: t for s, t in left.items() if s not in curset and now - t <= cd}
        _save()
    return new, commit

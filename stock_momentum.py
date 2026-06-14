"""
Small-Cap-Momentum (Warrior-Trading / Ross-Cameron-Guide) -- Scanner + ehrlicher
Backtest des EINEN mechanisierbaren Setups: der Bull Flag.

Der Guide ist eine DISKRETIONAERE US-Small-Cap-Momentum-Strategie. Ihr Edge sitzt
zu ~80% in der AUSWAHL (Echtzeit-Scanner: Gap%, niedriger Float, hohes Relative
Volume, Preis $1-20, News-Katalysator), nicht im Entry. Diesen Scanner bilden wir
hier 1:1 ab (TradingView-Screener via TVRemix). Den mechanischen Teil -- den
Bull-Flag-Continuation-Entry -- testen wir ehrlich mit Kosten.

ZWEI Modi:
  scan      -- Live-Watchlist der "stocks in play" (die Figur-11/12-Scanner des Guides),
               huebsch formatiert + je Name die juengste NEWS/Schlagzeile als Katalysator,
               mit Alter (FRISCH<=2d vs alt/squeeze>7d) und grobem Typ-Tag (FDA/M&A/OFFERING..).
               Schreibt zusaetzlich stock_scan.html (sortier-/filterbar) + loggt stock_scan_log.csv.
               Env: DISPLAY_N (Anzahl mit News, default 25), NEWS=0 schaltet News ab.
  (default) -- Backtest: Bull-Flag-Entry intraday auf den gescannten Namen

Bull Flag (mechanisiert, exakt nach Guide S.16-18):
  - Kontext: Preis ueber STEIGENDER 9-EMA, Tageshoch (HOD) kuerzlich gemacht (Pole).
  - Pullback: 1..pb_max rote/schwache Kerzen, halten ueber 9-EMA, Retrace <= 50%.
  - Entry:  erste gruene Kerze, die das HOCH der vorherigen roten Kerze bricht
            (Buy-Stop am Vorkerzenhoch).
  - Stop:   Tief des Pullbacks (Swing-Tief).
  - Target: 2R (2:1 CRV) ODER neues Tageshoch; Exit spaetestens EOD.

EHRLICHKEITS-WARNUNGEN (bewusst zugunsten der Strategie verzerrt):
  (1) SURVIVORSHIP: Universe = HEUTE gelistete, HEUTE volatile Namen. Delistete
      Pennystock-Leichen fehlen -> schmeichelt der Long-Strategie.
  (2) SELECTION: wir testen das Muster auf Namen, die bereits als Mover bekannt sind.
  (3) FENSTER: TVRemix liefert ~5000 5m-Bars (~60 Handelstage) -> nur jüngstes Regime.
  (4) FILLS: Buy-Stop am Vorkerzenhoch unterstellt Fill ohne Slippage; Kosten-Ladder
      modelliert Spread/Slippage separat (Small-Caps: real 30-100 bps RT).
  -> Faellt schon dieser GUENSTIG verzerrte Test durch, ist die Sache klar. "Funktioniert"
     er, verbietet die Verzerrung Vertrauen -> nur Forward/Paper wuerde es beweisen.

  INTERVAL=5m (default) | scan:  /c/Python313/python.exe stock_momentum.py scan

Forward-Track (Windows Task Scheduler, taeglich -- loggt das Universe point-in-time):
  $a=New-ScheduledTaskAction -Execute "C:\\Python313\\python.exe" `
       -Argument '"C:\\Users\\chmue\\trading_ai\\stock_momentum.py" scan' `
       -WorkingDirectory "C:\\Users\\chmue\\trading_ai"
  Register-ScheduledTask -TaskName StockScan -Action $a `
       -Trigger (New-ScheduledTaskTrigger -Daily -At 09:10) `
       -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable) -Force
"""
import os, sys, time, warnings; warnings.filterwarnings("ignore")
import json
import datetime
import numpy as np
import pandas as pd
import requests

TVREMIX_API_KEY = os.getenv("TVREMIX_API_KEY")  # kein Hardcode -- lokal Env-Var (setx), Cloud GitHub-Secret
TVREMIX_URL = "https://tvremix.xyz/api/mcp/v1"  # Repo darf so oeffentlich sein (kein Schluessel im Quellcode)
INTERVAL = os.getenv("INTERVAL", "5m")
N_NAMES  = int(os.getenv("N_NAMES", "50"))      # Universe-Groesse fuer den Backtest
BARS     = int(os.getenv("BARS", "5000"))       # ~60 Handelstage 5m
DISPLAY_N = int(os.getenv("DISPLAY_N", "25"))   # Anzahl Namen im Scan-Output (mit News)
SHOW_NEWS = os.getenv("NEWS", "1") == "1"        # News/Katalysator je Name anzeigen
LOG_NEWS_ALL = os.getenv("LOG_NEWS_ALL", "0") == "1"   # auch News der NICHT gezeigten Namen loggen
CACHE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_data")
SCANLOG  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_scan_log.csv")
SCANHTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_scan.html")
COSTS    = (0.0, 0.0010, 0.0030, 0.0050)        # 0 / 10 / 30 / 50 bps RT (Spread+Slippage)

# Ross Camerons ECHTE "5 Kriterien" (Warrior Trading -- per Web-Recherche bestaetigt,
# u.a. woertliches Ross-Zitat; ersetzt meine frueheren, zu lockeren Annahmen):
# Offizielle Warrior-Kriterien (Quelle: "Warrior Trading - Stock Selection" S.3 +
# "SAC2024-Strategy" S.1, beide im Repo):
#   1) >= +10% am Tag   2) RVOL >= 5x (Ross: 30-Tage-Basis!)   3) News-Katalysator
#   4) Preis $1.00-$20.00   5) Float < 20 Mio bevorzugt ("lower is better")
# ABWEICHUNGEN ggü. Quelle (bewusst, nicht aus dem PDF):
#   - relative_volume_10d_calc ist TVs 10-Tage-RVOL; Ross definiert 30 Tage
#     (TV-Screener bietet kein 30d-RVOL-Feld -> bestes verfuegbares Proxy).
#   - exchange NASDAQ/NYSE/AMEX ist MEIN Filter (steht in keinem Dokument).
#     (Frueherer absoluter Vol>=500k-Floor entfernt: untested + in Spannung mit
#      Ross' Low-Float-Logik -- low-float-Raketen bewegen sich auf modestem
#      Absolutvolumen; RVOL>=5x deckt die Liquiditaets-Relevanz bereits ab.)
#   - Sample-Trading-Plan engt fuer das Small-Account-Challenge auf $5-10 ein
#     ("sweet spot") -> optional strenger, hier nicht hart gesetzt.
# in_range ist inklusiv -> [10, ...] = >=10.
SCAN_FILTERS = [
    {"left": "change", "operation": "in_range", "right": [10, 100000]},               # >= +10% Tag
    {"left": "relative_volume_10d_calc", "operation": "in_range", "right": [5, 1_000_000]},  # >= 5x (10d-Proxy)
    {"left": "close", "operation": "in_range", "right": [1, 20]},                      # $1.00-$20.00 (Quelle)
    {"left": "float_shares_outstanding", "operation": "in_range", "right": [0, 20_000_000]}, # < 20M (Quelle)
    {"left": "exchange", "operation": "in_range", "right": ["NASDAQ", "NYSE", "AMEX"]},  # eigener Filter
]
SCAN_COLS = ["name", "exchange", "close", "change", "volume",
             "relative_volume_10d_calc", "float_shares_outstanding", "market_cap_basic"]
SCAN_SORT = "relative_volume_10d_calc"   # regulaere Session: nach RVOL sortieren

# --- VORBOERSE (vor 09:30 ET) --------------------------------------------------
# change/close/relative_volume_10d_calc sind Felder der REGULAEREN Session und rollen
# vorboerslich NICHT auf den neuen Tag -> sie zeigen den gestrigen Schluss (=> morgens
# identische Liste wie gestern). Vorboerslich daher auf die Premarket-Felder ausweichen:
# gap = heutiger Vorboersen-Gap (Ross' Gap-Scanner), premarket_volume = echte Vorboersen-
# Liquiditaet. Es gibt vorboerslich KEIN Live-RVOL-Feld.
# PROVENANZ: Gap>=10% = Ross' Gap-Schwelle (Quelle). PM-Vol-Floor = MEIN Liquiditaets-
# proxy (steht in keinem Ross-Dokument), per Env (PM_VOL_MIN/GAP_MIN) justierbar.
GAP_MIN    = float(os.getenv("GAP_MIN", "10"))          # >= +10% Vorboersen-Gap
PM_VOL_MIN = float(os.getenv("PM_VOL_MIN", "100000"))   # Vorboersen-Mindestvolumen (Stueck)
PM_SORT    = "gap"
PM_FILTERS = [
    {"left": "gap", "operation": "in_range", "right": [GAP_MIN, 100000]},                 # heutiger Gap
    {"left": "premarket_volume", "operation": "in_range", "right": [PM_VOL_MIN, 1e13]},    # PM-Liquiditaet
    {"left": "close", "operation": "in_range", "right": [1, 20]},                          # Preisband
    {"left": "float_shares_outstanding", "operation": "in_range", "right": [0, 20_000_000]},
    {"left": "exchange", "operation": "in_range", "right": ["NASDAQ", "NYSE", "AMEX"]},
]
PM_COLS = ["name", "exchange", "close", "change", "gap", "premarket_change",
           "premarket_volume", "premarket_close", "relative_volume_10d_calc",
           "float_shares_outstanding", "volume", "market_cap_basic"]


def _premarket_now():
    """True, wenn jetzt Mo-Fr VOR 09:30 ET ist -> change/rvol sind noch von gestern,
    also auf die Premarket-Felder (gap/premarket_volume) ausweichen. FORCE_SESSION=
    premarket|regular ueberschreibt (zum Testen)."""
    forced = os.getenv("FORCE_SESSION", "").lower()
    if forced in ("premarket", "regular"):
        return forced == "premarket"
    n = pd.Timestamp.now(tz="America/New_York")
    return n.weekday() < 5 and n.time() < datetime.time(9, 30)


def _mcp_call(tool, arguments, retries=3, backoff=1.5, timeout=60):
    """POST an TVRemix mit Retry/Backoff bei 429 (Rate-Limit) und Netzfehlern.
    timeout: HTTP-Timeout pro Versuch (Sekunden) -- klein halten, wenn schnelle
    Best-effort-Calls gewuenscht sind (z.B. Krypto-News bei vielen Coins)."""
    if not TVREMIX_API_KEY:
        raise RuntimeError("TVREMIX_API_KEY nicht gesetzt -- als Umgebungsvariable (lokal, setx) "
                           "bzw. GitHub-Secret (Cloud) hinterlegen.")
    last = None
    for i in range(retries):
        try:
            resp = requests.post(
                TVREMIX_URL,
                headers={"Authorization": f"Bearer {TVREMIX_API_KEY}",
                         "Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "method": "tools/call",
                      "params": {"name": tool, "arguments": arguments}, "id": 1},
                timeout=timeout)
            if resp.status_code == 429:
                last = "429 Too Many Requests"
                time.sleep(backoff * (i + 1)); continue
            resp.raise_for_status()
            return resp.json()["result"]["structuredContent"]
        except requests.exceptions.RequestException as ex:
            last = ex
            time.sleep(backoff * (i + 1))
    raise RuntimeError(f"TVRemix '{tool}' nach {retries} Versuchen fehlgeschlagen: {last}")


def scan(limit=N_NAMES):
    """Live-Watchlist: stocks in play nach Warrior-Kriterien. Regulaer nach RVOL;
    vorboerslich (vor 09:30 ET) nach heutigem Gap -- weil change/rvol vorboerslich
    noch den GESTRIGEN Schluss zeigen (sonst: morgens dieselbe Liste wie gestern)."""
    pm = _premarket_now()
    res = _mcp_call("run_screener", {
        "market": "america", "sort_by": (PM_SORT if pm else SCAN_SORT),
        "sort_order": "desc", "limit": limit,
        "columns": (PM_COLS if pm else SCAN_COLS),
        "filters": (PM_FILTERS if pm else SCAN_FILTERS)}, retries=5, backoff=2.0)
    rows = res["data"]["results"]
    df = pd.DataFrame(rows)
    if pm and len(df):
        # Premarket-Felder in die Anzeige-Spalten mappen, damit Print/HTML/Log unveraendert
        # laufen: CHG% -> heutiger Gap, VOL -> Premarket-Volumen, Price -> Premarket-Kurs.
        # RVOL bleibt der (vorboerslich nur gestrige) 10T-Wert -> im Header/Meta als
        # "Vortag" gekennzeichnet, NICHT als Live-RVOL ausgegeben.
        df["change"] = df["gap"]
        df["volume"] = df["premarket_volume"].fillna(0)
        df["close"]  = df["premarket_close"].fillna(df["close"])
    df.attrs["premarket"] = pm
    return df, res["data"].get("totalCount")


# Katalysator-Heuristik aus der Schlagzeile (grober Hinweis, warum der Name laeuft)
CATALYST_MAP = [
    ("OFFERING", ("offering", "registered direct", "priced", "public offering", "warrant",
                  "shelf", " atm ", "dilut", "capital raise", "private placement")),
    ("FDA/CLIN", ("fda", "phase ", "clinical", "trial", "approval", "designation",
                  "topline", "endpoint", " nda", " ind ", "breakthrough")),
    ("EARNINGS", ("earnings", "quarter", "results", "revenue", "guidance", "beats", "misses")),
    ("M&A",      ("merger", "acquir", "buyout", "takeover", "to be acquired", "combination", "stake")),
    ("DEAL",     ("contract", "agreement", "partnership", "secures", "awarded", "order ",
                  "collaboration", "selected", "launch")),
    ("LEGAL",    ("lawsuit", "investigation", "settlement", "court", "patent", " sec ")),
    ("ANALYST",  ("upgrade", "downgrade", "price target", "initiates", "rating")),
]


def classify_catalyst(title):
    t = " " + title.lower() + " "
    for tag, kws in CATALYST_MAP:
        if any(k in t for k in kws):
            return tag
    return ""


def _ascii(s):
    return (s or "").encode("ascii", "ignore").decode().strip()


def news_summary(symbol, limit=4):
    """Neueste relevante Schlagzeile -> (tag, alter_str, alt_tage, 'Provider: Title') oder None."""
    try:
        data = _mcp_call("get_news", {"symbol": symbol, "limit": limit}, retries=2)
    except Exception:
        return "ERR"                                # Abruf gedrosselt/fehlgeschlagen (!= keine News)
    d = data or {}
    heads = d.get("headlines")                     # je nach Tool-Wrapping flach ...
    if heads is None and isinstance(d.get("data"), dict):
        heads = d["data"].get("headlines")         # ... oder unter data{} (wie run_screener)
    heads = heads or []
    if not heads:
        return None
    h = heads[0]                                   # neueste zuerst
    title = _ascii(h.get("title", ""))
    if not title:
        return None
    prov = _ascii(h.get("provider", ""))
    age_d = None
    try:
        pub = pd.to_datetime(h.get("published"), utc=True)
        age_d = (pd.Timestamp.now(tz="UTC") - pub).days
        age = f"{age_d}d" if age_d >= 1 else "heute"
    except Exception:
        age = "?"
    tag = classify_catalyst(title)
    return tag, age, age_d, f"{prov}: {title}" if prov else title


def _news_fetch(symbol, state):
    """News holen mit Circuit-Breaker: nach mehreren 429-Fehlern in Folge keine
    weiteren Abrufe mehr (haelt den Scan auch unter Rate-Limit schnell + ehrlich)."""
    if not SHOW_NEWS:
        return None
    if state.get("off"):
        return "ERR"
    ns = news_summary(symbol)
    if ns == "ERR":
        state["streak"] = state.get("streak", 0) + 1
        if state["streak"] >= 4:
            state["off"] = True            # genug -- Rest wird nicht mehr abgefragt
    else:
        state["streak"] = 0
        time.sleep(0.30)                   # nur bei echtem Treffer takten
    return ns


_HTML_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:22px;background:#0f1115;color:#e6e9ef}
h1{font-size:20px;margin:0 0 2px} .meta{color:#9aa4b2;font-size:12.5px;margin:0 0 14px}
#q{width:340px;max-width:60%;padding:7px 10px;margin-bottom:12px;border:1px solid #2a2f3a;
   border-radius:8px;background:#171a21;color:#e6e9ef;font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid #232733;text-align:right;white-space:nowrap}
th{position:sticky;top:0;background:#171a21;color:#cbd3e1;cursor:pointer;user-select:none}
th:hover{color:#fff} td.sym,th.sym{text-align:left} td.num{font-variant-numeric:tabular-nums}
td.news,th.news{text-align:left;white-space:normal;color:#c4ccd8;max-width:620px}
tr:hover td{background:#161a22} a{color:#6ea8fe;text-decoration:none} a:hover{text-decoration:underline}
.up{color:#46d369;font-weight:600} .down{color:#ff6b6b;font-weight:600}
.badge{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:600;margin-right:6px}
.fresh{background:#10391f;color:#5ee08a;border:1px solid #1f7a44}
.old{background:#3a2a12;color:#e0b15e;border:1px solid #7a571f}
.tag{background:#1c2740;color:#88a7e6;border:1px solid #2c3f66}
.mut{color:#7a8493} .foot{color:#7a8493;font-size:12px;margin-top:14px;max-width:900px;line-height:1.5}
.sortbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 12px}
.sortbar .lbl{color:#7a8493;font-size:12px;margin-right:1px}
.sortbar button{background:#171a21;border:1px solid #2a2f3a;color:#cbd3e1;border-radius:8px;
 padding:6px 11px;font-size:13px;cursor:pointer}
.sortbar button:active{background:#243044}
@media(max-width:640px){
 body{margin:11px}h1{font-size:18px}.meta{font-size:11.5px}#q{max-width:100%;width:100%}
 thead{display:none} table,tbody{display:block;width:100%} table{font-size:13px}
 tr{display:block;border-bottom:1px solid #232733;padding:9px 2px 10px}
 td{display:inline-block;border:none;padding:0;text-align:left;white-space:nowrap}
 td:first-child{display:none}
 td.sym{display:block;font-size:15px;font-weight:650;margin:0 0 4px}
 td.num{margin-right:13px}
 td[data-label]::before{content:attr(data-label)" ";color:#6b7280;font-size:10px;text-transform:uppercase;letter-spacing:.04em}
 td.news{display:block;margin-top:6px;color:#7a8493;font-size:12px;white-space:normal;max-width:none}
}
"""

_HTML_JS = """
function filt(){var q=document.getElementById('q').value.toLowerCase();
 document.querySelectorAll('#t tbody tr').forEach(function(r){
  r.style.display=r.innerText.toLowerCase().indexOf(q)>=0?'':'none';});}
var ss={};
function srt(c,dd){var tb=document.querySelector('#t tbody');
 var rows=[].slice.call(tb.querySelectorAll('tr'));
 var asc=(c in ss)?!ss[c]:(dd?false:true);ss={};ss[c]=asc;
 rows.sort(function(a,b){var x=a.children[c].getAttribute('data-val'),y=b.children[c].getAttribute('data-val');
  var nx=parseFloat(x),ny=parseFloat(y);
  if(!isNaN(nx)&&!isNaN(ny))return asc?nx-ny:ny-nx;
  x=a.children[c].innerText.toLowerCase();y=b.children[c].innerText.toLowerCase();
  return asc?(x<y?-1:x>y?1:0):(x>y?-1:x<y?1:0);});
 rows.forEach(function(r){tb.appendChild(r);});}
"""


def scan_html(df, news_map, total, path=SCANHTML, premarket=False):
    """Schreibt den Scan als eigenstaendige, sortier-/filterbare HTML-Datei."""
    import html as _h
    now = pd.Timestamp.now(tz="America/New_York")
    body = []
    for i, (_, r) in enumerate(df.iterrows(), 1):
        sym = r["symbol"]; close = r["close"]; chg = r["change"]
        rvol = r["relative_volume_10d_calc"]; fl = r["float_shares_outstanding"]; vol = r["volume"]
        fl_disp = f"{fl/1e6:.1f}M" if pd.notna(fl) else "?"
        fl_val = fl / 1e6 if pd.notna(fl) else -1
        rvol_disp = f"{rvol:,.0f}x" if pd.notna(rvol) else "—"
        rvol_val = rvol if pd.notna(rvol) else -1
        ns = (news_map or {}).get(sym)
        news_html = '<span class="mut">keine News (kein frischer Grund / Squeeze)</span>'; age_val = 99999
        if ns == "ERR":
            news_html = '<span class="mut">News-Abruf gedrosselt (Rate-Limit)</span>'; age_val = 99998
        elif isinstance(ns, tuple):
            tag, age, age_d, txt = ns
            age_val = age_d if age_d is not None else 99999
            b = ""
            if age_d is not None and age_d <= 2:
                b += '<span class="badge fresh">FRISCH</span>'
            elif (age_d or 0) > 7:
                b += '<span class="badge old">alt/squeeze</span>'
            if tag:
                b += f'<span class="badge tag">{_h.escape(tag)}</span>'
            news_html = f'{b}<span class="mut">{_h.escape(age)}</span> {_h.escape(txt)}'
        tv = f"https://www.tradingview.com/chart/?symbol={_h.escape(sym)}"
        body.append(
            f'<tr><td class="num" data-val="{i}">{i}</td>'
            f'<td class="sym"><a href="{tv}" target="_blank">{_h.escape(sym)}</a></td>'
            f'<td class="num" data-val="{close}">${close:.2f}</td>'
            f'<td class="num {"up" if chg>=0 else "down"}" data-val="{chg}">{chg:+.1f}%</td>'
            f'<td class="num" data-label="RVOL" data-val="{rvol_val}">{rvol_disp}</td>'
            f'<td class="num" data-label="Float" data-val="{fl_val}">{fl_disp}</td>'
            f'<td class="num" data-label="Vol" data-val="{vol}">{vol/1e6:.1f}M</td>'
            f'<td class="news" data-val="{age_val}">{news_html}</td></tr>')
    if premarket:
        head1 = 'Warrior-Scanner &middot; Gap-Scanner (Vorb&ouml;rse)'
        crit = (f'VORB&Ouml;RSE &middot; Gap&ge;+{GAP_MIN:.0f}%, PM-Vol&ge;{PM_VOL_MIN/1e3:.0f}k, '
                '$1&ndash;20, Float&lt;20M, NASDAQ/NYSE/AMEX &middot; '
                'CHG%=heutiger Gap, VOL=Premarket-Vol, RVOL=Vortag')
    else:
        head1 = 'Warrior-Scanner &middot; stocks in play'
        crit = 'Ross-Kriterien: &ge;+10% Tag, RVOL&ge;5x, $1&ndash;20, Float&lt;20M, NASDAQ/NYSE/AMEX'
    doc = (
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Warrior-Scanner {now:%Y-%m-%d}</title><style>{_HTML_CSS}</style></head><body>'
        f'<h1>{head1}</h1>'
        f'<div class="meta">{now:%Y-%m-%d %H:%M} ET &nbsp;|&nbsp; {crit} '
        f'&nbsp;|&nbsp; {total} Treffer, {len(df)} gezeigt</div>'
        '<input id="q" placeholder="filtern (Symbol oder Schlagzeile) ..." oninput="filt()">'
        '<div class="sortbar"><span class="lbl">Sortieren:</span>'
        '<button onclick="srt(4,1)">RVOL</button><button onclick="srt(3,1)">Chg%</button>'
        '<button onclick="srt(5,1)">Float</button><button onclick="srt(6,1)">Vol</button>'
        '<button onclick="srt(7)">News&nbsp;frisch</button><button onclick="srt(1)">Symbol</button></div>'
        '<table id="t"><thead><tr>'
        '<th onclick="srt(0)">#</th><th class="sym" onclick="srt(1)">Symbol</th>'
        '<th onclick="srt(2)">Price</th><th onclick="srt(3)">Chg %</th>'
        '<th onclick="srt(4)">RVOL</th><th onclick="srt(5)">Float</th>'
        '<th onclick="srt(6)">Vol</th><th class="news" onclick="srt(7)">Katalysator / News</th>'
        f'</tr></thead><tbody>{"".join(body)}</tbody></table>'
        '<p class="foot">FRISCH (&le;2 Tage) = echter News-Katalysator. '
        '&bdquo;alt/squeeze&ldquo; oder keine News bei grossem Move = eher technischer Squeeze. '
        'Tag (M&amp;A/FDA/&hellip;) ist nur ein Hinweis aus der Schlagzeile. '
        'Klick auf Spaltenkopf = sortieren; Klick auf Symbol = TradingView-Chart.</p>'
        f'<script>{_HTML_JS}</script></body></html>')
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path


def log_scan(df, news_map=None, mode="regular"):
    """Haengt die heutige Watchlist (POINT-IN-TIME) an stock_scan_log.csv, dedup je date+symbol.
    Zweck: prospektiver, survivorship-freier Universe-Record. Spaeter laesst sich der
    Pullback+Pole-Edge auf genau diesen -- VOR Ausgang notierten -- Namen backtesten.
    news_map (symbol -> news_summary-Tupel) loggt zusaetzlich den Katalysator je Name."""
    if df is None or len(df) == 0:
        return 0
    now = pd.Timestamp.now(tz="UTC")
    fl = df["float_shares_outstanding"]
    nm = news_map or {}
    n_age, n_tag, n_head = [], [], []
    for sym in df["symbol"].values:
        ns = nm.get(sym)
        if isinstance(ns, tuple):
            tag, _age, age_d, txt = ns
            n_age.append(age_d if age_d is not None else "")
            n_tag.append(tag); n_head.append(txt)
        elif ns == "ERR":
            n_age.append(""); n_tag.append(""); n_head.append("(News-Abruf gedrosselt)")
        else:
            n_age.append(""); n_tag.append(""); n_head.append("")
    out = pd.DataFrame({
        "date": now.strftime("%Y-%m-%d"), "ts": now.strftime("%Y-%m-%d %H:%M"),
        "mode": mode,
        "symbol": df["symbol"].values, "close": df["close"].round(3).values,
        "change": df["change"].round(2).values,
        "rvol": df["relative_volume_10d_calc"].round(1).values,
        "float_m": (fl / 1e6).round(2).values, "vol_m": (df["volume"] / 1e6).round(2).values,
        "news_age_d": n_age, "news_tag": n_tag, "news_headline": n_head,
    })
    if os.path.exists(SCANLOG):
        out = pd.concat([pd.read_csv(SCANLOG), out], ignore_index=True)
        out = out.drop_duplicates(subset=["date", "symbol"], keep="last")
    out = out.sort_values(["date", "rvol"], ascending=[True, False]).reset_index(drop=True)
    out.to_csv(SCANLOG, index=False)
    return out["date"].nunique()


def fetch_intraday(symbol, interval=INTERVAL, count=BARS, refresh=False):
    """5m-Bars via TVRemix, gecacht je Symbol. Index = ET (America/New_York).
    refresh=True ignoriert den Cache und holt frisch (fuer den Forward-Eval noetig,
    damit die neu hinzugekommenen Tage abgedeckt sind)."""
    os.makedirs(CACHE, exist_ok=True)
    safe = symbol.replace(":", "_").replace("/", "_")   # "/" (Vorzugsaktien) wuerde Pfad brechen
    path = os.path.join(CACHE, f"{safe}_{interval}.csv")
    if os.path.exists(path) and not refresh:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df.tz_convert("America/New_York")
    data = _mcp_call("get_ohlcv", {"symbol": symbol, "interval": interval, "count": count})
    bars = data.get("bars") or []
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars)
    df["t"] = pd.to_datetime(df["t"], unit="s", utc=True)
    df = df.set_index("t").rename(columns={"o": "Open", "h": "High", "l": "Low",
                                           "c": "Close", "v": "Volume"})
    df = df[["Open", "High", "Low", "Close", "Volume"]].sort_index()
    df.to_csv(path)
    return df.tz_convert("America/New_York")


def ema(arr, n):
    return pd.Series(arr).ewm(span=n, adjust=False).mean().values


def bull_flag_trades(df, rr=2.0, pb_max=4, pole_look=12, min_pole=0.03,
                     max_risk=0.06, min_risk=0.004, ema_len=9,
                     rth=("09:30", "15:55"), entry_by="15:30", fill="break_next",
                     entry_slip=0.0):
    """Mechanischer Bull-Flag-Long-Entry intraday. Gibt Trades mit BRUTTO-ret + R zurueck.
    Keine ueberlappenden Positionen; nach Exit kann neu eingestiegen werden.
    fill: 'break_same' = Fill am Vorkerzenhoch, Stop/Target ab gleichem Bar (LOOK-AHEAD!).
          'break_next' = Fill am Vorkerzenhoch, Stop/Target erst ab NAECHSTEM Bar (sauber).
          'close_next' = Fill am Close der Trigger-Kerze, Stop/Target ab naechstem Bar (streng)."""
    if len(df) < pole_look + pb_max + 5:
        return pd.DataFrame()
    o, h, l, c = (df[x].values.astype(float) for x in ("Open", "High", "Low", "Close"))
    e9 = ema(c, ema_len)
    idx = df.index
    et_day = idx.normalize()
    tmin = idx.hour * 60 + idx.minute
    rth_lo = int(rth[0][:2]) * 60 + int(rth[0][3:])
    rth_hi = int(rth[1][:2]) * 60 + int(rth[1][3:])
    eby = int(entry_by[:2]) * 60 + int(entry_by[3:])
    in_rth = (tmin >= rth_lo) & (tmin <= rth_hi)

    # laufendes Tageshoch (HOD) je ET-Tag, ohne Lookahead (bis Bar i)
    day_codes, _ = pd.factorize(et_day)
    hod = np.empty(len(df)); run = -np.inf; prev = -1
    for i in range(len(df)):
        if day_codes[i] != prev:
            run = -np.inf; prev = day_codes[i]
        run = max(run, h[i]); hod[i] = run

    n = len(df); trades = []; i = pole_look + pb_max
    while i < n - 1:
        if not in_rth[i] or tmin[i] > eby or day_codes[i] != day_codes[i - 1]:
            i += 1; continue
        p = i - 1
        # Trigger: Bruch des Hochs der vorherigen roten Kerze
        prior_red = c[p] < o[p]
        breakout = h[i] >= h[p] and prior_red
        if not breakout:
            i += 1; continue
        # Kontext: ueber steigender 9-EMA
        if not (c[p] > e9[p] and e9[i] > e9[i - 3]):
            i += 1; continue
        # Pole: HOD kuerzlich (innerhalb pole_look) + Mindest-Anstieg im Fenster
        w0 = max(0, i - pole_look)
        recent_hod = h[i - 1] >= hod[i - 1] - 1e-9 or np.max(h[w0:i]) >= hod[i - 1] - 1e-9
        pole_rise = (np.max(h[w0:i]) - np.min(l[w0:i])) / max(np.min(l[w0:i]), 1e-9)
        if not (recent_hod and pole_rise >= min_pole):
            i += 1; continue
        # Pullback-Tief = Swing-Tief der letzten pb_max Bars
        stop = np.min(l[max(0, i - pb_max):i])
        entry = c[i] if fill == "close_next" else h[p]
        entry *= (1 + entry_slip)            # Einstiegs-Slippage (Buy-Stop frisst Breakout-Run)
        risk = (entry - stop) / entry
        if not (min_risk <= risk <= max_risk) or stop >= entry:
            i += 1; continue
        tgt = entry + rr * (entry - stop)
        # Simulation vorwaerts bis Stop/Target/EOD (gleicher Tag), Stop-first konservativ.
        # break_next/close_next: Aufloesung erst ab naechstem Bar (kein Intrabar-Lookahead).
        j0 = i if fill == "break_same" else i + 1
        end = i
        while end + 1 < n and day_codes[end + 1] == day_codes[i] and in_rth[end + 1]:
            end += 1
        exitp = c[end]; outcome = "eod"
        for j in range(j0, end + 1):
            if l[j] <= stop:
                exitp = stop; outcome = "stop"; break
            if h[j] >= tgt:
                exitp = tgt; outcome = "tgt"; break
        ret = (exitp - entry) / entry
        rmult = (exitp - entry) / (entry - stop)
        trades.append(dict(sym=None, time=idx[i], entry=entry, stop=stop,
                           ret=ret, R=rmult, outcome=outcome,
                           hour=idx[i].hour))
        i = end + 1  # nicht ueberlappen
    return pd.DataFrame(trades)


def report(tr, label, cost=0.0):
    if len(tr) == 0:
        print(f"  {label:22s}: keine Trades"); return None
    net = tr["ret"] - cost
    wr = (net > 0).mean() * 100
    pf_up = net[net > 0].sum(); pf_dn = -net[net < 0].sum()
    pf = pf_up / pf_dn if pf_dn > 0 else float("inf")
    g = tr["ret"].mean()
    print(f"  {label:22s}: {len(tr):4d} Tr | WR {wr:4.1f}% | brutto {g*100:+.3f}%/Tr "
          f"({g*1e4:+.0f} bps) | netto@{cost*1e4:.0f}bps {net.mean()*100:+.3f}%/Tr "
          f"| sum {net.sum()*100:+.1f}% | PF {pf:.2f}")
    return net.sum()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "backtest"

    if mode == "scan":
        try:
            df, total = scan()
        except Exception as ex:
            print("  [Fehler] Scanner-Abruf fehlgeschlagen -- TVRemix ist gerade ausgelastet (429).")
            print(f"           Bitte in ~1 Minute erneut versuchen.  Detail: {ex}")
            sys.exit(2)
        W = 100
        now_et = pd.Timestamp.now(tz="America/New_York")
        premarket = bool(df.attrs.get("premarket", _premarket_now()))
        print("=" * W)
        mlbl = "VORBOERSE / Gap-Scanner" if premarket else "regulaere Session"
        print(f"  WARRIOR-SCANNER   --   stocks in play   --   {now_et:%Y-%m-%d %H:%M} ET   [{mlbl}]")
        if premarket:
            print(f"  Vorboerse: Gap>=+{GAP_MIN:.0f}% | PM-Vol>={PM_VOL_MIN/1e3:.0f}k | $1-20 | Float<20M | NASDAQ/NYSE/AMEX")
            print(f"  Spalten: CHG%=heutiger Gap, VOL=Premarket-Vol, RVOL=Vortag (vorboerslich kein Live-RVOL)")
            print(f"  {total} Treffer  ->  Top {min(len(df), DISPLAY_N)} nach Gap{'   (+News)' if SHOW_NEWS else ''}")
        else:
            print(f"  Ross-Kriterien: >=+10% Tag | RVOL>=5x | $1-20 | Float<20M | NASDAQ/NYSE/AMEX")
            print(f"  {total} Treffer  ->  Top {min(len(df), DISPLAY_N)} nach RVOL{'   (+News)' if SHOW_NEWS else ''}")
        print("=" * W)
        hdr = (f"  {'#':>3s}  {'SYMBOL':15s} {'PRICE':>7s} {'CHG%':>8s} "
               f"{'RVOL':>7s} {'FLOAT':>7s} {'VOL':>8s}")
        print(hdr)
        print("  " + "-" * (W - 2))
        if len(df) == 0:
            print("  Keine Treffer -- Ross-Kriterien sind streng (an ruhigen Tagen/am Wochenende")
            print("  liefert der Snapshot oft nur 0-5 Namen). Spaeter / am Handelstag erneut.")
        news_map = {}
        nstate = {"off": False, "streak": 0}
        show = df.head(DISPLAY_N)
        for i, (_, r) in enumerate(show.iterrows(), 1):
            fl = r["float_shares_outstanding"]
            fls = f"{fl/1e6:.1f}M" if pd.notna(fl) else "?"
            _rv = r['relative_volume_10d_calc']
            rvs = (f"{_rv:.0f}x" if pd.notna(_rv) else "-")
            row = (f"  {i:>3d}  {r['symbol']:15s} {'$'+format(r['close'],'.2f'):>7s} "
                   f"{r['change']:>+7.1f}% {rvs:>7s} "
                   f"{fls:>7s} {r['volume']/1e6:>7.1f}M")
            print(row)
            ns = _news_fetch(r["symbol"], nstate)
            news_map[r["symbol"]] = ns
            if SHOW_NEWS:
                if ns == "ERR":
                    print("        News : Abruf gedrosselt (Rate-Limit) -- spaeter erneut")
                elif ns is None:
                    print("        News : keine aktuelle Schlagzeile  (kein frischer Grund / Squeeze)")
                else:
                    tag, age, age_d, txt = ns
                    txt = txt if len(txt) <= 76 else txt[:73] + "..."
                    flag = ("FRISCH" if (age_d is not None and age_d <= 2)
                            else ("alt/squeeze" if (age_d or 0) > 7 else ""))
                    tagstr = f"[{tag}] " if tag else ""
                    print(f"        News {age:>5s}  {flag:<11s}  {tagstr}{txt}")
        # Optional: News der NICHT angezeigten Namen fuer den Log nachladen (LOG_NEWS_ALL=1).
        # Default aus, damit ein Lauf nur ~DISPLAY_N News-Calls macht (schont das Rate-Limit).
        rest = (df["symbol"].iloc[DISPLAY_N:].tolist()
                if (SHOW_NEWS and LOG_NEWS_ALL) else [])
        if rest:
            print(f"  ... lade News fuer {len(rest)} weitere Namen (Log, LOG_NEWS_ALL) ...")
            for sym in rest:
                news_map[sym] = _news_fetch(sym, nstate)
        # Forward-Track = Premarket-Snapshot (die Namen, die du zur Eroeffnung vor dir hast).
        # Regulaere/Intraday-Laeufe aktualisieren nur Anzeige+HTML und verschmutzen den
        # survivorship-freien Track NICHT. FORCE_LOG=1 erzwingt das Loggen trotzdem.
        if premarket or os.getenv("FORCE_LOG") == "1":
            n_days = log_scan(df, news_map, mode=("premarket" if premarket else "regular"))
            print("  " + "-" * (W - 2))
            print(f"  [log]  stock_scan_log.csv: {len(df)} Namen geloggt | {n_days} Tage im Track "
                  f"(Forward-Universe, survivorship-frei).")
        else:
            print("  " + "-" * (W - 2))
            print("  [log]  regulaere Session -> NICHT in den Forward-Track geloggt "
                  "(Track = Premarket-Snapshot; FORCE_LOG=1 erzwingt).")
        html_path = scan_html(df, news_map, total, premarket=premarket)
        print(f"  [html] {html_path}  (im Browser oeffnen -- sortier-/filterbar)")
        print("  Lesart: FRISCHE News = echter Katalysator (Ross handelt das). Alte/keine News bei")
        print("  grossem Move = Squeeze/technisch -> vorsichtiger. Katalysator-Tag ist nur ein Hinweis.")
        sys.exit(0)

    # ---- Backtest-Modus ----
    print("=" * 92)
    print(f"  BULL-FLAG-BACKTEST  |  stocks in play  |  {INTERVAL}  |  Universe Top {N_NAMES} nach RVOL")
    print("=" * 92)
    uni, _ = scan(limit=N_NAMES)
    syms = uni["symbol"].tolist()
    print(f"  Universe: {len(syms)} Namen. Lade Intraday ({BARS} Bars je Symbol) ...")

    all_tr, all_trc = [], []
    for k, sym in enumerate(syms):
        try:
            df = fetch_intraday(sym)
        except Exception as ex:
            print(f"    [skip] {sym}: {ex}"); continue
        if len(df) < 100:
            continue
        tr = bull_flag_trades(df, fill="break_next")          # idealer Buy-Stop-Fill
        trc = bull_flag_trades(df, fill="close_next")         # realistischer Confirm-Fill
        if len(tr):
            tr["sym"] = sym; all_tr.append(tr)
        if len(trc):
            trc["sym"] = sym; all_trc.append(trc)
        if (k + 1) % 10 == 0:
            print(f"    ... {k+1}/{len(syms)} verarbeitet")
        time.sleep(0.05)

    if not all_tr:
        print("  Keine Trades erzeugt."); sys.exit(0)
    T = pd.concat(all_tr, ignore_index=True)
    T["time"] = pd.to_datetime(T["time"], utc=True)
    Tc = pd.concat(all_trc, ignore_index=True) if all_trc else T.iloc[:0]
    span_days = (T["time"].max() - T["time"].min()).days

    print("\n" + "-" * 92)
    print(f"  GESAMT  ({len(T)} Trades, {T['sym'].nunique()} Namen, ~{span_days} Tage Fenster)")
    print("-" * 92)
    print("  [A] FILL AM BREAKOUT-LEVEL (idealer Buy-Stop, 0 Slippage) -- unrealistisch fuer low-float:")
    for cst in COSTS:
        report(T, f"  break_next @{cst*1e4:.0f}bps", cst)
    print("\n  [B] FILL AM KERZEN-CLOSE (Breakout bestaetigt, dann Markt-Kauf) -- was Retail real tut:")
    for cst in COSTS:
        report(Tc, f"  close_next @{cst*1e4:.0f}bps", cst)
    print("\n  >> Der gesamte 'Edge' ist die Differenz [A]-[B] = der Run INNERHALB der Breakout-Kerze.")
    print("     Er lebt im Fill am Level (HFT/Maker). Siehe stock_fillcheck.py + Slippage-Sweep.")

    print("\n  Outcome-Mix:", dict(T["outcome"].value_counts()))
    print(f"  Erwartung pro Trade (R, brutto): {T['R'].mean():+.2f}R  |  Median {T['R'].median():+.2f}R")

    # Zeit-Split (erste vs zweite Haelfte des Fensters) als Mini-OOS
    mid = T["time"].min() + (T["time"].max() - T["time"].min()) / 2
    print("\n  ZEIT-SPLIT (Mini-OOS, @10bps):")
    report(T[T["time"] < mid], "1. Haelfte", 0.0010)
    report(T[T["time"] >= mid], "2. Haelfte", 0.0010)

    # Konzentration: traegt das Ergebnis 1 Name?
    print("\n  TOP-5-NAMEN nach Netto-Summe @10bps:")
    by = (T.assign(net=T["ret"] - 0.0010).groupby("sym")
          .agg(n=("net", "size"), net=("net", "sum")).sort_values("net", ascending=False))
    for s, r in by.head(5).iterrows():
        print(f"    {s:14s}  {int(r['n']):3d} Tr   netto {r['net']*100:+.1f}%")
    print("  ... BOTTOM-3:")
    for s, r in by.tail(3).iterrows():
        print(f"    {s:14s}  {int(r['n']):3d} Tr   netto {r['net']*100:+.1f}%")

    # Tageszeit (Guide: Morgens am staerksten)
    print("\n  NACH STUNDE (ET, brutto Ø/Trade):")
    hr = T.groupby("hour").agg(n=("ret", "size"), avg=("ret", "mean"))
    for h_, r in hr.iterrows():
        print(f"    {int(h_):02d}:00  {int(r['n']):4d} Tr   {r['avg']*1e4:+5.0f} bps")

    print("\n  BEFUND: [A] sieht stark aus (+83 bps, PF 1.7) -- ABER [B] (realistischer Fill) ist")
    print("  brutto NEGATIV. Kontrollen (stock_controls.py): Random-Long -4 bps, Buy&Hold -27 bps")
    print("  (die Namen FADEN intraday), Bull schlaegt Random um +76 bps -- der GANZE Vorsprung ist")
    print("  der Sprung vom Breakout-Level zum Kerzen-Close. Slippage-Sweep: ~50 bps Einstiegs-")
    print("  Slippage = Breakeven. Fuer low-float RVOL-Raketen ist das guenstig => kein Retail-Edge.")
    print("  Der Scanner (Modus 'scan') bleibt wertvoll; der mechanische Entry ist Execution-Alpha.")

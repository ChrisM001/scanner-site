"""
Cloud-Builder (laeuft in GitHub Actions). Fuehrt beide Scanner aus und legt die
Ergebnisse + eine mobile Startseite unter docs/ ab (von GitHub Pages serviert).

Robust: faellt EIN Scanner aus (z.B. Boerse blockt Cloud-IP, oder TVRemix 429),
bleibt das letzte gute docs/-HTML des anderen erhalten und die Seite wird mit
Hinweis-Badge gebaut. Exit-Code immer 0, damit der Commit/Deploy-Step laeuft.

ENV:
  TVREMIX_API_KEY   -- GitHub-Secret, fuer den Aktien-Scan (Pflicht fuer Aktien)
  CRYPTO_EXCHANGE   -- Boerse fuer den Krypto-Scan (default bybit; cloud-tauglich)
"""
import os, sys, subprocess, shutil, datetime, html, json

DIR  = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(DIR, "docs")
PY   = sys.executable
os.makedirs(DOCS, exist_ok=True)

# Auf dem Homescreen laeuft die Seite als Standalone-PWA -> die native "runterziehen
# zum Neuladen"-Geste fehlt UND die Seite friert beim Reoeffnen ein. Dieses Snippet
# wird in JEDE Seite injiziert (inject_refresh) und bringt: (1) Pull-to-Refresh per
# Touch, (2) Auto-Reload beim Wieder-Anzeigen/aus dem bfcache-Hintergrund.
REFRESH_JS = r"""<script>
(function(){
  var loadedAt = Date.now();
  // Cache-bustender Reload: frischer ?v= erzwingt eine frische Antwort (kein
  // Zweifel am max-age=600 von GitHub Pages). location.replace -> keine History-Flut.
  function hardReload(){ location.replace(location.pathname + '?v=' + Date.now()); }
  window.addEventListener('pageshow', function(e){ if(e.persisted) hardReload(); });
  document.addEventListener('visibilitychange', function(){
    if(document.visibilityState === 'visible' && Date.now() - loadedAt > 45000) hardReload();
  });
  var startY = 0, pulling = false, armed = false, THRESH = 70;
  var PULL = '↓ Zum Aktualisieren ziehen', REL = '↻ Loslassen zum Aktualisieren';
  var ind = document.createElement('div');
  ind.style.cssText = 'position:fixed;top:0;left:0;right:0;text-align:center;padding:10px;'
    + 'font:13px -apple-system,Segoe UI,sans-serif;color:#6ea8fe;background:#0f1115;'
    + 'transform:translateY(-100%);transition:transform .15s;z-index:9999';
  ind.textContent = PULL;
  function addInd(){ if(document.body){ document.body.appendChild(ind); }
    else { document.addEventListener('DOMContentLoaded', addInd); } }
  addInd();
  window.addEventListener('touchstart', function(e){
    if(window.scrollY <= 0){ startY = e.touches[0].clientY; pulling = true; armed = false; ind.textContent = PULL; }
  }, {passive:true});
  window.addEventListener('touchmove', function(e){
    if(!pulling) return;
    var dy = e.touches[0].clientY - startY;
    if(dy > 0){
      ind.style.transform = 'translateY(' + Math.min(dy - ind.offsetHeight, THRESH) + 'px)';
      // Text spiegelt den Zustand: erst ab Schwelle loest Loslassen wirklich aus.
      armed = dy > THRESH;
      ind.textContent = armed ? REL : PULL;
    }
  }, {passive:true});
  window.addEventListener('touchend', function(e){
    if(!pulling) return; pulling = false;
    if(e.changedTouches[0].clientY - startY > THRESH){
      ind.textContent = '↻ Aktualisiere…'; hardReload();
    } else ind.style.transform = 'translateY(-100%)';
  }, {passive:true});
})();
</script>"""


def inject_refresh(html_str):
    """REFRESH_JS vor </body> einfuegen (einmal). Faellt zurueck auf Anhaengen."""
    if "</body>" in html_str:
        return html_str.replace("</body>", REFRESH_JS + "</body>", 1)
    return html_str + REFRESH_JS


def placeholder(path, title, note):
    """Minimale Seite, damit ein Karten-Link nie ins 404 laeuft (z.B. Aktien
    ausserhalb der Handelszeit). Mit Refresh-Script -> Runterziehen laedt neu,
    sobald wieder Daten da sind."""
    page = (
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">'
        '<meta http-equiv="Pragma" content="no-cache"><meta http-equiv="Expires" content="0">'
        f'<title>{html.escape(title)}</title><style>:root{{color-scheme:dark}}'
        'body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;'
        'background:#0f1115;color:#e6e9ef}.wrap{max-width:560px;margin:0 auto;padding:22px 16px}'
        'a{color:#6ea8fe;text-decoration:none;font-size:13px}h1{font-size:19px}'
        '.note{color:#9aa4b2;font-size:14px;line-height:1.5;margin-top:14px}</style></head><body>'
        f'<div class="wrap"><a href="index.html">&#8592; Scanner</a><h1>{html.escape(title)}</h1>'
        f'<p class="note">{html.escape(note)}</p>'
        '<p class="note">Zum Aktualisieren die Seite nach unten ziehen.</p></div></body></html>')
    with open(path, "w", encoding="utf-8") as f:
        f.write(inject_refresh(page))


def run(label, args, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    try:
        r = subprocess.run([PY, *args], cwd=DIR, env=env,
                            capture_output=True, text=True, timeout=900)
        print(f"----- {label}  (exit {r.returncode}) -----")
        sys.stdout.write((r.stdout or "")[-3000:])
        if (r.stderr or "").strip():
            sys.stdout.write("\n[stderr] " + r.stderr[-1500:])
        print()
        return r.returncode == 0
    except Exception as ex:
        print(f"{label}: FEHLER {ex}")
        return False


def copy_if(src, dst):
    s = os.path.join(DIR, src)
    if os.path.exists(s):
        with open(s, "r", encoding="utf-8") as f:
            content = f.read()
        with open(os.path.join(DOCS, dst), "w", encoding="utf-8") as f:
            f.write(inject_refresh(content))
        return True
    return False


def teaser(csv, by, sym_col):
    """Top-3-Zeile des juengsten Tages als Vorschau fuer die Startseite."""
    p = os.path.join(DIR, csv)
    if not os.path.exists(p):
        return ""
    try:
        import pandas as pd
        d = pd.read_csv(p)
        if not len(d):
            return ""
        if "date" in d:
            d = d[d["date"] == d["date"].max()]
        d = d.sort_values(by, ascending=False).drop_duplicates(sym_col).head(3)
        return ", ".join(f"{r[sym_col]} {r[by]:+.0f}%" for _, r in d.iterrows())
    except Exception:
        return ""


def render_regime(json_path, html_out):
    """Render the multi-asset regime allocation page from the signal JSON.
    Returns (ok, teaser)."""
    if not os.path.exists(json_path):
        return False, ""
    try:
        d = json.load(open(json_path))
    except Exception:
        return False, ""
    n = d["n_assets"]
    coins = ""
    for a in d["assets"]:
        per = a["exposure"] / n * 100
        cls = "on" if a["gate"] > 0 else "off"
        px = a["price"]
        pxs = f"${px:,.2f}" if px >= 1 else f"${px:,.4f}"
        coins += (
            f'<div class="coin"><div class="cl">'
            f'<span class="sym">{html.escape(a["sym"])}</span>'
            f'<span class="hold {cls}">{per:.1f}%</span></div>'
            f'<div class="cr"><span>{pxs}</span><span>EMAs {a["emas_above"]}/4</span>'
            f'<span>Gate {a["gate"]*100:.0f}%</span><span>Vol {a["vol"]*100:.0f}%</span></div></div>'
        )
    port = d["portfolio_exposure"] * 100
    teaser = (f"Portfolio {port:.0f}% long" if port > 1 else "Portfolio 0% – alles Cash")
    page = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Krypto-Regime</title><style>
:root{{color-scheme:dark}}
*{{box-sizing:border-box}}
body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#0f1115;color:#e6e9ef;overflow-x:hidden}}
.wrap{{max-width:560px;margin:0 auto;padding:20px 16px 40px}}
.sub,.bigl,.foot,.cr{{overflow-wrap:anywhere}}
h1{{font-size:20px;margin:2px 0}} .sub{{color:#9aa4b2;font-size:12px;margin:2px 0 16px}}
.big{{background:#171a21;border:1px solid #232733;border-radius:14px;padding:18px;text-align:center;margin-bottom:16px}}
.bign{{font-size:34px;font-weight:720;color:#5ee08a}} .bign.zero{{color:#e0b15e}}
.bigl{{color:#9aa4b2;font-size:13px;margin-top:4px}}
.coin{{background:#171a21;border:1px solid #232733;border-radius:12px;padding:12px 14px;margin-bottom:10px}}
.cl{{display:flex;justify-content:space-between;align-items:center}}
.sym{{font-size:17px;font-weight:650}} .hold{{font-size:17px;font-weight:700}}
.hold.on{{color:#5ee08a}} .hold.off{{color:#6c7686}}
.cr{{display:flex;gap:12px;flex-wrap:wrap;color:#9aa4b2;font-size:12px;margin-top:6px}}
.foot{{color:#7a8493;font-size:12px;margin-top:18px;line-height:1.55}}
a.back{{color:#6ea8fe;text-decoration:none;font-size:13px}}
</style></head><body><div class="wrap">
<a class="back" href="index.html">&#8592; Scanner</a>
<h1>&#129518; Krypto-Regime-Portfolio</h1>
<div class="sub">{d["date"]} &middot; Quelle {html.escape(d["source"])} &middot; Ziel-Vola {d["target_vol"]*100:.0f}% &middot; gleichgewichtet 1/{n}</div>
<div class="big"><div class="bign {'zero' if port<=1 else ''}">{port:.0f}%</div>
<div class="bigl">des Kapitals long (Rest Cash) &middot; pro Coin = Anteil/{n}</div></div>
{coins}
<p class="foot">EMA-Regime-Gate (Anteil der EMAs 50/100/150/200 &uuml;ber dem Preis) &times; inverse-Vola-Sizing.
Long nur in Aufw&auml;rtstrends, Gr&ouml;&szlig;e nach Vola gedeckelt. Ehrliche Erwartung (Walk-Forward):
Sharpe&nbsp;~0.65, max&nbsp;Drawdown&nbsp;~25&ndash;30%. Rebalance 1&times;t&auml;glich nach Tagesschluss.
Risk-controlled Beta, kein Alpha &middot; PAPER/Research, keine Anlageberatung.</p>
</div></body></html>"""
    with open(html_out, "w", encoding="utf-8") as f:
        f.write(inject_refresh(page))
    return True, teaser


def _reltime(dt, now):
    if dt is None:
        return ""
    secs = (now - dt).total_seconds()
    if secs < 0:
        secs = 0
    if secs < 3600:
        return f"vor {int(secs // 60)} Min"
    if secs < 86400:
        return f"vor {int(secs // 3600)} Std"
    return f"vor {int(secs // 86400)} T"


def render_news(html_out, now):
    """Schlagzeilen-Seite (Krypto + Maerkte) aus RSS-Feeds. Returns (ok, teaser)."""
    try:
        from news_fetch import fetch_news
        items = fetch_news()
    except Exception as e:
        print(f"[news] nicht ladbar: {e}")
        return False, ""
    if not items:
        return False, ""
    rows = ""
    for it in items:
        rt = _reltime(it.get("dt"), now)
        meta = f'<span class="src">{html.escape(it["source"])}</span>'
        if rt:
            meta += f' &middot; {rt}'
        rows += (
            f'<a class="ni" href="{html.escape(it["link"])}" target="_blank" rel="noopener">'
            f'<div class="nt">{html.escape(it["title"])}</div>'
            f'<div class="nm">{meta}</div></a>'
        )
    page = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache"><meta http-equiv="Expires" content="0">
<title>News</title><style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}
body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#0f1115;color:#e6e9ef;overflow-x:hidden}}
.wrap{{max-width:560px;margin:0 auto;padding:20px 16px 40px}}
a.back{{color:#6ea8fe;text-decoration:none;font-size:13px}}
h1{{font-size:20px;margin:2px 0}} .sub{{color:#9aa4b2;font-size:12px;margin:2px 0 16px}}
a.ni{{display:block;text-decoration:none;color:inherit;background:#171a21;border:1px solid #232733;
 border-radius:12px;padding:12px 14px;margin-bottom:10px}}
a.ni:active{{background:#1c2029}}
.nt{{font-size:15px;line-height:1.35;overflow-wrap:anywhere}}
.nm{{color:#9aa4b2;font-size:12px;margin-top:6px}} .src{{color:#6ea8fe;font-weight:600}}
.foot{{color:#7a8493;font-size:12px;margin-top:18px;line-height:1.55}}
</style></head><body><div class="wrap">
<a class="back" href="index.html">&#8592; Scanner</a>
<h1>&#128240; Markt- &amp; Krypto-News</h1>
<div class="sub">{now:%Y-%m-%d %H:%M} UTC &middot; {len(items)} Schlagzeilen &middot; tippen &ouml;ffnet die Quelle</div>
{rows}
<p class="foot">Aggregiert aus CoinDesk, Cointelegraph, CNBC, MarketWatch, Yahoo Finance.
Nur Schlagzeilen-Vorschau &mdash; keine Anlageberatung.</p>
</div></body></html>"""
    with open(html_out, "w", encoding="utf-8") as f:
        f.write(inject_refresh(page))
    teaser = items[0]["title"]
    return True, (teaser[:70] + "…") if len(teaser) > 70 else teaser


def badge(ok, has):
    if ok:  return '<span class="ok">aktualisiert</span>'
    if has: return '<span class="stale">letzter Stand</span>'
    return '<span class="err">nicht verfuegbar</span>'


def _latest_rows(csv):
    """Zeilen des juengsten Laufs (max ts) aus einem Scan-Log."""
    p = os.path.join(DIR, csv)
    if not os.path.exists(p):
        return None
    try:
        import pandas as pd
        d = pd.read_csv(p)
    except Exception:
        return None
    if "ts" not in d.columns or not len(d):
        return None
    return d[d["ts"] == d["ts"].max()].copy()


def _tv_url(kind, r):
    """Direkter TradingView-Chart-Link je Symbol -> in die Telegram-Nachricht.
    Loest "Alarm da, aber Symbol nicht in der HTML": der User tippt den Alarm an
    und landet sofort im Chart, unabhaengig vom (transienten) Seitenzustand."""
    try:
        if kind == "crypto":
            from crypto_scan import TVPREFIX           # gleiche Quelle wie der Seiten-Link
            pref = TVPREFIX.get(str(r.get("exchange", "")).lower(), "BINANCE")
            sym = str(r["coin"]).replace("/", "").upper()      # ESPORTS/USDT -> ESPORTSUSDT
            return f"https://www.tradingview.com/chart/?symbol={pref}:{sym}.P"
        sym = str(r["symbol"]).upper()
        return f"https://www.tradingview.com/chart/?symbol={sym}"
    except Exception:
        return ""


def send_alerts(when):
    """Diff gegen letzten Lauf -> Telegram-Push fuer neue Coins/Aktien."""
    try:
        from alerts import tg_send, diff, enabled
    except Exception as e:
        print(f"[alert] Modul nicht ladbar: {e}"); return
    if not enabled():
        print("[alert] Telegram nicht konfiguriert (keine Secrets) -> keine Alarme"); return
    import html as _h
    lines = []
    specs = [("crypto", "crypto_scan_log.csv", "coin", "pct24h", "rvol", "🪙"),
             ("stock",  "stock_scan_log.csv",  "symbol", "change", "rvol", "📈")]
    any_changed = False
    for kind, csv, symcol, pctcol, rvcol, emoji in specs:
        d = _latest_rows(csv)
        if d is None or symcol not in d.columns:
            continue
        items = list(zip(d[symcol].astype(str), d[pctcol]))
        nw_list, changed = diff(kind, items)
        any_changed = any_changed or changed
        nw = set(nw_list)
        if not nw:
            continue
        sub = d[d[symcol].astype(str).isin(nw)].sort_values(rvcol, ascending=False)
        for _, r in sub.iterrows():
            cat = str(r.get("news_headline") or "").strip()
            cat = " — " + _h.escape(cat[:90]) if cat and cat.lower() != "nan" else ""
            try:
                pct = float(r[pctcol]); rv = float(r[rvcol])
                meta = f"  {pct:+.0f}%  RVOL {rv:.0f}x"
            except Exception:
                meta = ""
            url = _tv_url(kind, r)
            label = _h.escape(str(r[symcol]))
            sym_html = f'<a href="{url}">{label}</a>' if url else label
            lines.append(f"{emoji} <b>{sym_html}</b>{meta}{cat}")
    if lines:
        msg = (f"\U0001F4DF <b>Scanner</b> — {len(lines)} neu in play  "
               f"({when:%Y-%m-%d %H:%M} UTC)\n" + "\n".join(lines[:25]))
        ok = tg_send(msg)
        print(f"[alert] {len(lines)} neue Eintraege -> Telegram {'OK' if ok else 'FEHLER'}")
    else:
        print("[alert] keine neuen Eintraege")
    return any_changed


def mark_commit(any_changed):
    """Commit-Sentinel schreiben: nur committen, wenn sich das Set geaendert hat
    ODER der letzte Commit aelter als HEARTBEAT_MIN ist (Seite nicht einfrieren).
    Reduziert den Commit-Churn bei 5-Min-Takt drastisch."""
    reason = None
    if any_changed:
        reason = "set changed"
    else:
        hb = int(os.getenv("HEARTBEAT_MIN", "60"))
        try:
            import time as _t
            ts = subprocess.run(["git", "-C", DIR, "log", "-1", "--format=%ct"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
            age_min = (_t.time() - int(ts)) / 60 if ts else 1e9
        except Exception:
            age_min = 1e9
        if age_min >= hb:
            reason = f"heartbeat ({age_min:.0f}min >= {hb})"
        else:
            print(f"[commit] uebersprungen -- keine Aenderung (letzter Commit {age_min:.0f}min her)")
    if reason:
        with open(os.path.join(DIR, ".do_commit"), "w") as f:
            f.write(reason)
        print(f"[commit] committen -- {reason}")


now = datetime.datetime.now(datetime.timezone.utc)
# Boersen-Kette: erste, die durchlaeuft. GitHub-Runner-IPs werden von Binance/Bitget/
# Bybit/OKX geblockt (403/451); gate/mexc sind cloud-tauglich. Reihenfolge per
# CRYPTO_EXCHANGE (Komma-Liste).
CRYPTO_CHAIN = [e.strip() for e in os.getenv("CRYPTO_EXCHANGE", "gate,mexc,bybit").split(",") if e.strip()]

# Aktien: nur im US-Marktfenster scannen (Premarket+Handel ~08:00-21:00 UTC, Mo-Fr).
# Bei haeufigem Cron (alle 20 Min) vermeidet das nutzlose Nacht-/Wochenend-Laeufe +
# TVRemix-Last. SCAN_STOCKS=1/0 ueberschreibt die Zeitlogik.
_se = os.getenv("SCAN_STOCKS")
do_stocks = (_se == "1") if _se is not None else (now.weekday() < 5 and 8 <= now.hour < 21)
if do_stocks:
    stock_ok = run("Aktien-Scan", ["stock_momentum.py", "scan"])
else:
    stock_ok = False
    print("[stocks] ausserhalb US-Marktfenster -> uebersprungen (Krypto laeuft weiter)")

crypto_ok = False
CEX = CRYPTO_CHAIN[0] if CRYPTO_CHAIN else "gate"
for cex in CRYPTO_CHAIN:
    if run(f"Krypto-Scan [{cex}]", ["crypto_scan.py"], extra_env={"EXCHANGE": cex, "DIR": "up"}):
        crypto_ok = True; CEX = cex
        print(f"  [crypto] Boerse {cex} OK")
        break
    print(f"  [crypto] {cex} fehlgeschlagen -> naechste Boerse")

has_stock  = copy_if("stock_scan.html",  "stock.html")
has_crypto = copy_if("crypto_scan.html", "crypto.html")

# Krypto-Regime-Portfolio (BTC/ETH/XRP/SOL/LINK) -- eigenes Boersen-Fallback intern.
run("Krypto-Regime", ["crypto_regime_signal.py", "0.40", "--json", os.path.join(DOCS, "regime.json")])
regime_ok, regime_teaser = render_regime(os.path.join(DOCS, "regime.json"),
                                         os.path.join(DOCS, "regime.html"))
has_regime = os.path.exists(os.path.join(DOCS, "regime.html"))

# News-Ticker (RSS, keyfrei). Rein informativ -> kein Commit/Alert-Trigger.
news_ok, news_teaser = render_news(os.path.join(DOCS, "news.html"), now)
has_news = os.path.exists(os.path.join(DOCS, "news.html"))

# Platzhalter fuer fehlende Seiten -> Karten-Links laufen nie ins 404 (seit Artefakt-
# Deploy wird docs/ frisch gebaut; ausserhalb der Handelszeit fehlt sonst stock.html).
for _dst, _ok, _title, _note in [
    ("stock.html",  has_stock,  "Aktien · Warrior Gap-Scanner",
     "Aktien-Scan laeuft nur im US-Marktfenster (Mo-Fr ~08:00-21:00 UTC). Aktuell pausiert."),
    ("crypto.html", has_crypto, "Krypto · in play",
     "Krypto-Scan gerade nicht verfuegbar (Boerse blockt evtl. die Cloud-IP). Spaeter erneut ziehen."),
    ("regime.html", has_regime, "Krypto-Regime",
     "Regime-Signal gerade nicht verfuegbar."),
    ("news.html",   has_news,   "Markt- & Krypto-News",
     "News-Feeds gerade nicht erreichbar. Spaeter erneut ziehen."),
]:
    _p = os.path.join(DOCS, _dst)
    if not _ok and not os.path.exists(_p):
        placeholder(_p, _title, _note)
        print(f"[page] Platzhalter geschrieben: {_dst}")

# Push-Alarme: neue Eintraege seit letztem Lauf -> Telegram (no-op ohne Secrets).
# Rueckgabe: ob sich ein Listen-Set geaendert hat (steuert den Commit).
_changed = send_alerts(now)

stock_teaser  = teaser("stock_scan_log.csv",  "change", "symbol")
crypto_teaser = teaser("crypto_scan_log.csv", "pct24h", "coin")

index = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache"><meta http-equiv="Expires" content="0">
<title>Scanner</title><style>
:root{{color-scheme:dark}}
body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#0f1115;color:#e6e9ef}}
.wrap{{max-width:560px;margin:0 auto;padding:22px 16px 40px}}
h1{{font-size:21px;margin:4px 0 2px}} .sub{{color:#9aa4b2;font-size:13px;margin:0 0 20px}}
a.card{{display:block;text-decoration:none;color:inherit;background:#171a21;border:1px solid #232733;
 border-radius:14px;padding:16px 18px;margin:0 0 14px}}
a.card:active{{background:#1c2029}}
.rowt{{display:flex;align-items:center;justify-content:space-between;gap:10px}}
.t{{font-size:17px;font-weight:650}} .em{{font-size:21px;margin-right:9px}}
.teaser{{color:#c4ccd8;font-size:13px;margin-top:9px;min-height:18px}}
.ok{{color:#5ee08a}} .stale{{color:#e0b15e}} .err{{color:#ff6b6b}} .badge2{{font-size:12px;font-weight:600;white-space:nowrap}}
.foot{{color:#7a8493;font-size:12px;margin-top:22px;line-height:1.55}}
</style></head><body><div class="wrap">
<h1>&#128225; Scanner</h1>
<div class="sub">{now:%Y-%m-%d %H:%M} UTC &middot; automatisch aktualisiert (GitHub Actions)</div>

<a class="card" href="stock.html?v={now:%Y%m%d%H%M}">
  <div class="rowt"><div><span class="em">&#128200;</span><span class="t">Aktien &middot; Warrior Gap-Scanner</span></div>
  <span class="badge2">{badge(stock_ok, has_stock)}</span></div>
  <div class="teaser">{html.escape(stock_teaser) or 'keine Treffer / kein Lauf'}</div>
</a>

<a class="card" href="crypto.html?v={now:%Y%m%d%H%M}">
  <div class="rowt"><div><span class="em">&#129689;</span><span class="t">Krypto &middot; in play (RVOL)</span></div>
  <span class="badge2">{badge(crypto_ok, has_crypto)}</span></div>
  <div class="teaser">{html.escape(crypto_teaser) or 'keine Treffer / kein Lauf'}</div>
</a>

<a class="card" href="regime.html?v={now:%Y%m%d%H%M}">
  <div class="rowt"><div><span class="em">&#129518;</span><span class="t">Krypto-Regime &middot; Portfolio-Allokation</span></div>
  <span class="badge2">{badge(regime_ok, has_regime)}</span></div>
  <div class="teaser">{html.escape(regime_teaser) or 'kein Lauf'}</div>
</a>

<a class="card" href="news.html?v={now:%Y%m%d%H%M}">
  <div class="rowt"><div><span class="em">&#128240;</span><span class="t">Markt- &amp; Krypto-News</span></div>
  <span class="badge2">{badge(news_ok, has_news)}</span></div>
  <div class="teaser">{html.escape(news_teaser) or 'keine News geladen'}</div>
</a>

<p class="foot">Aktien: Ross-Gap-Scanner (vorboerslich Gap&ge;+10%, Float&lt;20M, $1&ndash;20).
Krypto: aktivste {html.escape(CEX)}-USDT-Perps nach RVOL. PAPER/Research, keine Anlageberatung.
Serverlos via GitHub Actions &mdash; aktuell auch wenn dein PC aus ist.</p>
</div></body></html>"""

with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
    f.write(inject_refresh(index))

print(f"docs/: index.html | stock.html={has_stock} | crypto.html={has_crypto}")
if not (has_stock or has_crypto):
    print("WARN: kein Scanner-Ergebnis -- Seite zeigt nur Platzhalter.")

# Commit-Sentinel setzen (Workflow committet nur, wenn .do_commit existiert).
mark_commit(_changed)
sys.exit(0)

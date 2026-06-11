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
import os, sys, subprocess, shutil, datetime, html

DIR  = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(DIR, "docs")
PY   = sys.executable
os.makedirs(DOCS, exist_ok=True)


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
        shutil.copyfile(s, os.path.join(DOCS, dst))
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


def badge(ok, has):
    if ok:  return '<span class="ok">aktualisiert</span>'
    if has: return '<span class="stale">letzter Stand</span>'
    return '<span class="err">nicht verfuegbar</span>'


now = datetime.datetime.now(datetime.timezone.utc)
CEX = os.getenv("CRYPTO_EXCHANGE", "bybit")

# Aktien: vorboerslich (Gap-Mode) am sinnvollsten -> Cron auf Premarket-ET legen.
stock_ok  = run("Aktien-Scan", ["stock_momentum.py", "scan"])
crypto_ok = run("Krypto-Scan", ["crypto_scan.py"],
                extra_env={"EXCHANGE": CEX, "DIR": "up"})

has_stock  = copy_if("stock_scan.html",  "stock.html")
has_crypto = copy_if("crypto_scan.html", "crypto.html")

stock_teaser  = teaser("stock_scan_log.csv",  "change", "symbol")
crypto_teaser = teaser("crypto_scan_log.csv", "pct24h", "coin")

index = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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

<a class="card" href="stock.html">
  <div class="rowt"><div><span class="em">&#128200;</span><span class="t">Aktien &middot; Warrior Gap-Scanner</span></div>
  <span class="badge2">{badge(stock_ok, has_stock)}</span></div>
  <div class="teaser">{html.escape(stock_teaser) or 'keine Treffer / kein Lauf'}</div>
</a>

<a class="card" href="crypto.html">
  <div class="rowt"><div><span class="em">&#129689;</span><span class="t">Krypto &middot; in play (RVOL)</span></div>
  <span class="badge2">{badge(crypto_ok, has_crypto)}</span></div>
  <div class="teaser">{html.escape(crypto_teaser) or 'keine Treffer / kein Lauf'}</div>
</a>

<p class="foot">Aktien: Ross-Gap-Scanner (vorboerslich Gap&ge;+10%, Float&lt;20M, $1&ndash;20).
Krypto: aktivste {html.escape(CEX)}-USDT-Perps nach RVOL. PAPER/Research, keine Anlageberatung.
Serverlos via GitHub Actions &mdash; aktuell auch wenn dein PC aus ist.</p>
</div></body></html>"""

with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
    f.write(index)

print(f"docs/: index.html | stock.html={has_stock} | crypto.html={has_crypto}")
if not (has_stock or has_crypto):
    print("WARN: kein Scanner-Ergebnis -- Seite zeigt nur Platzhalter.")
sys.exit(0)

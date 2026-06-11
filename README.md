# Scanner-Cloud (iPhone-tauglich, PC-unabhaengig)

Beide Scanner laufen **serverlos auf GitHub Actions** und veroeffentlichen ihre
Ergebnisse als Webseite ueber **GitHub Pages** — abrufbar auf dem iPhone per
Lesezeichen, **auch wenn dein PC aus ist**.

- **Aktien:** `stock_momentum.py scan` (Warrior Gap-Scanner, vorboerslich Gap-Mode)
- **Krypto:** `crypto_scan.py` (aktivste Perps nach RVOL; Boerse = **bybit**, weil
  Binance/Bitget die US-/Azure-IPs der GitHub-Runner blocken)
- `build_site.py` fuehrt beide aus und baut `docs/index.html` + `docs/stock.html` + `docs/crypto.html`
- Der Workflow committet `docs/` und die Forward-Track-CSVs zurueck → Git-Historie = Audit-Trail des Tracks

## Einmal-Einrichtung (GitHub-Seite — ~5 Min)

1. **Repo anlegen:** auf github.com → *New repository* → Name z.B. `scanner-site`,
   **Public** (Pages ist nur bei Public kostenlos), *ohne* README/gitignore. Erstellen.
2. **Hochladen** (in diesem Ordner, Git ist schon initialisiert):
   ```powershell
   git remote add origin https://github.com/<DEIN-USER>/scanner-site.git
   git branch -M main
   git push -u origin main
   ```
   (Beim ersten Push oeffnet sich ein Browser-Login fuer GitHub.)
3. **Secret setzen:** Repo → *Settings* → *Secrets and variables* → *Actions* →
   *New repository secret* → Name `TVREMIX_API_KEY`, Wert = dein TVRemix-Key.
4. **Pages aktivieren:** Repo → *Settings* → *Pages* → *Source* = **Deploy from a branch**,
   Branch = **main**, Ordner = **/docs** → *Save*.
5. **Ersten Lauf starten:** Repo → *Actions* → *scanners* → *Run workflow*.
   Danach laeuft er automatisch per Cron (siehe unten).
6. **Auf dem iPhone:** `https://<DEIN-USER>.github.io/scanner-site/` oeffnen →
   *Teilen* → *Zum Home-Bildschirm*. Fertig, sieht aus wie eine App.

## Laufzeiten (UTC, in `.github/workflows/scan.yml`)

- `11:00` & `13:00` Mo–Fr → vorboerslich (Gap-Mode), Aktien + Krypto
- `21:00` taeglich → Krypto-Refresh
- jederzeit manuell via *Run workflow* (auch aus der **GitHub-iPhone-App**)

## Logik aktualisieren

Die beiden Scanner sind **Kopien** aus dem Hauptordner. Nach Aenderungen dort:
```powershell
cd C:\Users\chmue\trading_ai\cloud_site
.\sync.ps1
```
Das kopiert `stock_momentum.py` + `crypto_scan.py` herueber, committet und pusht.

## Wichtig: zwei Tracks vermeiden

Lokaler Task **StockScan** (7:00 ET) **und** die Cloud schreiben je einen eigenen
`stock_scan_log.csv`. Damit der survivorship-freie Forward-Track nicht divergiert,
ist die **Cloud ab jetzt der kanonische Track** (Git-Historie!). Empfehlung: den
lokalen StockScan-Task deaktivieren — siehe Chat.

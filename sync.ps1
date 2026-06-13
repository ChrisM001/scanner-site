# Kopiert die aktuellen Scanner aus dem Hauptordner ins Cloud-Repo und pusht.
# Nach jeder Logik-Aenderung an stock_momentum.py / crypto_scan.py aufrufen:
#   cd C:\Users\chmue\trading_ai\cloud_site ; .\sync.ps1
$ErrorActionPreference = "Stop"
Copy-Item ..\stock_momentum.py        .\stock_momentum.py        -Force
Copy-Item ..\crypto_scan.py           .\crypto_scan.py           -Force
Copy-Item ..\crypto_regime_signal.py  .\crypto_regime_signal.py  -Force
git add stock_momentum.py crypto_scan.py crypto_regime_signal.py
git commit -m "sync scanners from main"
git push
Write-Host "Synchronisiert + gepusht. GitHub Actions baut die Seite neu."

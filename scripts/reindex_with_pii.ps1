# Re-index completo con redaction PII attiva.
# Lanciato dal task pianificato di Windows (una tantum, di notte).
# Riscrive tutti i ticket del perimetro applicando la PII (upsert idempotente).

$ErrorActionPreference = "Stop"
$proj = "C:\Users\fdesimone\OneDrive - Altea SPA\PROGETTI\AMPLIFON\IngestionPipeline"
Set-Location $proj

$env:PII_REDACTION_ENABLED = "true"

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$log = Join-Path $proj "reindex_pii_$stamp.log"

"=== Re-index con PII avviato: $(Get-Date) ===" | Out-File -FilePath $log -Encoding utf8
python -m pipeline.run --full *>> $log
"=== Terminato: $(Get-Date) (exit $LASTEXITCODE) ===" | Out-File -FilePath $log -Append -Encoding utf8

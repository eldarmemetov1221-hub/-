# ============================================================
#  Batch build of PDD bilets with ANTON voice (yandex3)
#  Exactly like the working test:
#    - structure + options from project bilet<N>.txt (auto-fetched
#      from pdd-exam.ru if missing)
#    - voice reads your prose from Desktop\<bilet><N>.txt (--speak)
#    - images from project kartinki<N>  (auto-fetched if missing)
#    - output -> Desktop\<pdd gotovye>\<bilet><N>.mp4
#    - 7 min pause between bilets. Stop anytime: Ctrl + C.
#
#  All Cyrillic names are built from char codes, so the script
#  parses correctly in any Windows encoding.
#
#  Run:
#    powershell -ExecutionPolicy Bypass -File .\sobrat_bilety.ps1 -From 1 -To 10
# ============================================================

param(
    [int]$From = 1,
    [int]$To   = 10,
    [string]$Voice  = "anton",
    [string]$Speed  = "",      # e.g. 0.95 (slower) / 1.05 (faster); empty = default
    [int]$Cooldown  = 420      # pause between bilets, sec (420 = 7 min); 0 = none
)

$ErrorActionPreference = "Stop"
$proj = $PSScriptRoot
Set-Location $proj

# --- Cyrillic words from code points (ASCII-safe source) ---
function CC([int[]]$codes) { -join ($codes | ForEach-Object { [char]$_ }) }
$W_bilet  = CC @(0x0431,0x0438,0x043B,0x0435,0x0442)                                # bilet
$W_outdir = CC @(0x043F,0x0434,0x0434,0x20,0x0433,0x043E,0x0442,0x043E,0x0432,0x044B,0x0435)  # pdd gotovye

if ($Speed -ne "") { $env:YANDEX_SPEED = $Speed }

$desk = [Environment]::GetFolderPath("Desktop")
$out  = Join-Path $desk $W_outdir
New-Item -ItemType Directory -Force -Path $out | Out-Null

function Find-Speak($n) {
    # your prose on the Desktop (voice text)
    $names = @("$W_bilet$n.txt", "$W_bilet $n.txt", "bilet$n.txt", "$W_bilet$n", "bilet$n")
    foreach ($nm in $names) {
        $p = Join-Path $desk $nm
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}

$done = 0; $skip = 0
for ($n = $From; $n -le $To; $n++) {
    Write-Host ""
    Write-Host "================== BILET $n ==================" -ForegroundColor Cyan

    # 1) structure + options (project bilet<N>.txt); fetch from site if missing
    $struct = Join-Path $proj "bilet$n.txt"
    if (-not (Test-Path -LiteralPath $struct)) {
        Write-Host "   no bilet$n.txt -> fetching structure from pdd-exam.ru..." -ForegroundColor DarkYellow
        python site_quiz.py $n --dump-bilet "bilet$n.txt"
        if (-not (Test-Path -LiteralPath $struct)) { Write-Host "!! bilet ${n}: could not get structure, skip" -ForegroundColor Red; $skip++; continue }
    }

    # 2) images (project kartinki<N>); fetch from site if missing
    $imgs = Join-Path $proj "kartinki$n"
    if (-not (Test-Path -LiteralPath $imgs)) {
        Write-Host "   no kartinki$n -> downloading images from pdd-exam.ru..." -ForegroundColor DarkYellow
        python site_quiz.py $n --save-images "kartinki$n"
    }

    # 3) your prose for the voice (Desktop)
    $speak = Find-Speak $n

    $cmdArgs = @($struct, (Join-Path $out "$W_bilet$n.mp4"),
                 "--engine", "yandex3", "--voice", $Voice,
                 "--images", $imgs, "--before", "0", "--pad", "0.3")
    if ($speak) { $cmdArgs += @("--speak", $speak); Write-Host ("   voice text: " + $speak) -ForegroundColor DarkGray }
    else        { Write-Host "   no Desktop text found -> voice reads explanation from bilet$n.txt" -ForegroundColor DarkGray }

    python auto_quiz.py @cmdArgs
    if ($LASTEXITCODE -eq 0) { $done++ } else { $skip++; Write-Host "!! bilet $n error" -ForegroundColor Red }

    if ($Cooldown -gt 0 -and $n -lt $To) {
        $mins = [math]::Round($Cooldown / 60, 1)
        Write-Host ("...cooldown $Cooldown sec (~$mins min, Ctrl+C to stop)") -ForegroundColor DarkYellow
        Start-Sleep -Seconds $Cooldown
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "DONE. Built: $done, skipped/errors: $skip" -ForegroundColor Green
Write-Host "Videos here: $out" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green

# ============================================================
#  Batch build of PDD bilets with ANTON voice (yandex3)
#  Run once -> it goes bilet by bilet -> walk away.
#  Stop anytime: Ctrl + C.  Output -> Desktop\<pdd gotovye>
#  (All Cyrillic names are built from char codes so the script
#   parses correctly in any Windows encoding.)
#
#  Run:
#    powershell -ExecutionPolicy Bypass -File .\sobrat_bilety.ps1 -From 1 -To 10
# ============================================================

param(
    [int]$From = 1,
    [int]$To   = 10,
    [string]$Dir    = "",      # where bilets live; default = Desktop
    [string]$Razbor = "",      # one combined razbor file with "### BILET N" markers
    [string]$Voice  = "anton",
    [string]$Speed  = "",      # e.g. 0.95 (slower) / 1.05 (faster); empty = default
    [int]$Cooldown  = 420      # pause between bilets, sec (420 = 7 min); 0 = none
)

$ErrorActionPreference = "Stop"
$proj = $PSScriptRoot
Set-Location $proj

# --- Cyrillic words built from code points (ASCII-safe source) ---
function CC([int[]]$codes) { -join ($codes | ForEach-Object { [char]$_ }) }
$W_bilet  = CC @(0x0431,0x0438,0x043B,0x0435,0x0442)                               # bilet
$W_BILET  = CC @(0x0411,0x0418,0x041B,0x0415,0x0422)                               # BILET (upper)
$W_outdir = CC @(0x043F,0x0434,0x0434,0x20,0x0433,0x043E,0x0442,0x043E,0x0432,0x044B,0x0435) # "pdd gotovye"
$W_kart   = "kartinki"
$W_razbor = "razbor"

if ($Speed -ne "") { $env:YANDEX_SPEED = $Speed }

$desk = [Environment]::GetFolderPath("Desktop")
if ($Dir -eq "") { $Dir = $desk }

$out = Join-Path $desk $W_outdir
New-Item -ItemType Directory -Force -Path $out | Out-Null

function Find-Bilet($n) {
    $names = @("$W_bilet$n.txt", "$W_bilet $n.txt", "bilet$n.txt", "$W_bilet$n", "$W_bilet $n", "bilet$n")
    foreach ($nm in $names) {
        $p = Join-Path $Dir $nm
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}

# --- combined razbor file -> split by "### BILET N" markers ---
$split = @{}
if ($Razbor -ne "") {
    if (-not (Test-Path -LiteralPath $Razbor)) { Write-Host "Razbor file not found: $Razbor" -ForegroundColor Red; exit 1 }
    $marker = "^\s*#{0,3}\s*(?:$W_BILET|BILET)\s+(\d+)"
    $tmp = Join-Path $env:TEMP ("razbor_split_" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $cur = $null; $buf = New-Object System.Collections.Generic.List[string]
    foreach ($line in Get-Content -LiteralPath $Razbor -Encoding UTF8) {
        $m = [regex]::Match($line, $marker, 'IgnoreCase')
        if ($m.Success) {
            if ($null -ne $cur -and $buf.Count -gt 0) { $f = Join-Path $tmp "$W_razbor$cur.txt"; Set-Content -LiteralPath $f -Value $buf -Encoding UTF8; $split[$cur] = $f }
            $cur = [int]$m.Groups[1].Value; $buf = New-Object System.Collections.Generic.List[string]
        } elseif ($null -ne $cur) { $buf.Add($line) }
    }
    if ($null -ne $cur -and $buf.Count -gt 0) { $f = Join-Path $tmp "$W_razbor$cur.txt"; Set-Content -LiteralPath $f -Value $buf -Encoding UTF8; $split[$cur] = $f }
}

$done = 0; $skip = 0
for ($n = $From; $n -le $To; $n++) {
    $bilet = Find-Bilet $n
    if (-not $bilet) { Write-Host "-- bilet $n: file not found in $Dir, skip" -ForegroundColor DarkGray; continue }

    $cmdArgs = @($bilet, (Join-Path $out "$W_bilet$n.mp4"))

    $rz = $null
    if ($split.ContainsKey($n)) { $rz = $split[$n] }
    else {
        foreach ($cand in @((Join-Path $Dir "$W_razbor$n.txt"), (Join-Path $proj "$W_razbor$n.txt"))) {
            if (Test-Path -LiteralPath $cand) { $rz = $cand; break }
        }
    }
    if ($rz) { $cmdArgs += @("--speak", $rz) }

    foreach ($cand in @((Join-Path $Dir "$W_kart$n"), (Join-Path $proj "$W_kart$n"))) {
        if (Test-Path -LiteralPath $cand) { $cmdArgs += @("--images", $cand); break }
    }

    $cmdArgs += @("--engine", "yandex3", "--voice", $Voice, "--before", "0", "--pad", "0.3")

    Write-Host ""
    Write-Host "================== BILET $n ==================" -ForegroundColor Cyan
    Write-Host ("file: " + $bilet) -ForegroundColor DarkGray
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

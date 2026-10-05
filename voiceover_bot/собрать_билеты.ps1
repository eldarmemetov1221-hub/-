# ============================================================
#  АВТО-СБОРКА БИЛЕТОВ ГОЛОСОМ ANTON (yandex3) — ПАЧКОЙ
#  Запускаешь один раз -> идёт по билетам сам -> уходишь.
#  Остановить в любой момент: Ctrl + C.
#  Готовые видео -> Рабочий стол\пдд готовые
#
#  Берёт билеты с РАБОЧЕГО СТОЛА (или из папки -Dir). Для билета N
#  ищет файл с любым из имён:
#     билет<N>.txt, билет <N>.txt, bilet<N>.txt  (можно и без .txt)
#  Картинки: папка kartinki<N> рядом с билетом или в папке проекта.
#
#  Разбор (твой текст для голоса) — по желанию:
#     -Razbor <файл>  — один общий файл, поделённый метками «### БИЛЕТ N»
#     либо razbor<N>.txt рядом с билетом.
#  Без разбора голос читает пояснение из самого билета.
#
#  Запуск:
#     powershell -ExecutionPolicy Bypass -File .\собрать_билеты.ps1 -From 1 -To 10
# ============================================================

param(
    [int]$From = 1,
    [int]$To   = 10,
    [string]$Dir    = "",       # где лежат билеты; по умолчанию Рабочий стол
    [string]$Razbor = "",       # общий файл разбора с метками «### БИЛЕТ N» (по желанию)
    [string]$Voice  = "anton",
    [string]$Speed  = "",       # напр. 0.95 (медленнее) / 1.05 (быстрее)
    [int]$Cooldown  = 420       # пауза между билетами, сек (остывание); 420 = 7 минут, 0 = без паузы
)

$ErrorActionPreference = "Stop"
$proj = $PSScriptRoot
Set-Location $proj

if ($Speed -ne "") { $env:YANDEX_SPEED = $Speed }

$desk = [Environment]::GetFolderPath("Desktop")
if ($Dir -eq "") { $Dir = $desk }

$out = Join-Path $desk "пдд готовые"
New-Item -ItemType Directory -Force -Path $out | Out-Null

function Find-Bilet($n) {
    $names = @("билет$n.txt", "билет $n.txt", "bilet$n.txt", "билет$n", "билет $n", "bilet$n")
    foreach ($nm in $names) {
        $p = Join-Path $Dir $nm
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}

# --- общий файл разбора -> делим по меткам «### БИЛЕТ N» ---
$split = @{}
if ($Razbor -ne "") {
    if (-not (Test-Path -LiteralPath $Razbor)) { Write-Host "Файл разбора не найден: $Razbor" -ForegroundColor Red; exit 1 }
    $tmp = Join-Path $env:TEMP ("razbor_split_" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $cur = $null; $buf = New-Object System.Collections.Generic.List[string]
    foreach ($line in Get-Content -LiteralPath $Razbor -Encoding UTF8) {
        $m = [regex]::Match($line, '^\s*#{0,3}\s*БИЛЕТ\s+(\d+)', 'IgnoreCase')
        if ($m.Success) {
            if ($cur -ne $null -and $buf.Count -gt 0) { $f = Join-Path $tmp "razbor$cur.txt"; Set-Content -LiteralPath $f -Value $buf -Encoding UTF8; $split[$cur] = $f }
            $cur = [int]$m.Groups[1].Value; $buf = New-Object System.Collections.Generic.List[string]
        } elseif ($cur -ne $null) { $buf.Add($line) }
    }
    if ($cur -ne $null -and $buf.Count -gt 0) { $f = Join-Path $tmp "razbor$cur.txt"; Set-Content -LiteralPath $f -Value $buf -Encoding UTF8; $split[$cur] = $f }
}

$done = 0; $skip = 0
for ($n = $From; $n -le $To; $n++) {
    $bilet = Find-Bilet $n
    if (-not $bilet) { Write-Host "-- билет $n: файл не найден в $Dir, пропускаю" -ForegroundColor DarkGray; continue }

    $cmdArgs = @($bilet, (Join-Path $out "билет$n.mp4"))

    $rz = $null
    if ($split.ContainsKey($n)) { $rz = $split[$n] }
    else {
        foreach ($cand in @((Join-Path $Dir "razbor$n.txt"), (Join-Path $proj "razbor$n.txt"))) {
            if (Test-Path -LiteralPath $cand) { $rz = $cand; break }
        }
    }
    if ($rz) { $cmdArgs += @("--speak", $rz) }

    foreach ($cand in @((Join-Path $Dir "kartinki$n"), (Join-Path $proj "kartinki$n"))) {
        if (Test-Path -LiteralPath $cand) { $cmdArgs += @("--images", $cand); break }
    }

    $cmdArgs += @("--engine", "yandex3", "--voice", $Voice, "--before", "0", "--pad", "0.3")

    Write-Host ""
    Write-Host "================== БИЛЕТ $n ==================" -ForegroundColor Cyan
    Write-Host ("файл: " + $bilet) -ForegroundColor DarkGray
    python auto_quiz.py @cmdArgs
    if ($LASTEXITCODE -eq 0) { $done++ } else { $skip++; Write-Host "!! Билет $n с ошибкой" -ForegroundColor Red }

    if ($Cooldown -gt 0 -and $n -lt $To) {
        $mins = [math]::Round($Cooldown / 60, 1)
        Write-Host ("...пауза на остывание $Cooldown сек (~$mins мин, Ctrl+C чтобы прервать)") -ForegroundColor DarkYellow
        Start-Sleep -Seconds $Cooldown
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "ГОТОВО. Собрано: $done, пропущено/с ошибкой: $skip" -ForegroundColor Green
Write-Host "Все видео здесь: $out" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green

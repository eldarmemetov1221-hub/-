# ============================================================
#  АВТО-СБОРКА БИЛЕТОВ ГОЛОСОМ ANTON (yandex3) — ПАЧКОЙ
#  Запускаешь один раз -> идёт по билетам сам -> уходишь.
#  Остановить в любой момент: Ctrl + C.
#  Готовые видео -> Рабочий стол\пдд готовые
#
#  Что нужно в папке проекта по каждому билету N:
#    bilet<N>.txt   — вопросы билета (обязательно)
#    kartinki<N>    — папка с картинками (если есть)
#
#  Разбор (твой текст для голоса) — на выбор:
#    а) один общий файл, разбитый метками «### БИЛЕТ N» (параметр -Razbor),
#    б) или отдельные razbor<N>.txt рядом (если -Razbor не задан).
#  Без разбора голос прочитает пояснение из самого bilet<N>.txt.
#
#  Запуск (пример):
#    powershell -ExecutionPolicy Bypass -File .\собрать_билеты.ps1 -From 1 -To 10 -Razbor .\разбор_1-10.txt
# ============================================================

param(
    [int]$From = 1,
    [int]$To   = 10,
    [string]$Razbor = "",
    [string]$Voice  = "anton",
    [string]$Speed  = ""        # например 0.95 (медленнее) или 1.05 (быстрее)
)

$ErrorActionPreference = "Stop"
$proj = $PSScriptRoot
Set-Location $proj

if ($Speed -ne "") { $env:YANDEX_ROLE = $env:YANDEX_ROLE; $env:YANDEX_SPEED = $Speed }

$out = "$env:USERPROFILE\Desktop\пдд готовые"
New-Item -ItemType Directory -Force -Path $out | Out-Null

# --- если дан общий файл разбора — делим его по меткам «### БИЛЕТ N» ---
$split = @{}   # N -> путь к временному файлу разбора
if ($Razbor -ne "") {
    if (-not (Test-Path $Razbor)) { Write-Host "Файл разбора не найден: $Razbor" -ForegroundColor Red; exit 1 }
    $tmp = Join-Path $env:TEMP ("razbor_split_" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $cur = $null; $buf = New-Object System.Collections.Generic.List[string]
    foreach ($line in Get-Content -LiteralPath $Razbor -Encoding UTF8) {
        $m = [regex]::Match($line, '^\s*#{0,3}\s*БИЛЕТ\s+(\d+)', 'IgnoreCase')
        if ($m.Success) {
            if ($cur -ne $null -and $buf.Count -gt 0) {
                $f = Join-Path $tmp "razbor$cur.txt"; Set-Content -LiteralPath $f -Value $buf -Encoding UTF8; $split[$cur] = $f
            }
            $cur = [int]$m.Groups[1].Value; $buf = New-Object System.Collections.Generic.List[string]
        } elseif ($cur -ne $null) { $buf.Add($line) }
    }
    if ($cur -ne $null -and $buf.Count -gt 0) {
        $f = Join-Path $tmp "razbor$cur.txt"; Set-Content -LiteralPath $f -Value $buf -Encoding UTF8; $split[$cur] = $f
    }
    Write-Host ("Разбор поделён по билетам: " + ($split.Keys | Sort-Object) -join ", ") -ForegroundColor DarkCyan
}

$done = 0; $skip = 0
for ($n = $From; $n -le $To; $n++) {
    $bilet = Join-Path $proj "bilet$n.txt"
    if (-not (Test-Path $bilet)) { Write-Host "-- билет $n: нет bilet$n.txt, пропускаю" -ForegroundColor DarkGray; continue }

    $cmdArgs = @($bilet, (Join-Path $out "bilet$n.mp4"))

    # разбор: из общего файла, иначе razbor<N>.txt рядом
    $rz = $null
    if ($split.ContainsKey($n)) { $rz = $split[$n] }
    elseif (Test-Path (Join-Path $proj "razbor$n.txt")) { $rz = Join-Path $proj "razbor$n.txt" }
    if ($rz) { $cmdArgs += @("--speak", $rz) }

    $imgs = Join-Path $proj "kartinki$n"
    if (Test-Path $imgs) { $cmdArgs += @("--images", $imgs) }

    $cmdArgs += @("--engine", "yandex3", "--voice", $Voice, "--before", "0", "--pad", "0.3")

    Write-Host ""
    Write-Host "================== БИЛЕТ $n ==================" -ForegroundColor Cyan
    python auto_quiz.py @cmdArgs
    if ($LASTEXITCODE -eq 0) { $done++ } else { $skip++; Write-Host "!! Билет $n с ошибкой" -ForegroundColor Red }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "ГОТОВО. Собрано: $done, с ошибками/пропущено: $skip" -ForegroundColor Green
Write-Host "Все видео здесь: $out" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green

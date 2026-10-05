# ============================================================
#  АВТО-ПЕРЕСБОРКА ВСЕХ БИЛЕТОВ ГОЛОСОМ ANTON (yandex3)
#  Запускаешь один раз -> идёт сам по всем билетам -> уходишь.
#  Готовые видео кладёт в: Рабочий стол\пдд_anton
#
#  Что ищет по каждому билету N (1..30) в папке проекта:
#    bilet<N>.txt     — вопросы билета (обязательно)
#    kartinki<N>      — папка с картинками (если есть)
#  И твой разбор на Рабочем столе:
#    razbor<N>.txt    — текст для --speak (если есть; без него озвучит
#                       пояснение из самого bilet<N>.txt)
#
#  Запуск:
#    cd <папка проекта>
#    powershell -ExecutionPolicy Bypass -File .\пересобрать_все.ps1
# ============================================================

$proj = $PSScriptRoot
Set-Location $proj

$out = "$env:USERPROFILE\Desktop\пдд_anton"
New-Item -ItemType Directory -Force -Path $out | Out-Null

$desk = [Environment]::GetFolderPath("Desktop")
$done = 0
$skip = 0

for ($n = 1; $n -le 30; $n++) {
    $bilet = Join-Path $proj "bilet$n.txt"
    if (-not (Test-Path $bilet)) { continue }

    $args = @($bilet, (Join-Path $out "bilet$n.mp4"))

    $razbor = Join-Path $desk "razbor$n.txt"
    if (Test-Path $razbor) { $args += @("--speak", $razbor) }

    $imgs = Join-Path $proj "kartinki$n"
    if (Test-Path $imgs) { $args += @("--images", $imgs) }

    $args += @("--engine", "yandex3", "--voice", "anton", "--before", "0", "--pad", "0.3")

    Write-Host ""
    Write-Host "================== БИЛЕТ $n ==================" -ForegroundColor Cyan
    python auto_quiz.py @args
    if ($LASTEXITCODE -eq 0) { $done++ } else { $skip++ ; Write-Host "!! Билет $n с ошибкой" -ForegroundColor Red }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "ГОТОВО. Собрано: $done, с ошибками: $skip" -ForegroundColor Green
Write-Host "Все видео здесь: $out" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green

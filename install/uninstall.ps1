<#
.SYNOPSIS
Deinstalator ByteTech Agent.
Zatrzymuje usługę, usuwa Scheduled Task, opcjonalnie usuwa pliki.
#>

$ErrorActionPreference = "Continue"

$InstallDir = "C:\ByteTechAgent"
$TaskName = "ByteTechAgent"

Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  ByteTech Agent - Deinstalacja" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Zatrzymanie i usunięcie Scheduled Task
Write-Host "[1/3] Zatrzymywanie Scheduled Task..." -ForegroundColor Yellow
try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "  [OK] Scheduled Task '$TaskName' usuniety." -ForegroundColor Green
    } else {
        Write-Host "  [INFO] Scheduled Task '$TaskName' nie istnieje." -ForegroundColor Gray
    }
} catch {
    Write-Host "  [WARN] Blad przy usuwaniu Scheduled Task: $_" -ForegroundColor Yellow
}

# 2. Zatrzymanie procesów
Write-Host "[2/3] Zatrzymywanie procesow agenta..." -ForegroundColor Yellow
try {
    $procs = Get-Process -Name "python*" -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -like "*ByteTechAgent*"
    }
    if ($procs) {
        $procs | Stop-Process -Force -ErrorAction SilentlyContinue
        Write-Host "  [OK] Zatrzymano $($procs.Count) procesow." -ForegroundColor Green
    } else {
        Write-Host "  [INFO] Brak aktywnych procesow agenta." -ForegroundColor Gray
    }
} catch {
    Write-Host "  [WARN] Blad przy zatrzymywaniu procesow: $_" -ForegroundColor Yellow
}

# 3. Usuwanie plików
Write-Host "[3/3] Usuwanie plikow..." -ForegroundColor Yellow

if (Test-Path -Path $InstallDir) {
    $keepConfig = Read-Host "  Zachowac konfiguracje i logi? (T/N, domyslnie T)"

    if ($keepConfig -eq 'N' -or $keepConfig -eq 'n') {
        Write-Host "  Usuwanie $InstallDir ze wszystkim..." -ForegroundColor Red
        Remove-Item -Path $InstallDir -Recurse -Force
        Write-Host "  [OK] Katalog usuniety." -ForegroundColor Green
    } else {
        Write-Host "  Usuwanie programu, zachowuje config.yaml, logs/ i spool/..." -ForegroundColor Yellow
        Get-ChildItem -Path $InstallDir -Exclude "config.yaml", "logs", "spool" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  [OK] Pliki programu usuniete. Config i logi zachowane w $InstallDir" -ForegroundColor Green
    }
} else {
    Write-Host "  [INFO] Katalog $InstallDir nie istnieje." -ForegroundColor Gray
}

Write-Host ""
Write-Host "  Deinstalacja zakonczona." -ForegroundColor Green
Write-Host ""

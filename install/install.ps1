<#
.SYNOPSIS
ByteTech Agent Installer - modular PC monitoring agent.
Automates setup: directory creation, python environment (venv),
automatic Python 3.10+ check/install (via winget), dependency installation,
service registration, and interactive/default config generation.
Also detects hardware backends (LHM WMI/JSON API) and PresentMon.

.DESCRIPTION
Run as Administrator:
    .\install\install.ps1
#>

$ErrorActionPreference = "Stop"

$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $SourceDir
$InstallDir = "C:\ByteTechAgent"

function Write-Title($text) {
    Write-Host ""
    Write-Host "========================================================" -ForegroundColor Cyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host "========================================================" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Step($step, $text) {
    Write-Host "[$step] $text" -ForegroundColor Yellow
}

function Write-Ok($text) {
    Write-Host "  [OK] $text" -ForegroundColor Green
}

function Write-Fail($text) {
    Write-Host "  [FAIL] $text" -ForegroundColor Red
}

function Write-Info($text) {
    Write-Host "  [INFO] $text" -ForegroundColor Gray
}

# Status tracking
$status = @{
    config_generated = $false
    influx_connection = $false
    influx_test_write = $false
    lhm_detected_wmi = $false
    lhm_detected_json = $false
    presentmon_detected = $false
    python_ok = $false
    venv_ok = $false
    deps_ok = $false
    task_registered = $false
}

Write-Title "ByteTech Agent Installer"

# ========================= STEP 1: Directory Setup =========================
Write-Step "1/9" "Preparing installation directory..."

if (-not (Test-Path -Path $InstallDir)) {
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Write-Ok "Created $InstallDir"
} else {
    Write-Info "Directory $InstallDir already exists. Files will be overwritten."
}

# ========================= STEP 2: Copying Files =========================
Write-Step "2/9" "Copying project files..."

Copy-Item -Path "$ProjectRoot\bytetech_agent" -Destination $InstallDir -Recurse -Force
Copy-Item -Path "$ProjectRoot\requirements.txt" -Destination $InstallDir -Force
Copy-Item -Path "$ProjectRoot\examples" -Destination $InstallDir -Recurse -Force
Copy-Item -Path "$ProjectRoot\install" -Destination $InstallDir -Recurse -Force
Copy-Item -Path "$ProjectRoot\pyproject.toml" -Destination $InstallDir -Force -ErrorAction SilentlyContinue

Write-Ok "Files copied to $InstallDir"

# ========================= STEP 3: Python and Venv =========================
Write-Step "3/9" "Checking Python environment..."

$pythonCmd = $null

# Check for Python in path
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python 3\.1[0-9]|Python 3\.[2-9][0-9]") {
            $pythonCmd = $cmd
            Write-Ok "Found $ver ($cmd)"
            $status.python_ok = $true
            break
        }
    } catch {}
}

# Install Python via winget if missing
if (-not $pythonCmd) {
    Write-Info "Python 3.10+ not found. Attempting automatic installation via winget..."
    try {
        & winget install -e --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements --silent 2>&1 | Out-Null
        # Try to locate the new installation by refreshing env vars
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        foreach ($cmd in @("python", "python3", "py")) {
            try {
                $ver = & $cmd --version 2>&1
                if ($ver -match "Python 3\.1[0-9]|Python 3\.[2-9][0-9]") {
                    $pythonCmd = $cmd
                    Write-Ok "Successfully installed $ver ($cmd)"
                    $status.python_ok = $true
                    break
                }
            } catch {}
        }
        
        if (-not $pythonCmd) {
            Write-Fail "Python installation succeeded but command not found. You might need to restart your terminal/PC."
            exit 1
        }
    } catch {
        Write-Fail "Automatic installation failed. Please install Python 3.10+ manually from python.org."
        exit 1
    }
}

Set-Location -Path $InstallDir
Write-Info "Creating virtual environment..."
& $pythonCmd -m venv venv 2>&1 | Out-Null
if ($?) {
    Write-Ok "Virtual environment created"
    $status.venv_ok = $true
} else {
    Write-Fail "Failed to create venv"
    exit 1
}

Write-Info "Installing dependencies (may take a moment)..."
.\venv\Scripts\python.exe -m pip install --upgrade pip 2>&1 | Out-Null
.\venv\Scripts\pip.exe install -e . 2>&1 | Out-Null
if (-not $?) {
    Write-Info "Fallback to requirements.txt..."
    .\venv\Scripts\pip.exe install -r requirements.txt 2>&1 | Out-Null
}

if ($?) {
    Write-Ok "Dependencies installed"
    $status.deps_ok = $true
} else {
    Write-Fail "Dependency installation failed"
}

# ========================= STEP 4: Interactive Config =========================
Write-Step "4/9" "Agent configuration..."

$ConfigDestination = "$InstallDir\config.yaml"
$needsConfig = $true

if (Test-Path -Path $ConfigDestination) {
    $overwrite = Read-Host "  config.yaml already exists. Overwrite? (Y/N, default Y)"
    if ($overwrite -eq 'N' -or $overwrite -eq 'n') {
        $needsConfig = $false
        Write-Info "Kept existing config.yaml"
    }
}

if ($needsConfig) {
    Write-Host ""
    Write-Host "  Enter InfluxDB connection details:" -ForegroundColor White

    $influx_host = Read-Host "    InfluxDB host/IP (e.g. 192.168.1.100 or localhost)"
    if (-not $influx_host) { $influx_host = "localhost" }
    
    $influx_port = Read-Host "    InfluxDB port (default 8086)"
    if (-not $influx_port) { $influx_port = "8086" }
    
    $influx_org = Read-Host "    InfluxDB org (default my-org)"
    if (-not $influx_org) { $influx_org = "my-org" }
    
    $influx_bucket = Read-Host "    InfluxDB bucket (default metrics)"
    if (-not $influx_bucket) { $influx_bucket = "metrics" }
    
    $influx_token = Read-Host "    InfluxDB API token"

    Write-Host ""
    Write-Host "  Enter host identification details:" -ForegroundColor White
    $host_alias = Read-Host "    Host alias (default $($env:COMPUTERNAME))"
    if (-not $host_alias) { $host_alias = $env:COMPUTERNAME }
    
    $site = Read-Host "    Site/Location (default Home)"
    if (-not $site) { $site = "Home" }
    
    $owner = Read-Host "    Owner (default $($env:USERNAME))"
    if (-not $owner) { $owner = $env:USERNAME }
    
    Write-Host ""
    Write-Host "  LHM JSON API Backend (Optional fallback):" -ForegroundColor White
    $lhm_json_url = Read-Host "    LHM JSON API URL (default http://127.0.0.1:8085)"
    if (-not $lhm_json_url) { $lhm_json_url = "http://127.0.0.1:8085" }

    Write-Host ""
    Write-Host "  Optional providers:" -ForegroundColor White
    $nvapi_enabled = Read-Host "    Enable NVAPI (NVIDIA)? (Y/N, default Y)"
    if ($nvapi_enabled -eq 'N' -or $nvapi_enabled -eq 'n') { $nvapi_val = "false" } else { $nvapi_val = "true" }
    
    $display_enabled = Read-Host "    Enable Display Provider? (Y/N, default Y)"
    if ($display_enabled -eq 'N' -or $display_enabled -eq 'n') { $display_val = "false" } else { $display_val = "true" }
    
    $presentmon_enabled = Read-Host "    Enable PresentMon (FPS)? (Y/N, default Y)"
    if ($presentmon_enabled -eq 'N' -or $presentmon_enabled -eq 'n') { $pm_val = "false" } else { $pm_val = "true" }

    $influx_url = "http://${influx_host}:${influx_port}"

    $configContent = @"
# ByteTech Agent Configuration
# Generated by installer $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")

influx:
  url: "$influx_url"
  token: "$influx_token"
  org: "$influx_org"
  bucket: "$influx_bucket"

metadata:
  host_alias: "$host_alias"
  site: "$site"
  owner: "$owner"

timing:
  hw_interval_sec: 10
  fps_interval_sec: 2

providers:
  lhm_enabled: true
  presentmon_enabled: $pm_val
  display_provider_enabled: $display_val
  nvapi_provider_enabled: $nvapi_val
  system_provider_enabled: true

lhm:
  json_url: "$lhm_json_url"

presentmon:
  target_mode: "active_foreground"
  process_name: ""
  process_id: 0

logging:
  level: "INFO"
  log_dir: "logs"

buffer:
  enabled: true
  max_memory_points: 10000
  spool_dir: "spool"
  max_spool_files: 50

options:
  tags_extra:
    device_class: "desktop"
  custom_fields: {}
  retention_hint_days: 30
"@
    Set-Content -Path $ConfigDestination -Value $configContent -Encoding UTF8
    Write-Ok "config.yaml generated"
    $status.config_generated = $true
} else {
    $status.config_generated = $true
}

# ========================= STEP 5: LibreHardwareMonitor Check =========================
Write-Step "5/9" "Checking LibreHardwareMonitor dependencies..."

try {
    $wmiTest = Get-WmiObject -Namespace "root\LibreHardwareMonitor" -Class Sensor -ErrorAction Stop | Select-Object -First 1
    if ($wmiTest) {
        Write-Ok "LibreHardwareMonitor WMI available (namespace: root\LibreHardwareMonitor)"
        $status.lhm_detected_wmi = $true
    }
} catch {
    try {
        $wmiTest2 = Get-WmiObject -Namespace "root\OpenHardwareMonitor" -Class Sensor -ErrorAction Stop | Select-Object -First 1
        if ($wmiTest2) {
            Write-Ok "OpenHardwareMonitor WMI available (namespace: root\OpenHardwareMonitor)"
            $status.lhm_detected_wmi = $true
        }
    } catch {
        Write-Info "WMI backend not detected. Checking JSON API fallback..."
        
        # Test JSON API Backend
        try {
            $jsonUrl = "http://127.0.0.1:8085/data.json"
            if (Test-Path -Path $ConfigDestination) {
                # Attempt to extract json_url from config using simple regex
                $content = Get-Content $ConfigDestination -Raw
                if ($content -match 'json_url:\s*"([^"]+)"') {
                    $jsonUrl = $matches[1] + "/data.json"
                    $jsonUrl = $jsonUrl.Replace("//data.json", "/data.json")
                }
            }
            
            $jsonResp = Invoke-RestMethod -Uri $jsonUrl -Method Get -TimeoutSec 3 -ErrorAction Stop
            if ($jsonResp.Text -match "Sensor") {
                Write-Ok "LHM JSON API available at ${jsonUrl}"
                $status.lhm_detected_json = $true
            } else {
                throw "No sensor data in JSON"
            }
        } catch {
            Write-Fail "No LHM backend (WMI/JSON) detected."
            Write-Info "Ensure LibreHardwareMonitor is running and WMI or Web Server is enabled."
        }
    }
}

# ========================= STEP 6: PresentMon Check =========================
Write-Step "6/9" "Checking PresentMon dependencies..."

$pmFound = $false
$pmPaths = @(
    "$env:ProgramFiles\PresentMon\PresentMonAPI2.dll",
    "$env:ProgramFiles\PresentMon\PresentMonAPI.dll",
    "${env:ProgramFiles(x86)}\PresentMon\PresentMonAPI2.dll",
    "$env:LOCALAPPDATA\PresentMon\PresentMonAPI2.dll",
    "$InstallDir\PresentMonAPI2.dll",
    "$InstallDir\PresentMonAPI.dll"
)

foreach ($p in $pmPaths) {
    if (Test-Path -Path $p) {
        Write-Ok "PresentMon API found: $p"
        $pmFound = $true
        $status.presentmon_detected = $true
        break
    }
}

if (-not $pmFound) {
    # Check if PresentMon Service is running
    $pmService = Get-Service -Name "PresentMon*" -ErrorAction SilentlyContinue
    if ($pmService) {
        Write-Ok "PresentMon Service found: $($pmService.DisplayName)"
        $status.presentmon_detected = $true
    } else {
        Write-Fail "PresentMon API/Service not detected."
        Write-Info "Agent will run without FPS metrics or fallback to generic ETW."
    }
}

# ========================= STEP 7: InfluxDB Connection Test =========================
Write-Step "7/9" "Testing InfluxDB connection..."

if ($status.config_generated -and $status.deps_ok) {
    try {
        $testScript = @"
import sys
sys.path.insert(0, r'$InstallDir')
from bytetech_agent.config import load_config
from influxdb_client import InfluxDBClient

config = load_config(r'$ConfigDestination')
client = InfluxDBClient(url=config.influx.url, token=config.influx.token, org=config.influx.org, timeout=10000)

# Health check
health = client.health()
print(f'HEALTH:{health.status}')

# Test write
from influxdb_client import Point
from influxdb_client.client.write_api import SYNCHRONOUS
write_api = client.write_api(write_options=SYNCHRONOUS)
test_point = Point('bytetech_test').tag('test', 'installer').field('value', 1)
write_api.write(bucket=config.influx.bucket, record=test_point)
print('WRITE:OK')

client.close()
"@
        $testResult = $testScript | .\venv\Scripts\python.exe - 2>&1

        if ($testResult -match "HEALTH:pass") {
            Write-Ok "InfluxDB connection: OK"
            $status.influx_connection = $true
        } else {
            Write-Fail "InfluxDB health check: $testResult"
        }

        if ($testResult -match "WRITE:OK") {
            Write-Ok "InfluxDB test write: OK"
            $status.influx_test_write = $true
        } else {
            Write-Fail "InfluxDB test write failed"
        }
    } catch {
        Write-Fail "Connection test error: $_"
    }
} else {
    Write-Info "Skipping connection test (missing config or dependencies)"
}

# ========================= STEP 8: Task Registration =========================
Write-Step "8/9" "Registering Scheduled Task..."

$TaskName = "ByteTechAgent"
try {
    $Action = New-ScheduledTaskAction `
        -Execute "$InstallDir\venv\Scripts\python.exe" `
        -Argument "-m bytetech_agent" `
        -WorkingDirectory $InstallDir

    $Trigger = New-ScheduledTaskTrigger -AtStartup

    $Principal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Highest

    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Days 9999) `
        -Priority 4 `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)

    Register-ScheduledTask `
        -Action $Action `
        -Trigger $Trigger `
        -Principal $Principal `
        -Settings $Settings `
        -TaskName $TaskName `
        -Description "ByteTech PC Monitoring Agent" `
        -Force | Out-Null

    Write-Ok "Registered Scheduled Task: $TaskName"
    $status.task_registered = $true

    Write-Info "Starting agent..."
    Start-ScheduledTask -TaskName $TaskName
    Write-Ok "Agent started"
} catch {
    Write-Fail "Error registering Scheduled Task: $_"
}

# ========================= STEP 9: Summary =========================
Write-Step "9/9" "Installation Summary"

Write-Host ""
Write-Title "Installation Status"

$statusItems = @(
    @("Configuration Generated", [string]$status.config_generated),
    @("Python & Venv Setup", [string]($status.python_ok -and $status.venv_ok)),
    @("Dependencies Installed", [string]$status.deps_ok),
    @("InfluxDB Connectivity", [string]$status.influx_connection),
    @("InfluxDB Write Test", [string]$status.influx_test_write),
    @("LHM Detected (WMI or JSON)", [string]($status.lhm_detected_wmi -or $status.lhm_detected_json)),
    @("PresentMon Detected", [string]$status.presentmon_detected),
    @("Scheduled Task Registered", [string]$status.task_registered)
)

foreach ($item in $statusItems) {
    # PadRight fix: Ensure argument is cast to string before padding
    $label = [string]$item[0]
    $label = $label.PadRight(30)
    
    $val = [string]$item[1]
    
    if ($val -eq "True") {
        Write-Host "  $label : " -NoNewline; Write-Host "OK" -ForegroundColor Green
    } else {
        Write-Host "  $label : " -NoNewline; Write-Host "MISSING/ERROR" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "  Install Directory  : $InstallDir" -ForegroundColor Gray
Write-Host "  Configuration      : $ConfigDestination" -ForegroundColor Gray
Write-Host "  Logs               : $InstallDir\logs\" -ForegroundColor Gray
Write-Host ""
Write-Host "  Commands:" -ForegroundColor White
Write-Host "    Restart:   Restart-ScheduledTask -TaskName $TaskName" -ForegroundColor DarkGray
Write-Host "    Stop:      Stop-ScheduledTask -TaskName $TaskName" -ForegroundColor DarkGray
Write-Host "    Status:    Get-ScheduledTask -TaskName $TaskName" -ForegroundColor DarkGray
Write-Host "    Logs:      Get-Content $InstallDir\logs\bytetech_agent.log -Tail 50" -ForegroundColor DarkGray
Write-Host ""

if ($status.influx_connection -and $status.task_registered) {
    Write-Host "  INSTALLATION COMPLETED SUCCESSFULLY" -ForegroundColor Green
} else {
    Write-Host "  INSTALLATION COMPLETED WITH WARNINGS" -ForegroundColor Yellow
    Write-Host "  Please check the status above and manually resolve any issues." -ForegroundColor Yellow
}
Write-Host ""

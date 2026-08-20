[CmdletBinding()]
param(
    [string]$BindAddress = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$PrepareOnly
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot "venv\Scripts\python.exe"
$envFile = Join-Path $repoRoot ".env"

if (-not (Test-Path $python)) {
    throw "venv가 없습니다. 최초 한 번 .\scripts\setup_local.ps1 을 실행하세요."
}

if (-not (Test-Path $envFile)) {
    throw ".env가 없습니다. 최초 한 번 .\scripts\setup_local.ps1 을 실행하세요."
}

function Get-EnvValue {
    param([string]$Name)

    $line = Get-Content $envFile | Where-Object { $_ -match "^$([regex]::Escape($Name))=" } | Select-Object -First 1
    if ($null -eq $line) {
        return $null
    }
    return $line.Substring($Name.Length + 1).Trim()
}

function Test-TcpPort {
    param(
        [string]$ComputerName,
        [int]$Port
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($ComputerName, $Port)
        if (-not $task.Wait(1500)) {
            return $false
        }
        $task.GetAwaiter().GetResult()
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

$dbHost = Get-EnvValue "POSTGRES_HOST"
$dbPort = Get-EnvValue "POSTGRES_PORT"
if ([string]::IsNullOrWhiteSpace($dbHost)) { $dbHost = "localhost" }
if ([string]::IsNullOrWhiteSpace($dbPort)) { $dbPort = "5432" }

if (-not (Test-TcpPort -ComputerName $dbHost -Port ([int]$dbPort))) {
    throw "PostgreSQL(${dbHost}:$dbPort)에 연결할 수 없습니다. 최초 실행이면 docker compose up -d 또는 PostgreSQL 서비스를 먼저 실행하세요."
}

Push-Location $repoRoot
try {
    & $python manage.py migrate --noinput
    if ($LASTEXITCODE -ne 0) { throw "마이그레이션 적용에 실패했습니다." }

    $boardCheck = & $python manage.py shell -c "from apps.board.models import Cell; print(f'BOARD_CELL_COUNT={Cell.objects.count()}')"
    if ($LASTEXITCODE -ne 0) { throw "보드 초기 상태를 확인하지 못했습니다." }

    $countLine = $boardCheck | Where-Object { $_ -match "^BOARD_CELL_COUNT=" } | Select-Object -Last 1
    if ($null -eq $countLine) { throw "보드 초기 상태를 읽지 못했습니다." }
    $cellCount = [int]($countLine.Substring("BOARD_CELL_COUNT=".Length))

    if ($cellCount -eq 0) {
        & $python manage.py seed_board
        if ($LASTEXITCODE -ne 0) { throw "보드 초기 데이터 생성에 실패했습니다." }
    }
    elseif ($cellCount -ne 36) {
        throw "보드 칸 수가 $cellCount개입니다. 0개 또는 36개여야 합니다. 데이터를 확인하세요."
    }

    if (-not $PrepareOnly) {
        & $python manage.py runserver "${BindAddress}:$Port"
    }
}
finally {
    Pop-Location
}

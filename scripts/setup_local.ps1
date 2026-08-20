$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $repoRoot "venv"
$python = Join-Path $venvPath "Scripts\python.exe"
$envFile = Join-Path $repoRoot ".env"
$envExample = Join-Path $repoRoot ".env.example"

if (-not (Test-Path $python)) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -eq $launcher) {
        throw "Python 3.12가 필요합니다. 설치 후 .\scripts\setup_local.ps1 을 다시 실행하세요."
    }

    & py -3.12 -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 가상환경 생성에 실패했습니다." }
}

Push-Location $repoRoot
try {
    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "패키지 설치에 실패했습니다." }

    if (-not (Test-Path $envFile)) {
        Copy-Item $envExample $envFile
        $djangoSecret = & $python -c "import secrets; print(secrets.token_urlsafe(50))"
        $jwtSecret = & $python -c "import secrets; print(secrets.token_urlsafe(48))"
        $content = Get-Content $envFile -Raw
        $content = $content -replace "(?m)^DJANGO_SECRET_KEY=$", "DJANGO_SECRET_KEY=$djangoSecret"
        $content = $content -replace "(?m)^JWT_SECRET=$", "JWT_SECRET=$jwtSecret"
        Set-Content -LiteralPath $envFile -Value $content -NoNewline
    }

    & (Join-Path $PSScriptRoot "run_local.ps1") -PrepareOnly
}
finally {
    Pop-Location
}

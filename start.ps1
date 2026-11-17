$ErrorActionPreference = 'Stop'
$projectPath = $PSScriptRoot
Set-Location -LiteralPath $projectPath
if (-not (Test-Path -LiteralPath '.env.local')) { throw 'Создайте .env.local по образцу .env.example' }
if (-not (Test-Path -LiteralPath '.next\standalone\server.js')) { throw 'Сначала выполните npm.cmd run build' }
New-Item -ItemType Directory -Force -Path 'logs','data' | Out-Null
& (Join-Path $projectPath '.venv\Scripts\python.exe') -m backend.migrate
if ($LASTEXITCODE -ne 0) { throw 'Миграция базы данных не выполнена' }
$services = @(
    @{ Name='api'; File=(Join-Path $projectPath '.venv\Scripts\python.exe'); Arguments=@('-m','uvicorn','backend.app:app','--host','127.0.0.1','--port','8100') },
    @{ Name='worker'; File=(Join-Path $projectPath '.venv\Scripts\python.exe'); Arguments=@('-m','backend.worker') },
    @{ Name='web'; File='node.exe'; Arguments=@('scripts/serve.mjs') }
)
$started = @()
foreach ($service in $services) {
    $pidPath = Join-Path $projectPath "data\$($service.Name).pid"
    if (Test-Path -LiteralPath $pidPath) {
        $rawPid = (Get-Content -LiteralPath $pidPath -Raw).Trim()
        if ($rawPid -match '^\d+$') {
            $servicePid = [int]$rawPid
            $existing = Get-CimInstance Win32_Process -Filter "ProcessId = $servicePid"
            if ($existing) {
                $expected = if ($service.Name -eq 'api') {'backend.app:app'} elseif ($service.Name -eq 'worker') {'backend.worker'} else {'scripts/serve.mjs'}
                if ($existing.CommandLine -notlike "*$expected*") { throw "PID $servicePid принадлежит другому процессу; проверьте $pidPath" }
                Write-Host "$($service.Name) уже работает (PID $servicePid)"; continue
            }
        }
    }
    $process = Start-Process -FilePath $service.File -ArgumentList $service.Arguments -WorkingDirectory $projectPath -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectPath "logs\$($service.Name).stdout.log") -RedirectStandardError (Join-Path $projectPath "logs\$($service.Name).stderr.log")
    $process.Id | Set-Content -LiteralPath $pidPath
    Start-Sleep -Milliseconds 300
    if ($process.HasExited) { throw "$($service.Name) не запустился. Смотрите logs\$($service.Name).stderr.log" }
    $started += $process.Id
    Write-Host "$($service.Name): PID $($process.Id)"
}
Write-Host 'Venture Lens: http://127.0.0.1:3100'
Write-Host 'API: http://127.0.0.1:8100/docs'

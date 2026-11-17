$ErrorActionPreference = 'Stop'
$projectPath = [IO.Path]::GetFullPath($PSScriptRoot)
foreach ($name in @('api','worker','web')) {
    $pidPath = Join-Path $projectPath "data\$name.pid"
    if (-not (Test-Path -LiteralPath $pidPath)) { continue }
    $servicePid = [int](Get-Content -LiteralPath $pidPath)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $servicePid"
    if (-not $process) { continue }
    $expected = if ($name -eq 'api') {'backend.app:app'} elseif ($name -eq 'worker') {'backend.worker'} else {'scripts/serve.mjs'}
    if ($process.CommandLine -notlike "*$expected*") { throw "PID $servicePid не принадлежит сервису $name; остановка отменена" }
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $servicePid"
    foreach ($child in $children) {
        if ($child.CommandLine -match 'backend\.(worker|app)' -or $child.CommandLine -match 'standalone[/\\]server.js') { Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue }
    }
    Stop-Process -Id $servicePid -ErrorAction SilentlyContinue
    Write-Host "Остановлен $name"
}

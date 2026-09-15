param([string]$Config = '.runtime/client.json')
$ErrorActionPreference = 'Stop'
$KitDir = Split-Path -Parent $PSScriptRoot
Push-Location $KitDir
try {
    $Branch = & git branch --show-current
    if ($LASTEXITCODE -ne 0 -or $Branch -ne 'main') { throw '请先切换到 main 分支。' }
    $Changes = & git status --porcelain
    if ($LASTEXITCODE -ne 0 -or $Changes) { throw '源码存在本地改动，请先保存。更新不会覆盖它们。' }
    & git pull --ff-only origin main
    if ($LASTEXITCODE -ne 0) { throw '拉取未完成，请解决上方 Git 提示后重试。' }
    & python agent_board.py network --config $Config upgrade
    if ($LASTEXITCODE -ne 0) { throw '配置升级未完成，请保留备份并检查上方错误。' }
    & python -m unittest test_board_network test_collaboration test_network_runtime test_coordination -q
    if ($LASTEXITCODE -ne 0) { throw '检查未通过，请暂缓启动并保留错误信息。' }
    Write-Host '更新完成。运行 scripts/client.ps1 启动客户端，再运行 scripts/open.ps1 打开中文界面。'
} finally { Pop-Location }

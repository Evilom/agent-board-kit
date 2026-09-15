param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference = 'Stop'
$KitDir = Split-Path -Parent $PSScriptRoot
& python (Join-Path $KitDir 'agent_board.py') network --config $Config open
exit $LASTEXITCODE

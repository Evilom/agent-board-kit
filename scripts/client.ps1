param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference = 'Stop'
$KitDir = Split-Path -Parent $PSScriptRoot
# The client runs Dagu Worker only; task and knowledge requests go to the server.
& python (Join-Path $KitDir 'agent_board.py') network --config $Config service
exit $LASTEXITCODE

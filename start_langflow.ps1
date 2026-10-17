[CmdletBinding()]
param([ValidateRange(1024,65535)][int]$Port = 7860)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:IWT_PROJECT_ROOT = $PSScriptRoot
$env:IWT_WRITER = 'portable'
# Keep this demo separate from other Langflow databases and saved credentials.
$env:LANGFLOW_CONFIG_DIR = Join-Path $PSScriptRoot '.langflow'
$env:LANGFLOW_DATABASE_URL = 'sqlite:///' + ((Join-Path $PSScriptRoot '.langflow\langflow.db') -replace '\\','/')
[System.IO.Directory]::CreateDirectory($env:LANGFLOW_CONFIG_DIR) | Out-Null
Push-Location $PSScriptRoot
try {
    python -m langflow run --host 127.0.0.1 --port $Port
} finally {
    Pop-Location
}

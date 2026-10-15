param(
    [string]$PythonExe = 'python',
    [Parameter(Mandatory=$true)][string]$CatalogPath,
    [string]$ArtifactRoot = '',
    [string]$LibraryRoot = '',
    [string]$RuntimeRoot = '',
    [int]$Port = 8946
)

# Uses an existing environment; no dependency or service setup.
$ErrorActionPreference = 'Stop'
$riffRcRepository = Split-Path -Parent $PSScriptRoot
if (-not $RuntimeRoot) {
    $RuntimeRoot = Join-Path (Split-Path -Parent $riffRcRepository) 'riff-unified-runtime'
}
$env:PYTHONPATH = Join-Path $riffRcRepository 'src'
Write-Host "Riff local release preview: http://127.0.0.1:$Port/"
Write-Host 'Cached packages and sandbox exports only. Real Songs remains read-only.'
Write-Host 'Press Ctrl+C to stop this server. The companion checkpoint is separate.'
$riffPreviewArguments = @('-u', '-m', 'csv_to_clonehero.unified_preview',
    '--catalog', $CatalogPath, '--runtime-root', $RuntimeRoot, '--port', $Port)
if ($LibraryRoot) { $riffPreviewArguments += @('--library-root', $LibraryRoot) }
if ($ArtifactRoot) { $riffPreviewArguments += @('--artifact-root', $ArtifactRoot) }
& $PythonExe @riffPreviewArguments

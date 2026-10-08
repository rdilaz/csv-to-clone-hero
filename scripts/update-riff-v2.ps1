<#
.SYNOPSIS
Checks or updates the existing Windows RIFF Compose service to the approved V2.
.DESCRIPTION
Requires Git and Docker Desktop running Linux containers on this computer.
The default and -CheckOnly are read-only. Only -Update fetches/checks out the
specified branch, builds clonehero, and recreates that service. No stash/reset,
volume deletion, prune, compose down, tunnel change, or credential write occurs.
An existing .env, one existing clonehero container, and its named /data volume
are required. Custom Compose overrides and remote Docker contexts fail closed.
Keep the printed original branch/commit/image ID for a manual rollback.
.EXAMPLE
PowerShell -NoProfile -File .\UPDATE-RIFF-V2.ps1 -CheckOnly
.EXAMPLE
PowerShell -NoProfile -File .\UPDATE-RIFF-V2.ps1 -Update -ExpectedCommit <40-character-reviewed-commit>
#>
[CmdletBinding()]
param(
    [string]$RepoPath = 'C:\dev\csv-to-clone-hero',
    [string]$Ref = 'riff/v2-20261008',
    [ValidatePattern('^$|^[0-9a-fA-F]{40}$')]
    [string]$ExpectedCommit = '',
    [switch]$CheckOnly,
    [switch]$Update,
    [ValidateRange(1, 600)]
    [int]$HealthTimeoutSeconds = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:RiffFailureMessage = ''
$script:CheckoutChanged = $false
$script:ServiceChanged = $false
$script:OriginalBranch = ''
$script:OriginalCommit = ''
$script:OriginalImage = ''
$Release = 'riff-v2-20261008'
$Experience = 'signal-check'

function Stop-Riff([string]$Message) {
    $script:RiffFailureMessage = $Message
    throw [System.InvalidOperationException]::new($Message)
}

function Invoke-QuietNative([string]$Command, [string[]]$Arguments, [string]$Failure, [int[]]$AllowedExitCodes = @(0)) {
    # Never forward Git/Docker output: origins, Compose config and errors may contain credentials.
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $global:LASTEXITCODE = 0
        $captured = @(& $Command @Arguments 2>&1)
        $code = $LASTEXITCODE
    } catch {
        Stop-Riff $Failure
    } finally {
        $ErrorActionPreference = $savedPreference
    }
    if ($AllowedExitCodes -notcontains $code) { Stop-Riff $Failure }
    return [pscustomobject]@{ Code = $code; Output = (($captured | ForEach-Object { $_.ToString() }) -join "`n").Trim() }
}

function Read-QuietJson([string]$Value, [string]$Label) {
    try { return ($Value | ConvertFrom-Json) }
    catch { Stop-Riff "$Label returned unreadable metadata. No raw output has been displayed." }
}

function Same-Path([string]$First, [string]$Second) {
    try {
        return [System.IO.Path]::GetFullPath($First).TrimEnd('\', '/') -ieq [System.IO.Path]::GetFullPath($Second).TrimEnd('\', '/')
    } catch { return $false }
}

function Is-LocalDockerPipe([string]$Value) {
    return $Value -match '^npipe:/{3,4}\./pipe/(docker_engine|dockerDesktopLinuxEngine)$'
}

function Assert-Origin([string]$Value) {
    if ($Value -match '^git@github\.com:rdilaz/csv-to-clone-hero(?:\.git)?$') { return }
    try {
        $uri = [System.Uri]$Value
        if ($uri.IsAbsoluteUri -and $uri.Host -ieq 'github.com' -and
            $uri.Scheme -in @('https', 'ssh') -and $uri.Query -eq '' -and $uri.Fragment -eq '' -and
            $uri.AbsolutePath.TrimEnd('/') -imatch '^/rdilaz/csv-to-clone-hero(?:\.git)?$') { return }
    } catch { }
    Stop-Riff 'The origin is not rdilaz/csv-to-clone-hero on GitHub. Check it privately; its URL has not been displayed.'
}

function Assert-Clean {
    $status = Invoke-QuietNative git @('-C', $RepoPath, 'status', '--porcelain=v1', '--untracked-files=no') 'Could not inspect tracked changes.'
    if ($status.Output) { Stop-Riff 'Tracked changes are present. Commit or preserve them yourself before updating; this helper never resets or stashes work.' }
}

function Assert-EnvUnchanged {
    if (-not (Test-Path -LiteralPath $EnvPath -PathType Leaf)) { Stop-Riff 'The original .env is missing. Restore it privately before continuing; this helper never creates keys.' }
    if ((Get-FileHash -LiteralPath $EnvPath -Algorithm SHA256).Hash -ne $EnvHash) {
        Stop-Riff 'The .env changed during this run. Stop and review it privately before continuing.'
    }
}

function Read-Container([string]$Id) {
    # A quote-free native argument preserves its exact shape under Windows PowerShell 5.
    $format = '[{{json .Name}},{{json .Image}},{{json .Config.Labels}},{{json .Mounts}},{{json .Config.Env}}]'
    $raw = Invoke-QuietNative docker @('inspect', $Id, '--format', $format) 'Could not inspect the existing clonehero container.'
    # Keep nested arrays and nulls as property values instead of enumerating them through a function return.
    $metadata = Read-QuietJson ('{"fields":' + $raw.Output + '}') 'Container inspection'
    $fields = $metadata.fields
    if ($fields -isnot [array] -or $fields.Count -ne 5) { Stop-Riff 'Container inspection returned an unexpected metadata shape.' }
    return [pscustomobject]@{ name = $fields[0]; image = $fields[1]; labels = $fields[2]; mounts = $fields[3]; environment = $fields[4] }
}

function Get-DataMount($Container) {
    $mounts = @($Container.mounts | Where-Object { $_.Destination -eq '/data' })
    if ($mounts.Count -ne 1 -or $mounts[0].Type -ne 'volume' -or -not $mounts[0].Name) {
        Stop-Riff 'clonehero must already use exactly one named volume at /data. Review the existing deployment before updating.'
    }
    return $mounts[0].Name
}

function Assert-Compose {
    Assert-EnvUnchanged
    $raw = Invoke-QuietNative docker ($ComposeArgs + @('config', '--format', 'json')) 'Compose validation failed. Check required values in .env privately; raw config and errors have been withheld.'
    $config = Read-QuietJson $raw.Output 'Compose validation'
    if ($config.name -ne $Project -or -not $config.services.PSObject.Properties['clonehero']) { Stop-Riff 'Compose no longer describes the existing clonehero project.' }
    $service = $config.services.clonehero
    $data = @($service.volumes | Where-Object { $_.target -eq '/data' })
    if ($data.Count -ne 1 -or $data[0].type -ne 'volume' -or -not $data[0].source) { Stop-Riff 'Compose would change the /data mount. No service update was attempted.' }
    $volumeDefinition = $config.volumes.PSObject.Properties[$data[0].source]
    if (-not $volumeDefinition -or $volumeDefinition.Value.name -ne $DataVolume) { Stop-Riff 'Compose would use a different data volume. No service update was attempted.' }
    if ($service.PSObject.Properties['container_name']) {
        if ($service.container_name -ne $ContainerName) { Stop-Riff 'Compose would change the existing container name.' }
    } elseif ($ContainerName -ne $GeneratedContainerName) { Stop-Riff 'The existing container name is custom. Preserve its Compose configuration manually before updating.' }
    if (-not $service.PSObject.Properties['build'] -or -not (Same-Path $service.build.context $RepoPath)) { Stop-Riff 'clonehero must build from this repository.' }
    $ports = @($service.ports)
    if ($ports.Count -ne 1 -or $ports[0].host_ip -ne '127.0.0.1' -or [string]$ports[0].published -ne '8000' -or [int]$ports[0].target -ne 8000) {
        Stop-Riff 'The expected loopback port 127.0.0.1:8000 is not preserved. Review custom port settings manually.'
    }
    $currentEnvironment = @{}
    foreach ($entry in $OriginalContainer.environment) {
        $pair = $entry -split '=', 2
        if ($pair.Count -eq 2) { $currentEnvironment[$pair[0]] = $pair[1] }
    }
    foreach ($key in @('SECRET_KEY', 'TOKEN_ENCRYPTION_KEY', 'SPOTIFY_CLIENT_ID', 'SOUNDCLOUD_CLIENT_ID', 'SOUNDCLOUD_CLIENT_SECRET', 'APP_BASE_URL', 'NOTIFICATIONS_API_KEY', 'CATALOG_PATH', 'DATABASE_URL')) {
        $desired = $service.environment.PSObject.Properties[$key]
        $current = if ($currentEnvironment.ContainsKey($key)) { $currentEnvironment[$key] } else { '' }
        $desiredValue = if ($desired) { [string]$desired.Value } else { '' }
        if ($current -cne $desiredValue) { Stop-Riff 'Compose would change an existing credential, application URL, catalog path, or database path. Review environment precedence privately before updating.' }
    }
}

function Assert-RunningIdentity {
    $ids = Invoke-QuietNative docker ($ComposeArgs + @('ps', '--all', '--quiet', 'clonehero')) 'Could not find clonehero after startup.'
    $lines = @($ids.Output -split "`r?`n" | Where-Object { $_ })
    if ($lines.Count -ne 1) { Stop-Riff 'Expected one clonehero container after startup. Review Docker Desktop; no automatic rollback was attempted.' }
    $container = Read-Container $lines[0]
    if ($container.name.TrimStart('/') -ne $ContainerName -or $container.labels.'com.docker.compose.project' -ne $Project -or
        $container.labels.'com.docker.compose.service' -ne 'clonehero' -or (Get-DataMount $container) -ne $DataVolume) {
        Stop-Riff 'The running container identity differs from the preserved project/name/volume. Review it before continuing.'
    }
}

function Read-Health {
    try { return (Invoke-RestMethod -Uri 'http://127.0.0.1:8000/healthz' -Method Get -TimeoutSec 5 -ErrorAction Stop) }
    catch { return $null }
}

function Is-V2Health($Health) {
    return $null -ne $Health -and $Health.PSObject.Properties['ok'] -and $Health.ok -eq $true -and
        $Health.PSObject.Properties['app'] -and $Health.app -eq 'csv-to-clonehero' -and
        $Health.PSObject.Properties['release'] -and $Health.release -eq $Release -and
        $Health.PSObject.Properties['experience'] -and $Health.experience -eq $Experience
}

try {
    if ($CheckOnly -and $Update) { Stop-Riff 'Choose either -CheckOnly or -Update.' }
    if ($Ref -notmatch '^[A-Za-z0-9][A-Za-z0-9._/-]*$') { Stop-Riff 'Ref must be a branch name, not an option or revision expression.' }
    foreach ($tool in @('git', 'docker')) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Stop-Riff 'Install Git and start Docker Desktop with Linux containers, then rerun this helper.' }
    }
    if (-not (Test-Path -LiteralPath $RepoPath -PathType Container)) { Stop-Riff 'The repository directory is missing. Use -RepoPath for the existing Gigabyte checkout.' }
    $RepoPath = (Resolve-Path -LiteralPath $RepoPath).ProviderPath
    $root = Invoke-QuietNative git @('-C', $RepoPath, 'rev-parse', '--show-toplevel') 'This directory is not a Git checkout.'
    if (-not (Same-Path $root.Output $RepoPath)) { Stop-Riff 'RepoPath must be the repository root.' }
    $null = Invoke-QuietNative git @('-C', $RepoPath, 'check-ref-format', '--branch', $Ref) 'The requested branch name is invalid.'
    $origin = Invoke-QuietNative git @('-C', $RepoPath, 'remote', 'get-url', 'origin') 'The checkout has no readable origin.'
    Assert-Origin $origin.Output
    Assert-Clean
    $EnvPath = Join-Path $RepoPath '.env'
    if (-not (Test-Path -LiteralPath $EnvPath -PathType Leaf)) { Stop-Riff 'The existing .env is missing. Restore it privately; this helper never creates keys.' }
    $trackedEnv = Invoke-QuietNative git @('-C', $RepoPath, 'ls-files', '--error-unmatch', '--', '.env') 'Could not verify .env protection.' @(0, 1)
    if ($trackedEnv.Code -eq 0) { Stop-Riff '.env is tracked by Git. Protect the existing credentials before updating.' }
    $EnvHash = (Get-FileHash -LiteralPath $EnvPath -Algorithm SHA256).Hash
    $ComposeFile = Join-Path $RepoPath 'docker-compose.yml'
    if (-not (Test-Path -LiteralPath $ComposeFile -PathType Leaf)) { Stop-Riff 'The expected docker-compose.yml is missing.' }
    $commit = Invoke-QuietNative git @('-C', $RepoPath, 'rev-parse', 'HEAD') 'Could not record the original revision.'
    $script:OriginalCommit = $commit.Output
    $branch = Invoke-QuietNative git @('-C', $RepoPath, 'symbolic-ref', '--quiet', '--short', 'HEAD') 'Could not record the original branch.' @(0, 1)
    $script:OriginalBranch = if ($branch.Code -eq 0) { $branch.Output } else { '(detached HEAD)' }

    if ($env:DOCKER_HOST -and -not (Is-LocalDockerPipe $env:DOCKER_HOST)) { Stop-Riff 'DOCKER_HOST is not a local Docker Desktop pipe. Use the local Docker Desktop context.' }
    $context = Invoke-QuietNative docker @('context', 'show') 'Could not read the Docker context.'
    $endpoint = Invoke-QuietNative docker @('context', 'inspect', $context.Output, '--format', '{{json .Endpoints.docker.Host}}') 'Could not inspect the Docker context.'
    if (-not (Is-LocalDockerPipe (Read-QuietJson $endpoint.Output 'Docker context'))) { Stop-Riff 'The Docker context is not local Docker Desktop. Switch to the local context before updating.' }
    $daemon = Invoke-QuietNative docker @('info', '--format', '{{json .OSType}}') 'Docker Desktop is not responding. Start it and enable Linux containers.'
    if ((Read-QuietJson $daemon.Output 'Docker daemon') -ne 'linux') { Stop-Riff 'Docker Desktop must run Linux containers for RIFF.' }
    $null = Invoke-QuietNative docker @('compose', 'version', '--short') 'Docker Compose v2 is required.'
    $ids = Invoke-QuietNative docker @('ps', '--all', '--filter', 'label=com.docker.compose.service=clonehero', '--format', '{{.ID}}') 'Could not find the existing clonehero deployment.'
    $containers = @($ids.Output -split "`r?`n" | Where-Object { $_ })
    if ($containers.Count -ne 1) { Stop-Riff 'Expected exactly one existing clonehero container. Start or identify the original Compose deployment before updating.' }
    $OriginalContainer = Read-Container $containers[0]
    $labels = $OriginalContainer.labels
    $Project = $labels.'com.docker.compose.project'
    if ($Project -notmatch '^[a-z0-9][a-z0-9_-]*$' -or $labels.'com.docker.compose.service' -ne 'clonehero') { Stop-Riff 'The existing Compose project identity is invalid.' }
    if (-not (Same-Path $labels.'com.docker.compose.project.working_dir' $RepoPath) -or
        -not (Same-Path $labels.'com.docker.compose.project.config_files' $ComposeFile)) {
        Stop-Riff 'The existing service uses another checkout or custom Compose files. Preserve that deployment configuration manually; this helper will not guess.'
    }
    $ContainerName = $OriginalContainer.name.TrimStart('/')
    $DataVolume = Get-DataMount $OriginalContainer
    $script:OriginalImage = $OriginalContainer.image
    $ComposeArgs = @('compose', '--project-name', $Project, '--project-directory', $RepoPath, '--env-file', $EnvPath, '-f', $ComposeFile)
    $GeneratedContainerName = "$Project-clonehero-1"
    if ($ContainerName -eq "${Project}_clonehero_1") {
        $ComposeArgs += '--compatibility'
        $GeneratedContainerName = "${Project}_clonehero_1"
    }
    Assert-Compose
    $remote = Invoke-QuietNative git @('-C', $RepoPath, 'ls-remote', '--exit-code', 'origin', "refs/heads/$Ref") 'The requested V2 branch is unavailable. Wait for the reviewed branch to be published.'
    if ($remote.Output -notmatch "^([0-9a-fA-F]{40})\s+refs/heads/$([regex]::Escape($Ref))$") { Stop-Riff 'Could not identify one exact remote branch revision.' }
    $TargetCommit = $Matches[1].ToLowerInvariant()
    if ($ExpectedCommit -and $TargetCommit -ne $ExpectedCommit.ToLowerInvariant()) { Stop-Riff 'The remote branch differs from the reviewed ExpectedCommit. Stop and obtain the current reviewed revision.' }
    Write-Host "Original branch: $script:OriginalBranch"
    Write-Host "Original commit: $script:OriginalCommit"
    Write-Host "Original image ID: $script:OriginalImage"
    Write-Host "Preserved Compose project: $Project; container: $ContainerName; data volume: $DataVolume"
    Write-Host "Reviewed target: $Ref at $TargetCommit"
    if (-not $Update) {
        if (Is-V2Health (Read-Health)) { Write-Host 'V2 already responds on this computer.' }
        else { Write-Host 'V2 is not confirmed locally. A running older release is expected before updating.' }
        Write-Host 'Check complete. No fetch, checkout, build, or service update was performed. Rerun with -Update to apply.'
        exit 0
    }

    $RemoteRef = "refs/remotes/origin/$Ref"
    $null = Invoke-QuietNative git @('-C', $RepoPath, 'fetch', '--no-tags', 'origin', "refs/heads/${Ref}:${RemoteRef}") 'Fetch failed. The current checkout and running service were not changed; check connectivity or a rewritten remote branch.'
    $fetched = Invoke-QuietNative git @('-C', $RepoPath, 'rev-parse', $RemoteRef) 'Could not verify the fetched revision.'
    if ($fetched.Output.ToLowerInvariant() -ne $TargetCommit) { Stop-Riff 'The branch changed during fetch. Rerun checks against the reviewed revision before updating.' }
    $targetEnv = Invoke-QuietNative git @('-C', $RepoPath, 'ls-tree', '--name-only', $RemoteRef, '--', '.env') 'Could not verify credential-file protection in the target revision.'
    if ($targetEnv.Output) { Stop-Riff 'The target revision tracks .env and could overwrite the existing credentials. The checkout and service were not changed.' }
    Assert-Clean
    Assert-EnvUnchanged
    $local = Invoke-QuietNative git @('-C', $RepoPath, 'show-ref', '--verify', '--quiet', "refs/heads/$Ref") 'Could not inspect the local target branch.' @(0, 1)
    if ($local.Code -eq 0) {
        $ancestry = Invoke-QuietNative git @('-C', $RepoPath, 'merge-base', '--is-ancestor', "refs/heads/$Ref", $RemoteRef) 'Could not inspect branch ancestry.' @(0, 1)
        if ($ancestry.Code -ne 0) { Stop-Riff 'The local V2 branch has commits absent from the remote branch. Preserve and reconcile them yourself; no forced switch was attempted.' }
        $null = Invoke-QuietNative git @('-C', $RepoPath, 'switch', $Ref) 'Checkout was blocked. Preserve conflicting files or active worktrees yourself; no reset/stash was attempted.'
        $script:CheckoutChanged = $true
        $null = Invoke-QuietNative git @('-C', $RepoPath, 'merge', '--ff-only', '--no-edit', $RemoteRef) 'Fast-forward failed. Preserve the checkout and reconcile it manually; the service has not been updated.'
    } else {
        $null = Invoke-QuietNative git @('-C', $RepoPath, 'switch', '--create', $Ref, '--track', $RemoteRef) 'Checkout was blocked. Preserve conflicting files or active worktrees yourself; no reset/stash was attempted.'
        $script:CheckoutChanged = $true
    }
    $head = Invoke-QuietNative git @('-C', $RepoPath, 'rev-parse', 'HEAD') 'Could not verify the checked-out revision.'
    if ($head.Output.ToLowerInvariant() -ne $TargetCommit) { Stop-Riff 'The checkout does not match the reviewed target. The service has not been updated.' }
    Assert-Clean
    Assert-Compose
    Write-Host 'Building V2 while the existing service keeps running...'
    $null = Invoke-QuietNative docker ($ComposeArgs + @('build', 'clonehero')) 'The image build failed. The existing service remains running. Review the build privately; raw logs may contain secrets.'
    Assert-Clean
    Assert-Compose
    Write-Host 'Starting the built clonehero image with the same project and data volume...'
    $script:ServiceChanged = $true
    $null = Invoke-QuietNative docker ($ComposeArgs + @('up', '-d', '--no-build', '--no-deps', 'clonehero')) 'Startup failed. Inspect Docker Desktop and the local health endpoint; no automatic reset, volume deletion, or rollback was attempted.'
    Assert-EnvUnchanged
    Assert-RunningIdentity
    $deadline = [DateTime]::UtcNow.AddSeconds($HealthTimeoutSeconds)
    do {
        if (Is-V2Health (Read-Health)) {
            Write-Host "V2 confirmed locally: $Release / $Experience. The existing .env, project, container name, and data volume were preserved."
            Write-Host 'Now open riff.ryo-nd.com and verify Add Music, Find, Confirm, and ZIP delivery. Local health does not verify the tunnel or a personal Spotify session.'
            exit 0
        }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $deadline)
    Stop-Riff 'The service did not confirm the expected V2 release and Signal Check experience. Inspect Docker Desktop and http://127.0.0.1:8000/healthz; do not change the tunnel or delete data.'
} catch {
    $message = if ($script:RiffFailureMessage) { $script:RiffFailureMessage } else { 'An unexpected check failed. No raw diagnostics were displayed. Inspect the existing checkout and Docker Desktop privately.' }
    Write-Host "Stopped: $message"
    if ($script:CheckoutChanged) {
        Write-Host "The checkout may now be on the V2 branch. Original branch: $script:OriginalBranch; original commit: $script:OriginalCommit"
    }
    if ($script:ServiceChanged) { Write-Host "If a manual rollback is needed, retain the original image ID $script:OriginalImage and original branch. This helper will not roll back database state automatically." }
    exit 1
}

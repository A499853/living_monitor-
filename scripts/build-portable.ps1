[CmdletBinding()]
param(
    [string]$PackageName = 'LiveWatch-Optimized-Windows-x64',
    [switch]$SkipBuild,
    [switch]$StageOnly,
    [switch]$RequireWechatBridge
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ($PackageName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]+$') { throw 'Invalid package name.' }
$ffmpegPath = Join-Path $projectRoot 'tools\ffmpeg.exe'
if (-not (Test-Path -LiteralPath $ffmpegPath -PathType Leaf) -or (Get-Item -LiteralPath $ffmpegPath).Length -le 1000000) {
    throw 'A complete tools\ffmpeg.exe is required for the delivery package.'
}
if (-not $SkipBuild) { & (Join-Path $PSScriptRoot 'build-desktop.ps1') }
$stageParent = Join-Path $projectRoot 'build\portable'
$stage = [IO.Path]::GetFullPath((Join-Path $stageParent $PackageName))
if (-not $stage.StartsWith([IO.Path]::GetFullPath($stageParent) + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe staging path.' }
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage -Force | Out-Null
$exeName = (-join @([char]0x76F4, [char]0x64AD, [char]0x5B88, [char]0x671B)) + '.exe'
foreach ($name in @($exeName, "$exeName.config", 'Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.WinForms.dll', 'WebView2Loader.dll')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot "build\desktop\$name") -Destination $stage
}
foreach ($name in @('dependency-manifest.json', 'desktop-dependency-manifest.json')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination $stage
}
# Ship clean fixed runtimes; never copy live browser profiles or entire tools/.
$packagedTools = Join-Path $stage 'tools'
New-Item -ItemType Directory -Path $packagedTools -Force | Out-Null
Copy-Item -LiteralPath $ffmpegPath -Destination (Join-Path $packagedTools 'ffmpeg.exe')
# The restoration inventory is an immutable allowlist for these fixed versions.
# Hash every input before copying, so unknown additions and altered binaries do
# not silently become release dependencies.
$restored = Get-Content -LiteralPath (Join-Path $projectRoot 'restoration-manifest.json') -Raw | ConvertFrom-Json
$runtimeInventory = @($restored.files | Where-Object { $_.path -match '^tools/(python|webview2)/' })
if ($runtimeInventory.Count -eq 0) { throw 'The fixed runtime allowlist is missing.' }
foreach ($item in $runtimeInventory) {
    $relative = [string]$item.path
    if ($relative -match '(^|/)\.\.(/|$)|[\\:]' -or [IO.Path]::IsPathRooted($relative)) { throw 'Unsafe runtime inventory path.' }
    $source = [IO.Path]::GetFullPath((Join-Path $projectRoot $relative))
    if (-not $source.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Runtime source is outside the project.' }
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing fixed runtime file: $relative" }
    $info = Get-Item -LiteralPath $source
    if (($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $info.Length -ne [long]$item.size -or
        (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne [string]$item.sha256) {
        throw "Fixed runtime differs from its verified inventory: $relative"
    }
    $destination = Join-Path $stage $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination
}
$coreDependencies = Get-Content -LiteralPath (Join-Path $projectRoot 'dependency-manifest.json') -Raw | ConvertFrom-Json
$desktopDependencies = Get-Content -LiteralPath (Join-Path $projectRoot 'desktop-dependency-manifest.json') -Raw | ConvertFrom-Json
foreach ($runtimeName in @('python', 'webview2')) {
    $runtimeRoot = Join-Path $packagedTools $runtimeName
    $runtimeHash = if ($runtimeName -eq 'python') { $coreDependencies.python.sha256 } else { $desktopDependencies.webview2_runtime.sha256 }
    [IO.File]::WriteAllText((Join-Path $runtimeRoot '.livewatch-package-sha256'), $runtimeHash, [Text.Encoding]::ASCII)
    $receipt = [ordered]@{}
    foreach ($file in Get-ChildItem -LiteralPath $runtimeRoot -Recurse -File -Force | Sort-Object FullName) {
        $receipt[$file.FullName.Substring($runtimeRoot.Length + 1)] = $file.Length
    }
    [IO.File]::WriteAllText((Join-Path $runtimeRoot '.livewatch-runtime.json'), ($receipt | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
}
[IO.File]::WriteAllText((Join-Path $stage 'runtime-source-manifest.json'), ($runtimeInventory | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
$bridgeRoot = Join-Path $projectRoot 'tools\wechat-bridge'
$bridgeExecutable = Join-Path $bridgeRoot 'wx_video_download.exe'
if (Test-Path -LiteralPath $bridgeExecutable -PathType Leaf) {
    if (-not (Test-Path -LiteralPath (Join-Path $bridgeRoot 'LICENSE') -PathType Leaf)) { throw 'The bundled WeChat bridge requires its LICENSE.' }
    $bridgeTarget = Join-Path $packagedTools 'wechat-bridge'
    New-Item -ItemType Directory -Path $bridgeTarget -Force | Out-Null
    foreach ($name in @('wx_video_download.exe', 'LICENSE', 'VERSION.txt', 'download-manifest.json')) {
        $source = Join-Path $bridgeRoot $name
        if (Test-Path -LiteralPath $source -PathType Leaf) { Copy-Item -LiteralPath $source -Destination (Join-Path $bridgeTarget $name) }
    }
} elseif ($RequireWechatBridge) {
    throw 'The full WeChat delivery requires tools\wechat-bridge\wx_video_download.exe and LICENSE.'
}
foreach ($folder in @('app', 'licenses')) {
    $sourceRoot = Join-Path $projectRoot $folder
    foreach ($file in Get-ChildItem -LiteralPath $sourceRoot -Recurse -File) {
        if ($file.FullName -match '[\\/](__pycache__|data|logs|video|time_begin_over)[\\/]' -or $file.Extension -in @('.pyc', '.pyo')) { continue }
        $relative = $file.FullName.Substring($projectRoot.Length + 1)
        $destination = Join-Path $stage $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $destination
    }
}
# Copy authored documents, never user databases, room lists, credentials or recordings.
foreach ($name in @('README.md', 'portable-delivery.md', 'start.cmd')) {
    if (Test-Path -LiteralPath (Join-Path $projectRoot $name)) { Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination $stage }
}
# Select the Chinese-language delivery guides by Unicode code points so Windows
# PowerShell 5.1 can continue to read this script as plain ASCII.
$documentNames = @(
    @(0x5148,0x8BFB,0x6211),
    @(0x4F7F,0x7528,0x8BF4,0x660E),
    @(0x4F18,0x5316,0x4EA4,0x4ED8,0x4E0E,0x9A8C,0x8BC1),
    @(0x7B2C,0x4E09,0x65B9,0x7EC4,0x4EF6,0x8BF4,0x660E),
    @(0x624B,0x673A,0x5206,0x4EAB,0x94FE,0x63A5,0x89E3,0x6790),
    @(0x5F02,0x5E38,0x6062,0x590D,0x4E0E,0x5A92,0x4F53,0x8BBE,0x7F6E),
    @(0x68C0,0x6D4B,0x8D85,0x65F6,0x6062,0x590D,0x4E0E,0x4EA4,0x4ED8)
)
for ($index = 0; $index -lt $documentNames.Count; $index++) {
    $name = -join @($documentNames[$index] | ForEach-Object { [char]$_ })
    if ($index -eq 0) { $name += '.txt' } else { $name += '.md' }
    if (Test-Path -LiteralPath (Join-Path $projectRoot $name)) { Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination $stage }
}
$inventory = @(Get-ChildItem -LiteralPath $stage -Recurse -File -Force | Sort-Object FullName | ForEach-Object {
    [ordered]@{ path = $_.FullName.Substring($stage.Length + 1).Replace('\','/'); size = $_.Length; sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
})
[IO.File]::WriteAllText((Join-Path $stage 'package-manifest.json'), ($inventory | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
# Validate the actual staged files and embedded Python before producing a ZIP.
$verificationArgs = @('-B', (Join-Path $PSScriptRoot 'verify-delivery-package.py'), '--stage', $stage)
if ($RequireWechatBridge) { $verificationArgs += '--require-wechat-bridge' }
& (Join-Path $stage 'tools\python\python.exe') @verificationArgs
if ($LASTEXITCODE -ne 0) { throw 'Offline delivery validation failed.' }
if ($StageOnly) { Write-Host "Staged offline package: $stage"; return }
$artifactRoot = Join-Path $projectRoot 'artifacts'
New-Item -ItemType Directory -Path $artifactRoot -Force | Out-Null
$zipPath = Join-Path $artifactRoot ($PackageName + '.zip')
$temporary = $zipPath + '.partial'
if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$stream = [IO.File]::Create($temporary)
try {
    $zip = New-Object IO.Compression.ZipArchive($stream, [IO.Compression.ZipArchiveMode]::Create, $true)
    try {
        foreach ($file in Get-ChildItem -LiteralPath $stage -Recurse -File -Force | Sort-Object FullName) {
            $relative = $file.FullName.Substring($stage.Length + 1).Replace('\','/')
            $entry = $zip.CreateEntry("$PackageName/$relative", [IO.Compression.CompressionLevel]::Optimal)
            $entry.LastWriteTime = [DateTimeOffset]::new(2000, 1, 1, 0, 0, 0, [TimeSpan]::Zero)
            $inputStream = $file.OpenRead()
            $entryStream = $entry.Open()
            try { $inputStream.CopyTo($entryStream) } finally { $entryStream.Dispose(); $inputStream.Dispose() }
        }
    } finally { $zip.Dispose() }
} finally { $stream.Dispose() }
Move-Item -LiteralPath $temporary -Destination $zipPath -Force
$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText(($zipPath + '.sha256'), "$hash  $PackageName.zip`n", [Text.Encoding]::ASCII)
Write-Host "Package: $zipPath"
Write-Host "Size: $((Get-Item -LiteralPath $zipPath).Length) bytes"
Write-Host "SHA256: $hash"

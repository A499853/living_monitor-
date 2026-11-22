[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$testRoot = Join-Path $projectRoot ('build\bootstrap-tests\' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$framework = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$arguments = @('/nologo', '/target:exe', '/platform:x64', '/langversion:5', '/codepage:65001', '/utf8output', "/out:$testRoot\verify.exe")
foreach ($name in @('System.dll', 'System.Core.dll', 'System.Drawing.dll', 'System.Windows.Forms.dll', 'System.Web.Extensions.dll', 'System.IO.Compression.dll', 'System.IO.Compression.FileSystem.dll')) {
    $arguments += '/reference:' + (Join-Path $framework $name)
}
$arguments += Join-Path $projectRoot 'desktop\DependencyBootstrapper.cs'
$arguments += Join-Path $projectRoot 'desktop\DesktopPaths.cs'
$arguments += Join-Path $PSScriptRoot 'BootstrapVerification.cs'
& (Join-Path $framework 'csc.exe') @arguments
if ($LASTEXITCODE -ne 0) { throw 'Bootstrap verification compilation failed.' }
& (Join-Path $testRoot 'verify.exe') (Join-Path $testRoot 'fixtures')
if ($LASTEXITCODE -ne 0) { throw 'Bootstrap verification failed.' }

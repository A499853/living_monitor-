<# Prepare an isolated bridge trial. Does not run the bridge, install a
certificate, or change any system proxy setting. Requires PowerShell 7+. #>
param(
    [string]$DataRoot = '',
    [string]$BridgeDirectory = ''
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
if (!$DataRoot) { $DataRoot = Join-Path $project 'artifacts\wechat-preview' }
if (!$BridgeDirectory) { $BridgeDirectory = Join-Path $project '.build-tools\wechat-bridge-v261123\unpacked' }
$DataRoot = [IO.Path]::GetFullPath($DataRoot)
$BridgeDirectory = [IO.Path]::GetFullPath($BridgeDirectory)
$scriptFile = Join-Path $DataRoot 'data\wechat-capture\global.js'
if (!(Test-Path -LiteralPath $scriptFile)) { throw 'Start capture receiving in LiveWatch first.' }
if (!(Test-Path -LiteralPath (Join-Path $BridgeDirectory 'wx_video_download.exe'))) {
    throw 'Download and verify the v261123 bridge package before preparing the trial.'
}
$trial = Join-Path $BridgeDirectory ('trial-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Path $trial | Out-Null
$certificateName = 'LiveWatch-WeChat-Trial-' + [Guid]::NewGuid().ToString('N').Substring(0,12)
$rsa = [Security.Cryptography.RSA]::Create(2048)
try {
    $request = [Security.Cryptography.X509Certificates.CertificateRequest]::new(
        "CN=$certificateName", $rsa, [Security.Cryptography.HashAlgorithmName]::SHA256,
        [Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($true, $false, 0, $true))
    $request.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new(
        [Security.Cryptography.X509Certificates.X509KeyUsageFlags]::KeyCertSign -bor
        [Security.Cryptography.X509Certificates.X509KeyUsageFlags]::CrlSign, $true))
    $certificate = $request.CreateSelfSigned([DateTimeOffset]::UtcNow.AddMinutes(-5), [DateTimeOffset]::UtcNow.AddDays(7))
    try {
        $certFile = Join-Path $trial 'trial-ca.pem'
        $keyFile = Join-Path $trial 'trial-ca-key.pem'
        [IO.File]::WriteAllText($certFile, $certificate.ExportCertificatePem())
        [IO.File]::WriteAllText($keyFile, $rsa.ExportPkcs8PrivateKeyPem())
        # The private key remains local to this Windows identity.
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl = Get-Acl -LiteralPath $keyFile
        $acl.SetAccessRuleProtection($true, $false)
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'Allow'))
        Set-Acl -LiteralPath $keyFile -AclObject $acl
        $manifest = [ordered]@{certificate_name=$certificateName; thumbprint=$certificate.Thumbprint;
            expires_utc=$certificate.NotAfter.ToUniversalTime().ToString('o'); installed=$false;
            bridge_version='v261123'; capture_script=$scriptFile}
        $manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $trial 'trial.json') -Encoding utf8
    } finally { $certificate.Dispose() }
} finally { $rsa.Dispose() }
function YamlPath([string]$value) { return "'" + $value.Replace('\','/').Replace("'", "''") + "'" }
$config = @"
# Trial only: running the bridge with this file installs the named local CA
# and changes the system proxy. Preparation alone makes neither change.
workdir: $(YamlPath $trial)
inject:
  globalScript: $(YamlPath $scriptFile)
api:
  protocol: http
  hostname: 127.0.0.1
  port: 2022
proxy:
  enabled: true
  system: true
  hostname: 127.0.0.1
  port: 2023
  tun: false
  skipInstallRootCert: false
cert:
  file: $(YamlPath $certFile)
  key: $(YamlPath $keyFile)
  name: '$certificateName'
log:
  enabled: false
debug:
  error: false
  echolog: false
mcp:
  enabled: false
bridge:
  enabled: false
download:
  remoteServer:
    enabled: false
update:
  sources: []
"@
$configFile = Join-Path $trial 'config.yaml'
[IO.File]::WriteAllText($configFile, $config, [Text.UTF8Encoding]::new($false))
Write-Output "Prepared config: $configFile"
Write-Output "Certificate: $certificateName (not installed; valid for 7 days)"
Write-Output 'No process was started and no system proxy was changed.'

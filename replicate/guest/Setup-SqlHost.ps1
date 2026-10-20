#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Turn a fresh Windows VM into a SQL Server host for Replicate.

.DESCRIPTION
    Replicate puts this script, together with replicate.json, on the
    REPLICATE_SETUP CD and runs it at the first sign-in of the unattended
    install. It can also be run by hand, any number of times: every step
    checks first and skips what is already done.

    replicate.json says which instances to install (name + fixed TCP port),
    the SQL login Replicate connects with, and where to write progress.
    Replicate follows that state file through the QEMU guest agent and
    registers the instances when the status becomes "done".

    What it does:
      1. SQL Server 2022 Developer: downloads the media once, installs each
         instance with mixed-mode authentication and TCP enabled
      2. Gives each instance its fixed TCP port, so the host can connect
         without SQL Browser
      3. Creates the Replicate login (sysadmin) on each instance
      4. Opens the firewall for the instances and SQL Browser
      5. Writes the result to the state file

    Nothing here is specific to any company: no domain, shares or agents.
#>
[CmdletBinding()]
param(
    [string]$Manifest = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$ProgressPreference = 'SilentlyContinue'

function Say([string]$Message) { Write-Host "==> $Message" -ForegroundColor Green }
function Warn([string]$Message) { Write-Host "==> $Message" -ForegroundColor Yellow }

# SQL Server 2022 Developer web installer, as pinned by winget
# (manifests/m/Microsoft/SQLServer/2022/Developer/16.0.1000.6).
$SseiUrl = 'https://download.microsoft.com/download/c/c/9/cc9c6797-383c-4b24-8920-dc057c1de9d3/SQL2022-SSEI-Dev.exe'
$SseiSha256 = '5D9D277DCCEF80F2395F915664BD8E63FE7496B7B9C005C2F0C78A3C70D03813'
$SqlMedia = 'C:\SQLServerMedia'
$StateDir = Join-Path $env:ProgramData 'Replicate'

# ---------------------------------------------------------------------------
# Manifest and state

if (-not $Manifest) {
    $candidates = @((Join-Path $PSScriptRoot 'replicate.json'), (Join-Path $StateDir 'replicate.json'))
    $Manifest = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Manifest) { throw 'replicate.json was not found next to this script.' }
$config = Get-Content $Manifest -Raw | ConvertFrom-Json
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
# Keep a copy so a rerun from C:\ works after the CD is gone.
if ((Resolve-Path $Manifest).Path -ne (Join-Path $StateDir 'replicate.json')) {
    Copy-Item $Manifest (Join-Path $StateDir 'replicate.json') -Force
}
$stateFile = if ($config.statePath) { $config.statePath } else { Join-Path $StateDir 'setup-state.json' }
$instances = @($config.instances)
$adminLogin = $config.adminLogin
$adminPassword = $config.adminPassword
$windowsUser = $config.windowsUser

function Write-State([string]$Status, [string]$Step, [string]$Message) {
    $state = [ordered]@{
        status    = $Status
        step      = $Step
        message   = $Message
        instances = @($instances | ForEach-Object { @{ name = $_.name; port = [int]$_.port } })
        updatedAt = (Get-Date).ToString('o')
    }
    $state | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -Path $stateFile
}

function Get-ServiceName([string]$Instance) {
    if ($Instance -eq 'MSSQLSERVER') { 'MSSQLSERVER' } else { "MSSQL`$$Instance" }
}

function Invoke-Sql([string]$Instance, [string]$Query) {
    $server = if ($Instance -eq 'MSSQLSERVER') { 'localhost' } else { "localhost\$Instance" }
    $conn = New-Object System.Data.SqlClient.SqlConnection
    $conn.ConnectionString = "Server=$server;Database=master;Integrated Security=True;TrustServerCertificate=True;Connect Timeout=30"
    $conn.Open()
    try {
        $cmd = $conn.CreateCommand()
        $cmd.CommandText = $Query
        $cmd.CommandTimeout = 120
        [void]$cmd.ExecuteNonQuery()
    } finally {
        $conn.Close()
    }
}

try {
    Write-State 'running' 'start' 'Setup script started'

    # -----------------------------------------------------------------------
    # 1. SQL Server 2022 Developer

    $missing = @($instances | Where-Object { -not (Get-Service -Name (Get-ServiceName $_.name) -ErrorAction SilentlyContinue) })
    if ($missing.Count -eq 0) {
        Say "SQL Server instances are installed: $(($instances | ForEach-Object { $_.name }) -join ', ')"
    } else {
        New-Item -ItemType Directory -Force -Path $SqlMedia | Out-Null
        $iso = Get-ChildItem -Path $SqlMedia -Filter *.iso -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $iso) {
            Write-State 'running' 'download' 'Downloading the SQL Server 2022 Developer media'
            $ssei = Join-Path $SqlMedia 'SQL2022-SSEI-Dev.exe'
            Say 'Downloading the SQL Server 2022 Developer installer'
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -UseBasicParsing -Uri $SseiUrl -OutFile $ssei
            if ((Get-FileHash -Algorithm SHA256 -Path $ssei).Hash -ne $SseiSha256) {
                Remove-Item $ssei
                throw 'The SQL Server installer does not match its expected checksum.'
            }
            Say 'Downloading the installation media (about 1 GB)'
            $p = Start-Process -FilePath $ssei -Wait -PassThru -ArgumentList @(
                '/ACTION=Download', "/MEDIAPATH=$SqlMedia", '/MEDIATYPE=ISO', '/LANGUAGE=en-US', '/QUIET')
            $iso = Get-ChildItem -Path $SqlMedia -Filter *.iso | Select-Object -First 1
            if ($p.ExitCode -ne 0 -or -not $iso) { throw "Downloading the SQL Server media failed (exit code $($p.ExitCode))." }
        }

        $image = Mount-DiskImage -ImagePath $iso.FullName -PassThru
        try {
            $setup = "$(($image | Get-Volume).DriveLetter):\setup.exe"
            $sysadmins = @('BUILTIN\Administrators')
            if ($windowsUser) { $sysadmins += "$env:COMPUTERNAME\$windowsUser" }
            foreach ($instance in $missing) {
                $name = $instance.name
                Write-State 'running' 'install' "Installing SQL Server instance $name"
                Say "Installing SQL Server instance $name (several minutes)"
                $p = Start-Process -FilePath $setup -Wait -PassThru -ArgumentList @(
                    '/Q', '/ACTION=Install', '/FEATURES=SQLENGINE', "/INSTANCENAME=$name",
                    "/SQLSVCACCOUNT=`"NT Service\$(Get-ServiceName $name)`"",
                    ('/SQLSYSADMINACCOUNTS=' + (($sysadmins | ForEach-Object { "`"$_`"" }) -join ' ')),
                    '/SECURITYMODE=SQL', "/SAPWD=`"$adminPassword`"",
                    '/TCPENABLED=1', '/BROWSERSVCSTARTUPTYPE=Automatic',
                    '/SQLSVCINSTANTFILEINIT=True',
                    '/UPDATEENABLED=False', '/IACCEPTSQLSERVERLICENSETERMS')
                if ($p.ExitCode -notin 0, 3010) {
                    throw "SQL Server setup for $name failed (exit code $($p.ExitCode)). Logs: C:\Program Files\Microsoft SQL Server\160\Setup Bootstrap\Log"
                }
            }
        } finally {
            Dismount-DiskImage -ImagePath $iso.FullName | Out-Null
        }
    }

    # -----------------------------------------------------------------------
    # 2. Fixed TCP ports

    Write-State 'running' 'network' 'Setting TCP ports'
    $instanceIds = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL'
    foreach ($instance in $instances) {
        $name = $instance.name
        $port = [string][int]$instance.port
        $id = $instanceIds.$name
        if (-not $id) { Warn "No registry entry for instance $name; skipping its port"; continue }
        $tcp = "HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\$id\MSSQLServer\SuperSocketNetLib\Tcp"
        $ipAll = Join-Path $tcp 'IPAll'
        $current = (Get-ItemProperty $ipAll).TcpPort
        Set-ItemProperty -Path $tcp -Name Enabled -Value 1 -Type DWord
        if ($current -ne $port) {
            Set-ItemProperty -Path $ipAll -Name TcpDynamicPorts -Value '' -Type String
            Set-ItemProperty -Path $ipAll -Name TcpPort -Value $port -Type String
            Say "Instance $name listens on TCP $port; restarting it"
            Restart-Service -Name (Get-ServiceName $name) -Force
        } else {
            Say "Instance $name listens on TCP $port"
        }
    }
    Set-Service -Name SQLBrowser -StartupType Automatic -ErrorAction SilentlyContinue
    Start-Service -Name SQLBrowser -ErrorAction SilentlyContinue

    # -----------------------------------------------------------------------
    # 3. The Replicate login

    Write-State 'running' 'login' 'Creating the Replicate login'
    $login = $adminLogin.Replace("'", "''")
    $pwd = $adminPassword.Replace("'", "''")
    $sql = @"
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'$login')
    CREATE LOGIN [$adminLogin] WITH PASSWORD = N'$pwd', CHECK_POLICY = OFF, CHECK_EXPIRATION = OFF;
ELSE
    ALTER LOGIN [$adminLogin] WITH PASSWORD = N'$pwd', CHECK_POLICY = OFF, CHECK_EXPIRATION = OFF;
IF IS_SRVROLEMEMBER('sysadmin', N'$login') = 0
    ALTER SERVER ROLE sysadmin ADD MEMBER [$adminLogin];
EXEC xp_instance_regwrite N'HKEY_LOCAL_MACHINE', N'Software\Microsoft\MSSQLServer\MSSQLServer', N'LoginMode', REG_DWORD, 2;
"@
    foreach ($instance in $instances) {
        $svc = Get-ServiceName $instance.name
        $deadline = (Get-Date).AddMinutes(3)
        while ((Get-Service -Name $svc).Status -ne 'Running' -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 3 }
        $tries = 0
        while ($true) {
            try { Invoke-Sql $instance.name $sql; break }
            catch { if (++$tries -ge 10) { throw }; Start-Sleep -Seconds 6 }
        }
        Say "$adminLogin is sysadmin on $($instance.name)"
    }

    # -----------------------------------------------------------------------
    # 4. Firewall

    Write-State 'running' 'firewall' 'Opening the firewall'
    $ports = @($instances | ForEach-Object { [int]$_.port })
    if (-not (Get-NetFirewallRule -DisplayName 'Replicate SQL Server' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'Replicate SQL Server' -Direction Inbound -Protocol TCP -LocalPort $ports -Action Allow | Out-Null
    } else {
        Set-NetFirewallRule -DisplayName 'Replicate SQL Server' -Protocol TCP -LocalPort $ports | Out-Null
    }
    if (-not (Get-NetFirewallRule -DisplayName 'Replicate SQL Browser' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'Replicate SQL Browser' -Direction Inbound -Protocol UDP -LocalPort 1434 -Action Allow | Out-Null
    }
    Say 'Firewall rules are in place'

    # -----------------------------------------------------------------------
    # 5. Done

    Write-State 'done' 'done' 'SQL Server is installed'
    Say 'Done. Replicate on the host will pick the instances up by itself.'
    Say 'You can close this window; the VM keeps running in the background.'
} catch {
    $message = $_.Exception.Message
    Write-State 'failed' 'error' $message
    Write-Host "==> $message" -ForegroundColor Red
    throw
}

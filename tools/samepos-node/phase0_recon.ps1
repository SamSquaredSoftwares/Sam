<#
    phase0_recon.ps1 - read-only recon on a prospective SAMePOS venue node.

    STRICTLY READ-ONLY. It creates no database, writes no venue data, installs
    nothing and starts no service. Safe to run on a live trading box during
    service. The only thing it writes is its own JSON report on stdout (which the
    driver redirects to a log file).

    It answers, in one pass, every prerequisite the install runbook says to
    confirm rather than assume:

      * identity + elevation we actually got over the network (the UAC
        remote-token-filtering check)
      * free space per drive, to decide whether C:\SamePOS-Server must be a
        junction onto a data drive
      * the DigitotPOS SQL instance and database name FOR THIS VENUE - these vary
        (default instance vs .\SQLEXPRESS, DigitotPOS_New vs DigitotPOS_psy) and
        the importer must be pointed at the real ones
      * which of the four optional `articles` columns exist, so we know before
        the import whether compat fix #2 is load-bearing here (a single missing
        column rolls back the ENTIRE catalog/staff/tender import)
      * an ODBC SQL Server driver, without which the import cannot connect
      * whether a SAMePOS install, its scheduled tasks or port 8077 are already
        in use - i.e. whether this box is already commissioned

    Emits a single JSON object so the orchestrator can branch on it.
#>

[CmdletBinding()]
param(
    # Where a previous/partial install would live. Reported on, never modified.
    [string] $InstallRoot = 'C:\SamePOS-Server'
)

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

function New-Report {
    [ordered] @{
        schema_version = 1
        generated_at   = (Get-Date).ToString('o')
        readonly       = $true
    }
}

$report = New-Report
$report.errors = New-Object System.Collections.ArrayList

# Verdict keys are seeded so they are ALWAYS present in the report, even if the
# probe that computes one fails. A consumer branching on these should never have
# to distinguish "absent" from "unknown".
$report.install_location_advice = 'unknown - the drive probe did not complete'
$report.digitot_verdict         = 'unknown - the SQL probe did not complete'
$report.payload_verdict         = 'unknown - the payload search did not complete'

function Add-Problem([string] $Area, [string] $Message) {
    [void] $report.errors.Add([ordered] @{ area = $Area; message = $Message })
}

# ---------------------------------------------------------------- identity ---
# Elevation is the fork the whole install hangs off: a token-filtered network
# logon looks like a valid admin until something actually needs the admin token.
try {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $report.identity = [ordered] @{
        whoami        = $identity.Name
        elevated      = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        is_system     = $identity.IsSystem
        computer_name = $env:COMPUTERNAME
    }
} catch {
    Add-Problem 'identity' $_.Exception.Message
}

# ------------------------------------------------------------------- system ---
try {
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
    $report.system = [ordered] @{
        os_caption     = $os.Caption
        os_version     = $os.Version
        os_build       = $os.BuildNumber
        architecture   = $os.OSArchitecture
        last_boot      = $os.LastBootUpTime.ToString('o')
        ps_version     = $PSVersionTable.PSVersion.ToString()
        total_ram_gb   = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
    }
} catch {
    Add-Problem 'system' $_.Exception.Message
}

# ------------------------------------------------------------------- drives ---
# C: on venue boxes is routinely too small for a growing trading DB. Anything
# under ~20 GB free on C: means install elsewhere and junction.
try {
    $report.drives = @(
        Get-CimInstance Win32_LogicalDisk -Filter 'DriveType = 3' -ErrorAction Stop |
            ForEach-Object {
                [ordered] @{
                    drive       = $_.DeviceID
                    label       = $_.VolumeName
                    size_gb     = [math]::Round($_.Size / 1GB, 2)
                    free_gb     = [math]::Round($_.FreeSpace / 1GB, 2)
                    free_pct    = if ($_.Size -gt 0) { [math]::Round(100 * $_.FreeSpace / $_.Size, 1) } else { 0 }
                }
            }
    )
    $systemDrive = @($report.drives | Where-Object { $_.drive -eq $env:SystemDrive })[0]
    $report.install_location_advice = if (-not $systemDrive) {
        'could not read the system drive - determine free space manually'
    } elseif ($systemDrive.free_gb -lt 20) {
        "only $($systemDrive.free_gb) GB free on $($env:SystemDrive) - install to a data " +
        'drive and junction C:\SamePOS-Server onto it (the product hardcodes that path)'
    } else {
        "$($systemDrive.free_gb) GB free on $($env:SystemDrive) - a direct install is fine"
    }
} catch {
    Add-Problem 'drives' $_.Exception.Message
}

# --------------------------------------------------------- SMB shares (read) ---
# We transfer the payload over a writable NON-admin share, because C$ keeps
# refusing on a token-filtered box even after the policy is set.
try {
    $report.shares = @(
        Get-SmbShare -ErrorAction Stop | ForEach-Object {
            [ordered] @{ name = $_.Name; path = $_.Path; description = $_.Description }
        }
    )
} catch {
    Add-Problem 'shares' "Get-SmbShare unavailable: $($_.Exception.Message)"
}

# ------------------------------------------------------------- ODBC drivers ---
# No SQL Server ODBC driver => pyodbc cannot reach Digitot => no import at all.
try {
    $odbc = @(Get-OdbcDriver -ErrorAction Stop | Select-Object -ExpandProperty Name)
    $report.odbc = [ordered] @{
        all             = $odbc
        sql_server      = @($odbc | Where-Object { $_ -match 'SQL Server' })
        has_sql_driver  = [bool] (@($odbc | Where-Object { $_ -match 'SQL Server' }).Count)
    }
} catch {
    Add-Problem 'odbc' "Get-OdbcDriver unavailable: $($_.Exception.Message)"
}

# ------------------------------------------------------- SQL Server instances ---
# Enumerate from the registry, exactly as the node's discovery does.
$instances = New-Object System.Collections.ArrayList
try {
    $key = 'HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL'
    if (Test-Path $key) {
        $props = Get-ItemProperty -Path $key -ErrorAction Stop
        foreach ($name in ($props.PSObject.Properties |
                Where-Object { $_.Name -notmatch '^PS' } | Select-Object -ExpandProperty Name)) {
            # MSSQLSERVER is the default instance and is addressed as just '.'
            $connectAs = if ($name -eq 'MSSQLSERVER') { '.' } else { ".\$name" }
            [void] $instances.Add([ordered] @{ instance_name = $name; connect_as = $connectAs })
        }
    }
} catch {
    Add-Problem 'sql_registry' $_.Exception.Message
}

try {
    $report.sql_services = @(
        Get-Service -ErrorAction Stop |
            Where-Object { $_.Name -eq 'MSSQLSERVER' -or $_.Name -like 'MSSQL$*' } |
            ForEach-Object { [ordered] @{ name = $_.Name; status = "$($_.Status)" } }
    )
} catch {
    Add-Problem 'sql_services' $_.Exception.Message
}

function Invoke-SqlScalarSet {
    <#
        Run a read-only query with Integrated Security and return rows as
        hashtables. Deliberately uses no SqlClient feature beyond SELECT, and a
        short timeout so a wedged instance cannot stall recon on a live box.
    #>
    param(
        [Parameter(Mandatory)] [string] $ServerInstance,
        [Parameter(Mandatory)] [string] $Query,
        [string] $Database = 'master',
        [int]    $TimeoutSeconds = 8
    )

    $connectionString = "Server=$ServerInstance;Database=$Database;Integrated Security=True;" +
                        "Connect Timeout=$TimeoutSeconds;Application Name=SAMePOS-Recon"
    $connection = New-Object System.Data.SqlClient.SqlConnection $connectionString
    try {
        $connection.Open()
        $command = $connection.CreateCommand()
        $command.CommandText    = $Query
        $command.CommandTimeout = $TimeoutSeconds
        $reader = $command.ExecuteReader()
        $rows = New-Object System.Collections.ArrayList
        while ($reader.Read()) {
            $row = [ordered] @{}
            for ($i = 0; $i -lt $reader.FieldCount; $i++) {
                $row[$reader.GetName($i)] = if ($reader.IsDBNull($i)) { $null } else { $reader.GetValue($i) }
            }
            [void] $rows.Add($row)
        }
        $reader.Close()
        return , $rows
    } finally {
        $connection.Dispose()
    }
}

# The four columns compat fix #2 null-fills. Knowing which are absent HERE tells
# us whether the patch is load-bearing on this venue before we run the import.
$optionalArticleColumns = @('unitofquantity', 'producttype', 'itemclassification', 'defaulttaxcode')

$digitot = New-Object System.Collections.ArrayList
foreach ($inst in $instances) {
    $entry = [ordered] @{
        instance   = $inst.instance_name
        connect_as = $inst.connect_as
        reachable  = $false
        databases  = @()
    }
    try {
        $dbs = Invoke-SqlScalarSet -ServerInstance $inst.connect_as -Query @'
SELECT name, state_desc, create_date
FROM sys.databases
WHERE name LIKE 'Digitot%'
ORDER BY name
'@
        $entry.reachable = $true
        $entry.databases = @(
            foreach ($db in $dbs) {
                $dbEntry = [ordered] @{
                    name        = $db.name
                    state       = $db.state_desc
                    create_date = if ($db.create_date) { $db.create_date.ToString('o') } else { $null }
                }

                # Probe the articles schema - this is the compat-fix-#2 question.
                try {
                    $cols = Invoke-SqlScalarSet -ServerInstance $inst.connect_as -Database $db.name -Query @'
SELECT LOWER(COLUMN_NAME) AS column_name
FROM INFORMATION_SCHEMA.COLUMNS
WHERE LOWER(TABLE_NAME) = 'articles'
'@
                    $present = @($cols | ForEach-Object { $_.column_name })
                    $dbEntry.articles_table_present = [bool] $present.Count
                    $dbEntry.articles_column_count  = $present.Count
                    $dbEntry.optional_columns_missing = @(
                        $optionalArticleColumns | Where-Object { $present -notcontains $_ }
                    )
                    $dbEntry.compat_patch_2_required = [bool] $dbEntry.optional_columns_missing.Count
                } catch {
                    $dbEntry.articles_probe_error = $_.Exception.Message
                }

                # Row counts, to confirm this is the live trading DB and not a husk.
                try {
                    $counts = Invoke-SqlScalarSet -ServerInstance $inst.connect_as -Database $db.name -Query @'
SELECT
    (SELECT COUNT(*) FROM sys.tables)                                        AS table_count,
    (SELECT SUM(p.rows) FROM sys.partitions p
      JOIN sys.tables t ON t.object_id = p.object_id
     WHERE p.index_id IN (0, 1))                                             AS approx_total_rows
'@
                    if ($counts.Count) {
                        $dbEntry.table_count       = $counts[0].table_count
                        $dbEntry.approx_total_rows = $counts[0].approx_total_rows
                    }
                } catch {
                    $dbEntry.rowcount_probe_error = $_.Exception.Message
                }

                $dbEntry
            }
        )
    } catch {
        $entry.error = $_.Exception.Message
    }
    [void] $digitot.Add($entry)
}
$report.sql_instances = @($digitot)

# The single most important recon output: what the importer must be pointed at.
$candidates = @(
    foreach ($inst in $report.sql_instances) {
        foreach ($db in $inst.databases) {
            [ordered] @{
                digitot_server   = $inst.connect_as
                digitot_database = $db.name
                approx_total_rows = $db.approx_total_rows
                compat_patch_2_required = $db.compat_patch_2_required
            }
        }
    }
)
$report.digitot_candidates = $candidates
$report.digitot_verdict = if (-not $candidates.Count) {
    'NO DigitotPOS database found - a node built here would be schema-only. Confirm ' +
    'with the venue before proceeding; do not assume the wrong instance.'
} elseif ($candidates.Count -eq 1) {
    "single candidate: $($candidates[0].digitot_database) on $($candidates[0].digitot_server) - " +
    'confirm this is the live trading DB, then pin it in config\node.json'
} else {
    "$($candidates.Count) candidates found - the venue must confirm WHICH is live before import"
}

# --------------------------------------------- existing SAMePOS install state ---
# If this box is already commissioned, that changes the job entirely.
try {
    $rootItem = Get-Item -LiteralPath $InstallRoot -ErrorAction SilentlyContinue
    $report.existing_install = [ordered] @{
        root            = $InstallRoot
        exists          = [bool] $rootItem
        is_junction     = if ($rootItem) { [bool] ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) } else { $false }
        junction_target = if ($rootItem -and $rootItem.Target) { "$($rootItem.Target)" } else { $null }
        subdirectories  = if ($rootItem) {
            @(Get-ChildItem -LiteralPath $InstallRoot -Directory -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty Name)
        } else { @() }
        has_node_json   = Test-Path (Join-Path $InstallRoot 'config\node.json')
        has_app_env     = Test-Path (Join-Path $InstallRoot 'config\app.env')
    }
} catch {
    Add-Problem 'existing_install' $_.Exception.Message
}

# Bundled Python: the import fails without these four modules.
try {
    $pythonExe = Join-Path $InstallRoot 'python\python.exe'
    if (Test-Path $pythonExe) {
        $probe = & $pythonExe -c @'
import json, sys
mods = {}
for name in ("psycopg", "pyodbc", "argon2", "fastapi", "uvicorn"):
    try:
        __import__(name)
        mods[name] = True
    except Exception as exc:
        mods[name] = f"MISSING: {exc.__class__.__name__}"
print(json.dumps({"version": sys.version.split()[0], "modules": mods}))
'@ 2>&1
        $report.bundled_python = [ordered] @{
            path   = $pythonExe
            probe  = "$probe"
        }
    } else {
        $report.bundled_python = [ordered] @{ path = $pythonExe; present = $false }
    }
} catch {
    Add-Problem 'bundled_python' $_.Exception.Message
}

# ------------------------------------------------- staged installer payload ---
# The payload and update bundle are commonly already staged on the venue box.
# Find them here rather than in a second round trip, and report *completeness* --
# a partial payload is the failure that wastes the most time later.
#
# Bounded on purpose: a depth-limited search over a few likely roots, skipping
# the Windows directory. This runs on a trading box, so it must not turn into a
# full-disk crawl.
try {
    $searchRoots = New-Object System.Collections.ArrayList
    foreach ($drive in @($report.drives | ForEach-Object { $_.drive })) {
        [void] $searchRoots.Add("$drive\")
    }
    foreach ($sub in @('Users\Public\Desktop', 'Users\Public\Downloads', 'ProgramData')) {
        $candidate = Join-Path $env:SystemDrive $sub
        if (Test-Path $candidate) { [void] $searchRoots.Add($candidate) }
    }
    # Per-user Desktop/Downloads, where an operator most often drops a bundle.
    try {
        foreach ($profileDir in @(Get-ChildItem (Join-Path $env:SystemDrive 'Users') -Directory -ErrorAction Stop)) {
            foreach ($sub in @('Desktop', 'Downloads')) {
                $candidate = Join-Path $profileDir.FullName $sub
                if (Test-Path $candidate) { [void] $searchRoots.Add($candidate) }
            }
        }
    } catch { }

    $windowsDir = $env:WINDIR
    $markers = @('configure-node.ps1', 'deploy-update.ps1', 'run-server.ps1')
    $found = New-Object System.Collections.ArrayList

    foreach ($root in ($searchRoots | Select-Object -Unique)) {
        foreach ($marker in $markers) {
            try {
                $hits = Get-ChildItem -LiteralPath $root -Filter $marker -File -Recurse -Depth 3 `
                            -Force -ErrorAction SilentlyContinue |
                        Where-Object { $_.FullName -notlike "$windowsDir*" } |
                        Select-Object -First 5
                foreach ($hit in $hits) {
                    [void] $found.Add([ordered] @{
                        marker    = $marker
                        directory = $hit.DirectoryName
                        modified  = $hit.LastWriteTime.ToString('o')
                        size      = $hit.Length
                    })
                }
            } catch { }
        }
    }

    # Group by containing directory and score each as a payload or update bundle.
    $payloadDirs = @($found | ForEach-Object { $_.directory } | Select-Object -Unique)
    $report.staged_payloads = @(
        foreach ($dir in $payloadDirs) {
            $present = @(Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue |
                            Select-Object -ExpandProperty Name)
            # A complete install payload carries all of these; an update bundle
            # carries deploy-update.ps1 plus app/ and sync/ but no postgres/.
            $expectedPayload = @('app', 'db', 'postgres', 'python', 'sync', 'configure-node.ps1')
            $missingPayload  = @($expectedPayload | Where-Object { $present -notcontains $_ })
            [ordered] @{
                directory            = $dir
                entries              = $present
                has_configure_node   = ($present -contains 'configure-node.ps1')
                has_deploy_update    = ($present -contains 'deploy-update.ps1')
                missing_for_payload  = $missingPayload
                looks_like           = if (-not $missingPayload.Count) {
                    'complete install payload'
                } elseif ($present -contains 'deploy-update.ps1') {
                    'update bundle'
                } else {
                    "INCOMPLETE - missing: $($missingPayload -join ', ')"
                }
            }
        }
    )
    $report.payload_verdict = if (-not $report.staged_payloads.Count) {
        'no staged payload found in the searched roots - confirm the path with the operator'
    } else {
        $complete = @($report.staged_payloads | Where-Object { $_.looks_like -eq 'complete install payload' })
        if ($complete.Count) {
            "complete payload at: $($complete[0].directory)"
        } else {
            'candidates found but none complete - review missing_for_payload before transferring'
        }
    }
} catch {
    Add-Problem 'staged_payloads' $_.Exception.Message
}

try {
    $report.scheduled_tasks = @(
        Get-ScheduledTask -ErrorAction SilentlyContinue |
            Where-Object { $_.TaskName -like 'SamePOS*' } |
            ForEach-Object { [ordered] @{ name = $_.TaskName; state = "$($_.State)" } }
    )
} catch {
    Add-Problem 'scheduled_tasks' $_.Exception.Message
}

# Ports 8077 (app) and 5433 (embedded Postgres) must be free on a fresh node.
try {
    $report.ports = @(
        foreach ($port in 8077, 5433) {
            $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
                Select-Object -First 1
            [ordered] @{
                port      = $port
                listening = [bool] $listener
                pid       = if ($listener) { $listener.OwningProcess } else { $null }
                process   = if ($listener) {
                    (Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue).ProcessName
                } else { $null }
            }
        }
    )
} catch {
    Add-Problem 'ports' $_.Exception.Message
}

# VC++ runtime - both the embedded PostgreSQL and Python need it.
try {
    $vcKeys = @(
        'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64'
    )
    $vc = $null
    foreach ($k in $vcKeys) {
        if (Test-Path $k) { $vc = (Get-ItemProperty $k).Version; break }
    }
    $report.vc_redist_x64 = [ordered] @{ installed = [bool] $vc; version = $vc }
} catch {
    Add-Problem 'vc_redist' $_.Exception.Message
}

$report.errors = @($report.errors)
$report | ConvertTo-Json -Depth 8

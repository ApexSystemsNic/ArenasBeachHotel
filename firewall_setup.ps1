# Arenas Beach Hotel - Windows Firewall setup
#
# Lets devices on the hotel network (the local subnet of a network Windows
# treats as "Private") reach the app on port 8000, and nobody else:
#   - allows TCP 8000 from the local subnet, Private profile only
#   - blocks TCP 8000 on Public/Domain networks (e.g. a cafe or airport Wi-Fi)
#   - disables the blanket "python.exe, any port, anyone" rules Windows created
#     the first time Python asked for network access (disabled, not deleted,
#     so -Undo can restore them)
#
# Run from an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File firewall_setup.ps1          # apply
#   powershell -ExecutionPolicy Bypass -File firewall_setup.ps1 -Undo    # revert
param([switch]$Undo, [string]$LogPath)

$ErrorActionPreference = 'Stop'
$Group = 'Arenas Beach Hotel'
$Port = 8000

function Log($msg) {
    Write-Output $msg
    if ($LogPath) { Add-Content -Path $LogPath -Value $msg -Encoding utf8 }
}

try {
    if ($Undo) {
        Get-NetFirewallRule -Group $Group -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        Log "Removed the '$Group' rules."
        Get-NetFirewallRule -DisplayName 'python.exe' -ErrorAction SilentlyContinue |
            Where-Object { $_.Description -like '*Disabled by Arenas Beach Hotel firewall_setup*' } |
            ForEach-Object {
                $_ | Set-NetFirewallRule -Enabled True -Description ''
                Log "Re-enabled python.exe rule ($($_.Profile))."
            }
        exit 0
    }

    # Start clean so re-running the script never stacks duplicate rules.
    Get-NetFirewallRule -Group $Group -ErrorAction SilentlyContinue | Remove-NetFirewallRule

    New-NetFirewallRule -Group $Group -DisplayName "Arenas Beach Hotel - web app (port $Port, hotel network only)" `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
        -Profile Private -RemoteAddress LocalSubnet | Out-Null
    Log "Allowed TCP $Port from the local subnet on Private networks."

    New-NetFirewallRule -Group $Group -DisplayName "Arenas Beach Hotel - block port $Port on public networks" `
        -Direction Inbound -Action Block -Protocol TCP -LocalPort $Port `
        -Profile Public, Domain | Out-Null
    Log "Blocked TCP $Port on Public and Domain networks."

    Get-NetFirewallRule -DisplayName 'python.exe' -Direction Inbound -Action Allow -Enabled True -ErrorAction SilentlyContinue |
        ForEach-Object {
            $_ | Set-NetFirewallRule -Enabled False -Description 'Disabled by Arenas Beach Hotel firewall_setup.ps1 (run with -Undo to restore)'
            Log "Disabled blanket python.exe allow rule ($($_.Profile))."
        }

    Log 'Done.'
}
catch {
    Log "ERROR: $($_.Exception.Message)"
    exit 1
}

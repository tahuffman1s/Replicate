Replicate setup CD
==================

Replicate (https://github.com/tahuffman1s/Replicate) attaches this CD to the
SQL host VM. The unattended Windows install runs Setup-SqlHost.ps1 at the
first sign-in; it reads replicate.json, installs SQL Server 2022 Developer
with the instances listed there, gives each a fixed TCP port, creates the
Replicate login and opens the firewall.

To run it again by hand, in Windows PowerShell as Administrator:

    powershell -ExecutionPolicy Bypass -File <this CD>:\Setup-SqlHost.ps1

It skips what is already done. Progress goes to
C:\ProgramData\Replicate\setup-state.json, which Replicate reads through the
QEMU guest agent.

# Replicate

Restore SQL Server databases from VHD images into a Windows VM that Replicate
creates and runs for you on Linux, and watch what those servers are doing.

Replicate is a GTK 4 / libadwaita desktop app. It is free software
(GPL-3.0-or-later) and ships as an AppImage and a Nix flake.

![Activity: live counters, waits and requests as charts](docs/screenshots/activity.png)

| Statement trace | Images & clones | Linked servers |
| --- | --- | --- |
| ![Trace](docs/screenshots/trace.png) | ![Images](docs/screenshots/images.png) | ![Links](docs/screenshots/linked-servers.png) |

(Screenshots are rendered by `tools/smoke.py` with sample data; no VM was harmed.)

## What it does

- **Creates the SQL host VM in the background.** One dialog: pick a Windows 11
  ISO, set the instance names, type a password. Replicate writes an unattended
  install, defines a libvirt/KVM domain (UEFI, TPM, VirtIO, Hyper-V
  enlightenments), installs Windows, then SQL Server 2022 Developer with every
  instance you asked for, fixed TCP ports, a login for itself, and firewall
  rules. Close the app in the middle; the VM keeps installing and Replicate
  picks the progress back up when you return.
- **Runs the VM and the SQL Servers separately.** Start, shut down, pause and
  force off the VM; start and stop each SQL Server service on its own while the
  VM stays up. Service control goes through the QEMU guest agent, so it works
  with no network and no Windows password.
- **Takes VHD and VHDX images and makes databases out of them.** Point it at a
  folder of images: Hyper-V disks, `New-VHD` files, qcow2 or raw images,
  anything that holds data files or backups. Attaching makes a copy-on-write clone (a qcow2 overlay; the image
  is never written to) and hot-plugs it into the VM. Windows brings the disk
  online, Replicate lists the `.mdf`/`.ldf`/`.ndf` and `.bak` files on it, and
  attaches the data files in place or restores the backups, with the names you
  choose. Many clones of one image cost nothing until they are written to.
- **Keeps a server list that is yours.** Instances inside the VM register
  themselves. Add any other SQL Server you can reach, with a SQL login, a
  Windows account and password, or the Kerberos ticket your Linux desktop
  already has, and link servers to each other: a switch per pair creates a
  linked server (`sp_addlinkedserver`) with the right login mapping, and tests
  it.
- **Shows activity like a profiler, as charts.** Live CPU (SQL Server vs. the
  rest), batches, transactions and compilations per second, wait time by
  category, I/O and locking, connections, the requests running right now, and
  a statement trace built on Extended Events: event rate, duration
  distribution, the statements costing the most time, and every captured batch
  or procedure call with its text.

Nothing in Replicate is tied to a particular company, domain or share. The
only things it needs from you are a Windows 11 ISO (Microsoft hands it out at
<https://www.microsoft.com/software-download/windows11>) and a machine with
KVM.

## Install

### AppImage

Download `Replicate-<version>-x86_64.AppImage` from the
[releases](https://github.com/tahuffman1s/Replicate/releases), make it
executable, run it. The AppImage bundles GTK, libadwaita, Python, python-tds,
`qemu-img`, `virsh` and `xorriso`. The host still needs:

- **libvirt** with its daemon running and your user in the `libvirt` group
  (`sudo usermod -aG libvirt,kvm $USER`, then sign in again). Replicate talks
  to `qemu:///system`.
- **KVM** (`/dev/kvm`), **swtpm** (Windows 11 needs a TPM) and **OVMF / edk2**
  firmware, which libvirt packages pull in on Fedora, Ubuntu, Debian and Arch.
  On Ubuntu: `sudo apt install libvirt-daemon-system qemu-system-x86 swtpm ovmf`.
- **virt-viewer** (optional) to see the VM's screen from the Overview page.

### Nix / NixOS

```sh
nix run github:tahuffman1s/Replicate
```

The flake also has a NixOS module that sets libvirt up the way Replicate
expects (swtpm, OVMF with Secure Boot keys, the users in `libvirtd`):

```nix
{
  inputs.replicate.url = "github:tahuffman1s/Replicate";
  # ...
  imports = [ inputs.replicate.nixosModules.host ];
  programs.replicate = { enable = true; users = [ "you" ]; };
}
```

### From source

```sh
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
    gir1.2-secret-1 qemu-utils libvirt-clients xorriso
pip install --user python-tds
python3 -m replicate
```

## Using it

1. **Settings** → set the Windows 11 ISO, press *Download* next to virtio-win,
   and list the instance names you want (`MSSQLSERVER` is the default
   instance; `MSSQLSERVER, ACCT` gives you two). Adjust memory and CPUs.
2. **Overview** → *Create the VM*. Enter a Windows password. The first time,
   Replicate asks for your administrator password once to write the disk into
   `/var/lib/libvirt/images` (or choose a directory you own in Settings).
   Windows takes 20–40 minutes, SQL Server another 10–20. *Open screen* shows
   the VM if you are curious; nothing needs clicking.
3. **SQL Servers** → the instances appear with their ports and the `replicate`
   login. *Check* tests a connection; the row's menu has *Linked servers…*,
   *Databases…* and *Watch activity*. *Add server* registers any other SQL
   Server.
4. **Images & Clones** → *Add folder* where your VHDs are, *Attach…* one. Pick
   a clone (recommended) or the image itself, and the restore dialog opens
   once Windows sees the disk: tick the databases, rename if you like,
   *Restore selected*. *Detach* later, optionally detaching the databases and
   deleting the clone.
5. **Activity** → choose a server, *Monitor* for the counters and charts,
   *Start trace* for the statement trace. *Events* picks which Extended Events
   to capture and a minimum duration.

Configuration lives in `~/.config/replicate/config.json`, state (which clones
are attached where) in `~/.local/share/replicate/state.json`, passwords in
your keyring (libsecret) or, without one, in a 0600 file next to the config.

### Windows authentication from Linux

Three ways to sign in to a server, chosen per server in *Add server*:

- **SQL Server login**: user name and password, kept in your keyring.
- **Windows account (password)**: domain, user name and password; Replicate
  authenticates with NTLM. Works against domain accounts and against local
  Windows accounts (use the machine name, or leave the domain blank).
- **Windows account (Kerberos ticket)**: no password stored anywhere.
  Replicate uses the ticket in this computer's credential cache, the same one
  `klist` shows, through GSSAPI. If there is no ticket, *Get Ticket…* in the
  dialog logs you in to the domain (or run `kinit user@REALM`); desktops
  joined with sssd or realmd have one from login already.

**With Kerberus.** [Kerberus](https://github.com/tahuffman1s/kerberus) is a
ticket manager for the same desktops: it signs in, keeps tickets renewed in
the background and can sign in again by itself with a saved password. When it
is installed (on PATH, or as an AppImage that left its autostart or desktop
entry behind, or pointed to in *Settings → Kerberos*), Replicate works with it
instead of around it: the server dialog shows the principals Kerberus holds
and which one matches the server, *Sign in with Kerberus…* opens Kerberus's
own sign-in dialog for that principal (starting Kerberus if it is not
running), and a connection that fails for want of a ticket offers the same.
Tickets Kerberus issues are the ones Replicate uses, because both go through
the machine's credential caches; nothing is copied between the two apps.

Kerberos needs, on the Linux side, the `python3-gssapi` bindings (bundled in
the AppImage and the Nix package) and a working `/etc/krb5.conf` for the
realm, which `realm join` or `sssd` write for you; otherwise DNS discovery
of the domain controllers is enough for most Active Directory domains. On
the SQL Server side the instance has to be reachable by a name that matches
its service principal (`MSSQLSvc/host.fqdn:port`), which SQL Server
registers itself when its service account is allowed to; the dialog has a
field to override the SPN when it is not. The Replicate VM's own instances
accept Kerberos once you join that VM to a domain; until then use the SQL
login the setup created, or a Windows account with the VM's name as domain.

### How clones work

A clone is `qemu-img create -f qcow2 -b image.vhdx clone.qcow2`: QEMU reads
the VHDX directly and writes changes to the qcow2 file. The VM sees a normal
disk with a serial number Replicate chose; a PowerShell snippet run through the
guest agent finds the disk by that serial, brings it online, assigns drive
letters and lists database files. Databases are then attached with `CREATE
DATABASE … FOR ATTACH` or restored with `RESTORE DATABASE … WITH MOVE`, over a
normal TDS connection from the host.

Differencing VHDX disks (children of a parent VHDX) cannot be opened by QEMU;
merge them in Windows first (`Merge-VHD`) or use the parent image, which is
what you usually want anyway.

### The VM's own setup script

`replicate/guest/Setup-SqlHost.ps1` runs inside the VM at the first sign-in
and is safe to run again by hand (it is also on the `REPLICATE_SETUP` CD
attached to the VM). It installs SQL Server 2022 Developer for each instance
listed in `replicate.json`, sets fixed TCP ports (1433 for the default
instance, 14331, 14332, … for named ones), creates the `replicate` login as
sysadmin, and opens the firewall. Progress goes to
`C:\ProgramData\Replicate\setup-state.json`, which the host reads.

## Development

```sh
nix develop            # or install the packages listed under "From source"
python3 -m pytest      # unit tests (the VHDX clone test needs qemu-img)
python3 tools/smoke.py # start the app, render every page to tools/screenshots/
packaging/appimage/build-appimage.sh   # on Ubuntu 24.04
```

Layout:

```
replicate/
  config.py, secrets.py, paths.py, tasks.py   configuration, keyring, threads
  vm/        libvirt domain XML, virsh wrapper, guest agent, unattended install, VM manager
  vhd/       image library, qcow2 clones, hot-plug + online in Windows, attach/restore T-SQL
  sql/       python-tds connection, server registry + linked servers, monitor, XE profiler
  guest/     Autounattend.xml.in, Setup-SqlHost.ps1 (go on the setup CD)
  ui/        libadwaita app: pages, dialogs, cairo charts
packaging/appimage/   appimage-builder recipe and script
flake.nix             package, dev shell, NixOS host module
```

## License

GPL-3.0-or-later. See `LICENSE`.

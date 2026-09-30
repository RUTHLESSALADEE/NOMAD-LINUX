# NOMAD

**Network Operations, Monitoring And Diagnostics**: one Windows app to set up network adapters, troubleshoot networks, talk to devices and keep track of IP addresses. (Formerly NIC Manager, with the RADAR subnet sweep and the Latenct latency monitor built in.)

![The Interfaces page](docs/screenshots/interfaces.png)

- **Works offline.** Tests only talk to the hosts you give them, and the MAC vendor list is built in.
- **One exe, nothing to install.** `NOMAD-<version>.exe` runs on its own ([build it](#running-from-source-and-building) with `build.ps1`).
- **Safe changes.** New IP settings, and disabling an adapter, revert on their own unless you confirm within 15 seconds, so a change that cuts off your remote session undoes itself.

Screenshots use made-up demo data.

## Contents

- [Getting around](#getting-around)
- [Pages](#pages)
- [Terminal and SCP](#terminal-and-scp)
- [IP address management (IPAM)](#ip-address-management-ipam)
- [Sharing IPAM with the tribe](#sharing-ipam-with-the-tribe)
- [Good to know](#good-to-know)
- [Running from source and building](#running-from-source-and-building)

## Getting around

- Pick a page from the **sidebar** (Ctrl+Tab and Ctrl+Shift+Tab move between pages). Hide it with **«**, View > Show Sidebar or Ctrl+B.
- Pick an **adapter** at the top of the window. Interfaces, MTU, Ping, Sweep, DHCP Servers and other pages work with it.
- **F11** (focus mode) hides everything but the current page.
- **View > Text Size** scales all text from 90% to 200% (Ctrl+= and Ctrl+- too, and Ctrl+0 to reset).

## Pages

### This Computer

| Page | Function |
| --- | --- |
| **Interfaces** | The adapter's status, addresses, gateway, DNS, speed and MAC. Switch between DHCP and a static IP (CIDR like `192.168.1.10/24` works), set DNS and MTU, and enable, disable, reset, release or renew. **Free Address from IPAM** fills in the next free address in a subnet you pick. Save settings as **profiles** to switch networks in one click. |
| **Routing Table** | View, filter, add, edit and delete IPv4 and IPv6 routes, persistent ones included. Type an address in the filter to see which route Windows would use for it. |
| **ARP** | Which MAC answers for each IP, with vendors. Warns about possible ARP spoofing and address conflicts. |
| **Connections** | Every TCP connection and listening port with the program that owns it, like `netstat -ano`. |
| **Network Reset** | Flush DNS, clear ARP, renew DHCP, reset Winsock and TCP/IP, and turn off a leftover proxy. |

![The Routing Table page](docs/screenshots/routing.png)

### Connect

| Page | What it does |
| --- | --- |
| **Terminal** | SSH, Telnet, serial and raw TCP sessions in tabs, tiles and pop-out windows. [More below.](#terminal-and-scp) |
| **SCP** | A WinSCP-style file manager for SSH servers. [More below.](#terminal-and-scp) |

### Manage

| Page | Functions |
| --- | --- |
| **IP Addresses** | IP address management for several separate networks, shared with the tribe through an IPAM server and usable offline. [More below.](#ip-address-management-ipam) |

### Test

| Page | Functions |
| --- | --- |
| **Ping** | Ping with a running summary; quick buttons for the gateway and DNS server. |
| **Latency** | Watches several hosts at once, with gauges, a graph over time (lost pings in red) and optional CSV logging. |
| **Traceroute** | Loss, latency and jitter for every hop, like MTR/WinMTR. Every hop is probed at once, so a trace takes seconds. |
| **MTU** | Finds the largest MTU that reaches a host without fragmenting, and applies it. |
| **Ports** | Open, closed or filtered, for common port presets or any list and range. |
| **iperf** | Bandwidth tests with a built-in iperf3-compatible client and server. |

### Discover

| Page | Functions |
| --- | --- |
| **Sweep** | Finds every host on a subnet (ping, plus ARP on local subnets), with names, MACs and vendors. **Compare with IPAM** shows which hosts IPAM has, is missing or records with a different MAC. |
| **Switch Port** | Which switch, port and VLAN you're plugged into, from LLDP and CDP. |
| **DHCP Servers** | Every DHCP server that answers, with each option it offers decoded, and warns about rogue servers. Nothing is leased. |
| **SNMP** | Walk or get over SNMP v1/v2c, with presets and a per-port interface summary. |

### DNS & Web

| Page | Functions |
| --- | --- |
| **DNS Lookup** | A, AAAA, MX, TXT and other records, from any DNS server. |
| **DNS Servers** | Times how fast each DNS server answers, and checks reverse (PTR) records. |
| **Web Check** | DNS, connect, TLS and first-byte timings, the certificate, and redirects. |

### Tools

| Page | Functions |
| --- | --- |
| **Packet Capture** | Captures to a pcapng file for Wireshark with Windows' built-in pktmon. Nothing to install. |
| **Syslog** | Receives syslog from network devices, coloured by severity and filterable. |
| **TFTP** | A TFTP server and client for firmware and config transfers. |
| **Subnet Calculator** | Network, mask, host range and counts for IPv4 and IPv6, and splitting a network into smaller subnets. |
| **Wake-on-LAN** | Wake a computer by its MAC, and save the ones you wake often. |

![The Subnet Calculator](docs/screenshots/subnet-calculator.png)

## Terminal and SCP

![Two sessions side by side on the Terminal page](docs/screenshots/terminal.png)

**Sessions**

- Save sessions in folders, and import them from PuTTY or a SecureCRT export.
- **Quick connect** takes `admin@10.0.0.1`, `telnet 10.0.0.5`, `raw 10.0.0.9:9100` or `COM3:115200`.
- **Recent** at the top of the list keeps your last 10 connections.
- **Layout** tiles sessions side by side, stacked, or in a 2 × 2 or 3 × 2 grid. Drag tabs between panes and windows.
- Pop any tab out into its own window (right-click the tab).

**Working with many devices**

- **Send to All** sends a command (or Ctrl+C, Ctrl+Z, Ctrl+Shift+6...) to every session, and **Type in All** mirrors your typing.
- **Command buttons** send saved commands or blocks of configuration with one click, or with Ctrl+1 to Ctrl+9 (even with the Buttons bar hidden). Drag a button, or right-click it > Move to Position, to change the order and so its hotkey.
- **Keyword highlighting** colours down, err-disabled, % Invalid, up, and IP and MAC addresses. Change the rules in View > Terminal Keyword Highlighting.

**Staying connected**

- A per-session **line delay** keeps slow consoles from dropping pasted text.
- **Reconnect automatically** when a device reloads.
- **Anti-idle** keystrokes keep exec-timeout from logging you out.

**Security**

- SSH logs in with a password, an OpenSSH private key, or Pageant/SSH agent.
- Saved passwords are encrypted for your Windows account (DPAPI). Add a **master password** (AES-256) for more protection.
- Host keys are remembered, and NOMAD warns if one changes.
- Older switches that only speak SHA-1 SSH algorithms still connect.

**Serial**

- Serial sessions list the COM ports present, and can send a break (for Cisco ROMMON).

**Keys**

- Ctrl+Shift+F finds text, Shift+PgUp scrolls back, and Ctrl+mouse wheel zooms.
- Keys like Ctrl+B and F5 go to the device, not to NOMAD.

**SCP** is a WinSCP-style file manager using the same saved sessions:

- Drag files between your computer and the server, or press F5.
- F4 edits a remote file in place; F2 renames, F7 makes a folder, F8 deletes.
- Change permissions and owners (chmod and chown).
- **Work as Root** (sudo) browses, copies and edits as root.
- Transfers queue with progress, can be paused and resumed, and can be checked with SHA-256.
- **Synchronize** compares a local and a remote folder and copies the differences.

## IP address management (IPAM)

The **IP Addresses** page keeps track of addresses for several separate networks, such as air-gapped ones that reuse the same ranges.

![The IP Addresses page](docs/screenshots/ip-addresses.png)

**Everyday use**

- **Subnets as a tree** (blocks hold the subnets inside them), each with how much is used.
- **Every address** in a subnet as used, reserved or free, or as its network, broadcast or gateway address.
- **Use Next Free** records the lowest free address.
- **Loopback subnets:** every address is its own /32, with no network, broadcast or gateway address.
- **Select several** addresses or subnets to change them together.
- **Find Free Blocks** (right-click a subnet) shows the unused space in it, ready to add new subnets in.

**Sweeps and Last Seen**

- **Sweep Subnet** pings every address in place. **Last Seen** shows when each address last answered:
  - red where IPAM says used but nothing answered;
  - amber where something answered that IPAM doesn't have.
- **Record Answering Devices** and **Update MACs** save what a sweep found.
- Sweep results are kept, and shared through the IPAM server with who swept, including sweeps made offline.

**Search**

Search every network, or narrow it to subnets, addresses, one network, or one field (such as a name, a MAC, or a detail column from your workbook).

![Searching one detail column](docs/screenshots/ipam-search.png)

**Workbooks**

- **Import Spreadsheet** reads your addressing workbook (`.xlsx`, a network per page, or a `.csv` of one page).
  - Where a page's summary and its detailed listing disagree, you choose which to keep.
  - Impossible gateways get a suggested fix.
  - Unusable rows are listed by row number.
  - SNMP strings are never imported.
- **Export to Workbook** writes networks back in the same layout, and it imports back unchanged. **Export to CSV** is there too.
- **Compare with Workbook** lists every difference from a newer copy of the workbook to tick and apply, instead of importing it again. Edits made in NOMAD since the import are left unticked.

![Compare with Workbook](docs/screenshots/compare-workbook.png)

**Checking and history**

- **Check Data** lists likely mistakes, most serious first. Double-click one to go to it. It looks for:
  - a device on a network or broadcast address;
  - addresses outside every subnet;
  - names that look like a stray note;
  - "Loopback" subnets not marked as loopbacks;
  - duplicate MACs, and more.
- **History** shows every change with who made it and when: an address, a subnet, or the whole network.
- **View As Of** shows a network as it was at any moment.

![Check Data](docs/screenshots/check-data.png)

**On the Interfaces page**, **Free Address from IPAM** fills in the next free address, mask and gateway. It records the address in IPAM once you keep the new settings.

## Sharing IPAM with the tribe

Networks are either **Local** (on this computer) or **Tribe** (shared by an IPAM server on one always-on Windows machine).

**Setting up the server**

1. On the server machine, run NOMAD as administrator and open **Tools > IPAM Server**.
2. Click **Install Service**. It sets up the database, a certificate and nightly backups (kept 14 days) in `%ProgramData%\NOMAD\server`, installs the NOMAD IPAM Server service, and opens TCP port 8443.
3. **Save Tribe Key File** and give it only to the tribe: anyone with it can change the tribe's IPAM. **Change Tribe Key** locks out every old copy.
4. After updating NOMAD, click **Update Service** in the same window.

Spreadsheets are imported, and tribe networks added or deleted, only in NOMAD on the server itself (running as administrator).

**On each laptop**

1. **Tribe > Connect with Tribe Key File.**
2. The laptop keeps a copy of the tribe's networks and history, so lookups work offline.
3. It syncs the moment anyone changes anything.

**Offline and conflicts**

- **Offline**, you can still assign, edit and free addresses. Changes wait as *pending* and are sent in order when the server is back.
- If someone else changed the same address first, **Review Refused Changes** lets you take the next free address instead, or discard yours.
- Subnets and networks can only be changed online.

**Troubleshooting:** stop the service and run `NOMAD.exe --ipam-server` to run the server in a console. Its log is `%ProgramData%\NOMAD\server\server.log`.

## Good to know

- **Administrator rights:** NOMAD starts without them, so viewing and testing work straight away. When a change needs them, it offers to restart as administrator (or use File > Restart as Administrator).
- **Diagnostics report:** Tools > Run Diagnostics Report (Ctrl+R) checks the adapter, gateway, DNS, internet access, route, path MTU and ARP table in about half a minute. It saves the findings as a web page to attach to a ticket.
- **Updating the MAC vendor list:** download https://standards-oui.ieee.org/oui/oui.csv and run `python -m nomad.oui build oui.csv`.

**Shortcuts**

| Keys | Does |
| --- | --- |
| F5 | Refresh |
| Ctrl+Tab / Ctrl+Shift+Tab | Next / previous page |
| Ctrl+B | Hide or show the sidebar |
| F11 | Focus mode |
| Ctrl+R | Diagnostics report |
| Ctrl+F | Filter the routing table |
| Ctrl+1 to Ctrl+9 | The first nine command buttons (in a Terminal session) |
| Ctrl+= / Ctrl+- / Ctrl+0 | Text size |

**Where things are kept**

| What | Where |
| --- | --- |
| Terminal sessions and SSH host keys | `%APPDATA%\NOMAD\sessions.json`, `known_hosts` |
| Profiles | `%APPDATA%\NOMAD\profiles.json` |
| Local IPAM networks | `%APPDATA%\NOMAD\ipam.db` |
| Log (Tools > View Log) | `%LOCALAPPDATA%\NOMAD\nomad.log` |
| IPAM server | `%ProgramData%\NOMAD\server` |

The first time NOMAD runs, it moves over the folders and settings from NIC Manager.

## Running from source and building

    pip install -r requirements.txt
    pythonw Main.py

Tests, and a standalone exe (`dist\NOMAD-<version>.exe`):

    pip install -r requirements-dev.txt
    python -m pytest
    .\build.ps1

**Releases:** the version lives in `nomad\__init__.py`. Note changes under Unreleased in `CHANGELOG.md` as you go, then:

    python -m nomad.version bump patch     (or minor / major)
    git add -A
    git commit -m "Release X.Y.Z"
    git tag vX.Y.Z
    .\build.ps1

## Third-party software

NOMAD bundles PyQt5 (GPL), paramiko (LGPL 2.1) for SSH, pyte (LGPL 3) for terminal emulation, pyserial (BSD) for serial ports, openpyxl (MIT) for workbooks, and pywin32 (PSF) for the IPAM server service.

Happy troubleshooting!

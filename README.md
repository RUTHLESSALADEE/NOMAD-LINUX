# NOMAD

**Network Operations, Monitoring And Diagnostics**: a one stop shop to manage Network Interface Cards (NICs) and troubleshoot networks in Windows. (Formerly NIC Manager, now with the RADAR subnet sweep built in.)

Pick a page from the sidebar, grouped into This Computer, Connect, Manage, Test, Discover, DNS & Web and Tools (Ctrl+Tab and Ctrl+Shift+Tab move between pages). Hide the sidebar for more room with its « button, View > Show Sidebar or Ctrl+B; a slim strip then keeps a menu of every page and a button to bring it back. Pick an adapter at the top of the window; the Interfaces, MTU, Ping, Sweep, DHCP Servers and other pages work with it. NOMAD always opens on the Interfaces page.

Everything works without internet access: the tests only talk to the hosts you give them, and the MAC vendor list is built in (to update it, download https://standards-oui.ieee.org/oui/oui.csv and run python -m nomad.oui build oui.csv).

This Computer:
  Interfaces - shows the adapter's status, addresses, gateway, DNS servers, link speed and MAC address. Lets you switch between DHCP and a static IP (CIDR notation like 192.168.1.10/24 works), set DNS servers and MTU, and enable/disable, reset, release or renew the adapter. After changing IP settings you get 15 seconds to confirm; if you don't (say the change cut off your remote session), the previous settings come back automatically. Save settings as named profiles to switch networks in one click, and import/export profiles to share them.
  Routing Table - view, filter, sort, add, edit and delete IPv4 and IPv6 routes, including persistent routes that survive a reboot. System routes are hidden by default.
  ARP - the ARP (IPv4) and neighbor (IPv6) tables: which MAC address answers for each IP, with the vendor of each device. Warns when one MAC answers for the gateway and other addresses (possible ARP spoofing), when an address starts answering from a different MAC (two devices sharing an IP), and when Windows reports that another device is using this computer's address. Delete an entry or clear the whole cache (like arp -d *).
  Connections - every TCP connection and listening TCP/UDP port with the program that owns it, like netstat -ano. Filter by port, address or program to see what's using a port; optionally refresh every 2 seconds.
  Network Reset - fixes for a damaged network stack: flush the DNS cache, clear the ARP cache, renew every DHCP lease, reset Winsock and reset TCP/IP (the resets ask first and offer to restart the computer), and open Windows' own full network reset. Also shows this user's proxy and the machine-wide WinHTTP proxy used by Windows services, with one click to turn a leftover proxy off (and Undo) or reset the WinHTTP proxy.

Connect:
  Terminal - an SSH, Telnet, serial and raw TCP client in the style of MobaXterm and SecureCRT. Save sessions in folders (import them from PuTTY in one click), open them in tabs, and pop any tab out into its own window (right-click the tab). Quick connect takes admin@10.0.0.1, telnet 10.0.0.5, raw 10.0.0.9:9100 or COM3:115200. For more room, « beside the tabs hides the session list (Sessions ▾ then lists your saved sessions), and F11 (Focus mode) hides everything but the sessions; F11 in a pop-out window makes it full screen. Select text to copy it and right-click to paste; Ctrl+Shift+F finds text in the scrollback, Shift+PgUp scrolls back and Ctrl+mouse wheel zooms. Keys like Ctrl+B, Ctrl+R and F5 go to the device, not to NOMAD. SSH logs in with a password, a private key (OpenSSH format) or Pageant/SSH agent; saved passwords and key passphrases are encrypted for your Windows account (DPAPI), so they can't be read on another computer or account. For more protection, set a master password (Master Password... on the Terminal page, or Tools > Saved Password Protection): saved passwords are then also encrypted with AES-256 using a key made from it (scrypt), so reading them needs both your Windows account and the master password. NOMAD asks for it once when a saved password is first needed, and can lock again after 15 minutes, 1 hour or 4 hours unused. A forgotten master password can't be recovered; Forget All Saved Passwords starts over (sessions are kept). NOMAD remembers each device's host key and warns if it changes. Older switches that only support SHA-1 SSH algorithms still connect (with a note suggesting a firmware update). Serial sessions list the COM ports present and can send a break (for Cisco ROMMON password recovery). Sessions can log everything to a text file. Sweep and Ports open SSH and Telnet sessions here.

Manage:
  IP Addresses - IP address management (IPAM) for several separate networks, such as air-gapped ones, which may reuse the same ranges. Pick a network to see its subnets as a tree (blocks hold the subnets inside them) with how much of each is used; select one to see every address in it as used, reserved, free, or the network, broadcast or gateway address, and record or free addresses. Use Next Free records the lowest free address; the search box finds an address, name or subnet in every network. Import Spreadsheet reads the team's addressing workbooks (.xlsx, where every page becomes a network, or a .csv of one page): where the summary at the top of a page and its Detailed Info disagree, you choose which to keep; where a gateway isn't in its subnet, a likely one is suggested for you to accept or change; and rows that can't be used are listed with their row numbers. The window can be maximized or made full screen (F11). An address written with a mask stands for the subnet holding it (172.28.101.0/16 is 172.28.0.0/16), and a summary row for an address inside a subnet already listed is recorded as that address. SNMP strings in the workbook are never imported. Export a network to CSV from the Network menu. Every change records who made it and when. For now the data is kept on this computer only (%APPDATA%\NOMAD\ipam.db); syncing with a NOMAD server everyone shares is coming.

Test:
  Ping - ping a host with a running summary (loss, min/avg/max). Quick buttons ping the adapter's gateway or DNS server.
  Latency - monitors several hosts at once (Google and Cloudflare DNS to start; add your gateway with one click), with a gauge per host, a graph of latency over time with lost pings marked in red, and last/average/min/max/loss for each. Switch hosts on and off while it runs, view the last minute up to the last 8 hours, and optionally log every ping to CSV. Compact shows just the gauges and graph. (This replaces the separate Latenct tool.)
  Traceroute - shows each router on the way to a host, with loss, last/average/best/worst latency and jitter (standard deviation) for each hop, like MTR / WinMTR. Every hop is probed at once, so a trace takes seconds. Run a set number of probes per hop, or keep it running to watch a problem develop; Copy Report gives a text table to send to an ISP.
  MTU - finds the largest MTU that reaches a remote host without fragmenting, from the selected adapter, and applies it with one click.
  Ports - checks whether TCP ports on a host are open, closed (refused) or filtered (no answer, usually a firewall), with presets for common, web, remote access and file sharing ports, or any list and ranges up to all 65535. Open ports can be opened in the browser, Remote Desktop or PuTTY from the results.
  iperf - measures bandwidth (TCP or UDP, upload or download, parallel streams) with a built-in iperf3-compatible client and server, so no iperf3 download is needed. Test against any iperf3 server, or switch to server mode and run iperf3 -c <this computer> (or another copy of NOMAD) elsewhere. Open Firewall Port adds the Windows Firewall rule server mode needs.

Discover:
  Sweep - finds every host on an IPv4 subnet that answers ping (hosts that miss are retried, 3 tries in all). On subnets this computer is directly connected to it also uses ARP, which finds devices whose firewall drops ping, and shows each host's MAC address and vendor. Host names come from DNS, or from NetBIOS on networks without a DNS server. One click fills in the selected adapter's subnet. From the results, open an SSH session in PuTTY, open the host's web page, ping, trace, scan ports or monitor its latency; copy the addresses or export them (with names, MACs and vendors) to CSV. Tools > Add PuTTY to PATH and Tools > Default Browser Settings help set up those actions. (This replaces the separate RADAR tool.)
  Switch Port - shows which switch, port and VLAN (and voice VLAN) the computer is plugged into, from the LLDP and CDP announcements managed switches send every 30-60 seconds, along with the switch's model, management address and software. Uses Windows' built-in packet monitor (pktmon), so nothing needs installing, but it needs administrator rights.
  DHCP Servers - asks the selected adapter's network for a DHCP offer and lists every server that answers, with the address, gateway, DNS servers and lease each one offers. Select a server to see every option in its offer, named and decoded (NTP and WINS servers, static routes, domain search list, TFTP and boot servers for PXE and IP phones, vendor data and anything else it sends), and copy them as text. Any server other than the one this adapter's lease came from is flagged as a possible rogue DHCP server (such as a home router plugged in the wrong way round). Only a request is sent: no address is taken.
  SNMP - reads switches, routers, printers and UPSes over SNMP v1/v2c with a community string: walk any part of the MIB (presets for system details, interfaces, IP and ARP tables, MAC address tables, LLDP neighbors and hardware serial numbers) or get one value, with names for common OIDs. Interface Summary gives one row per port with its name, description, status, speed and error counters. Copy or export to CSV. (SNMPv3 isn't supported yet.)

DNS & Web:
  DNS Lookup - look up A, AAAA, MX, TXT and other records, optionally against a specific DNS server.
  DNS Servers - Times how fast each DNS server answers (the adapter's, the gateway, well-known public servers and any you add; remove any you don't want with Remove Selected or the Delete key, and Restore Removed brings them back), asking each directly so Windows' cache doesn't skew the result, and checks that a name's addresses have PTR records pointing back to it.
  Web Check - Fetches a page and shows how long DNS, connecting, the TLS handshake and the first byte took, the certificate (who issued it, the names it covers, days until it expires, and why it isn't trusted if it isn't) and the reply, following redirects.

Tools:
  Packet Capture - captures traffic on every adapter with Windows' built-in packet monitor (pktmon) and saves a pcapng file that opens in Wireshark, optionally filtered by address, port and protocol, with a size limit and an automatic stop time. Needs administrator rights; nothing to install.
  Syslog - receives syslog messages (UDP, and optionally TCP) from switches, firewalls and access points, understands both the BSD and RFC 5424 formats, colours them by severity, and filters by severity or text. Save them, or write everything to a log file as it arrives. Open Firewall Port adds the Windows Firewall rule.
  TFTP - a TFTP server for firmware and config transfers (serve a folder; optionally accept uploads, such as config backups, without replacing existing files), with a live list of transfers, and a TFTP client to download from or upload to another server. Supports large blocks for speed, and falls back to the basic protocol for old devices.
  Utilities - a subnet calculator (network, netmask, broadcast, host range and counts for IPv4 and IPv6, and splitting a network into smaller subnets by prefix or by hosts needed) and Wake-on-LAN (wake a computer by its MAC address, save devices you wake often, or pick one from the Sweep or ARP page).

The program starts without administrator rights, so viewing, ping, latency monitoring, traceroute, port scans, sweeps and lookups work straight away. Changing settings needs administrator rights; NOMAD offers to restart itself as administrator when you first try (switch discovery, packet capture and the network resets need it too) (or use File > Restart as Administrator).

Diagnostics report: Tools > Run Diagnostics Report (Ctrl+R) checks the selected adapter's settings, the gateway, DNS, internet access, the route to the internet, the path MTU and the ARP table in about half a minute, then saves the findings as a web page to email or attach to a ticket. On a network without internet access it says so rather than reporting a fault.

Text size: View > Text Size makes all text from 90% to 200% of normal, and NOMAD remembers the choice. Ctrl+= and Ctrl+- also change it, and Ctrl+0 goes back to the default.

Shortcuts: F5 refreshes, Ctrl+Tab / Ctrl+Shift+Tab move between pages, Ctrl+B hides or shows the sidebar, F11 switches focus mode on and off, Ctrl+R runs a diagnostics report, Ctrl+F filters the routing table, Delete removes the selected route, Ctrl+= / Ctrl+- / Ctrl+0 change the text size.

Terminal sessions are saved in %APPDATA%\NOMAD\sessions.json and known SSH host keys in %APPDATA%\NOMAD\known_hosts.

Logs are written to %LOCALAPPDATA%\NOMAD\nomad.log (Tools > View Log). Profiles are saved in %APPDATA%\NOMAD\profiles.json. The first time NOMAD runs it moves over the folders and settings from NIC Manager.

Running from source:

    pip install -r requirements.txt
    pythonw Main.py

Running the tests and building a standalone exe (dist\NOMAD-<version>.exe, e.g. dist\NOMAD-1.0.0.exe):

    pip install -r requirements-dev.txt
    python -m pytest
    .\build.ps1

Versions and releases: the version is set in nomad\__init__.py and shows in the title bar, Help > About, the log, the exe's file name and its Properties > Details. Note changes under Unreleased in CHANGELOG.md as you go, then release with:

    python -m nomad.version bump patch     (or minor / major)
    git add -A
    git commit -m "Release X.Y.Z"
    git tag vX.Y.Z
    .\build.ps1

Screenshots below are from an earlier version, before the sidebar.

NIC Tab:

![image](https://github.com/user-attachments/assets/e37cd314-97d2-4cba-8dd0-3faa197ae232)

Routing Table Tab:

![image](https://github.com/user-attachments/assets/ce43098e-7e4b-420a-afb9-679cb0687f4b)

MTU Tab:

![image](https://github.com/user-attachments/assets/d9809bb0-f1e3-4d29-86b7-0aec12695b8b)

Third-party software: NOMAD uses PyQt5 (GPL), paramiko (LGPL 2.1) for SSH, pyte (LGPL 3) for terminal emulation and pyserial (BSD) for serial ports, all bundled in the exe.

Happy troubleshooting!

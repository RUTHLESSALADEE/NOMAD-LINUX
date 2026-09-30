# Changelog

Notable changes to NOMAD. Versions follow [semantic versioning](https://semver.org/): MAJOR for big or breaking changes, MINOR for new features, PATCH for fixes.

Add changes under Unreleased as you go; `python -m nomad.version bump <part>` dates them when you release.

## [Unreleased]

## [1.8.0] - 2026-09-29

### Added

- Command buttons on the Terminal page (Buttons, beside Send to All; also View > Terminal Command Buttons): saved commands, or whole blocks of configuration, sent with one click to the session you're in, or to every session while Type in All is on. Right-click a button to send it to all, edit, duplicate, move or delete it. Kept in %APPDATA%\NOMAD\commands.json, shared by every window.
- Line delay (session settings > Sending and Staying Connected): pasting several lines, a command button or Send to All sends them one at a time, so consoles and older switches don't drop characters. The status line shows progress, and right-click the tab > Stop Sending drops the rest.
- Keyword highlighting in terminal sessions: down, err-disabled, % Invalid, notconnect, up, IP and MAC addresses and more in colour, only where the device didn't colour the text itself. Change the words and colours in View > Terminal Keyword Highlighting, or switch it off in View > Highlight Terminal Keywords.
- Reconnect automatically (session settings, or right-click the tab): when the connection drops, such as a device reloading, NOMAD tries again every 10 seconds (for up to half an hour) until it's back. Not after you type exit or logout, or disconnect it yourself; right-click the tab > Stop Reconnecting stops it.
- Anti-idle (session settings): after a set time without typing, send a space and a backspace (or any text you choose), so the device's exec-timeout doesn't log you out.
- Send to All on the Terminal page (beside Layout): type a command once and send it to every connected session, the sessions on screen, or this window's, each with its own Enter; Up and Down bring back earlier commands. Ctrl+C in its box interrupts every session (unless text in the box is selected, which it copies), Ctrl+Z with the box empty sends Ctrl+Z, and Ctrl+Shift+6 sends Cisco's abort; the Keys menu sends these and Tab, Space, q, Esc and Enter to all. Type in All mirrors your typing into all of them as you go (the bar turns amber while it's on, and closing the bar turns it off). Right-click a tab > Leave Out of Send to All to skip a session (its tab shows ⊘).
- Tiling on the Terminal page: Layout (beside the tabs) shows several sessions at once: two or three side by side or stacked, or a 2 × 2 or 3 × 2 grid, with draggable splitters. Each pane has its own tabs: drag a tab onto another pane, or right-click it > Move to, to move the session there (it stays connected), even into a pane of another window. An empty pane has an Open Session menu (saved sessions, Recent and Quick Connect) that opens a session in that pane, and pop-out windows have a Sessions menu of their own, so sessions can be opened straight into them. Clicking in a pane makes it the active one (outlined in the accent color); new sessions open in an empty pane, or else the active one. Also in View > Terminal Layout. Pop-out windows can be tiled too, and the page remembers its layout.
- Route lookup on the Routing Table page: type an address or network in the filter (such as 10.1.2.3, 10.1.0.0/16, or a partly typed 10.1.) to see every route that covers it, as a router's lookup would, system routes included. The route Windows would use (longest prefix, then lowest metric) is marked ► and named under the table.

### Changed

- The Utilities page is now two pages under Tools: Subnet Calculator and Wake-on-LAN. Saved devices and the last subnet carry over.

### Fixed

- Adding a persistent route failed, because New-NetRoute doesn't accept the persistent store directly. It's now added to both stores at once (replacing an active-only copy of the same route first), and deleting a route removes every matching copy.

## [1.7.0] - 2026-09-29

### Added

- Work as root on the SCP page: right-click the tab (or the remote side) > Work as Root (sudo), or tick "Work as root (sudo) on the SCP page" in a session's settings. The tab reconnects with SFTP run through sudo (as WinSCP does), so browsing, copying, editing and Synchronize all happen as root, and the remote side's title turns amber: "Remote (as root)". NOMAD tries your login password for sudo and asks only if that doesn't work (servers where sudo needs no password just work); the password is kept in memory for that connection only. It says clearly when the user isn't allowed to use sudo, when sudo needs a terminal (requiretty), or when the server's sftp-server can't be found. Needs SFTP (the session's File transfer set to Auto or SFTP).
- Changing owners on the SCP page: Properties has User and Group fields, listing the server's users and groups (numbers work too), and can apply them to everything inside a folder. Links are left alone. A "Permission denied" message suggests Work as Root.

### Fixed

- Selecting text in a terminal while the device was printing could fail with "cannot unpack non-iterable NoneType object" when the mouse button was let go. The selection now stays on the text it covers as new output scrolls it up (as in PuTTY), and is copied when you let go. Once the scrollback was full, it could also copy a different line from the one selected; that's fixed too. While scrolled back, the view keeps showing the same text when the scrollback is full.

## [1.6.2] - 2026-09-29

### Added

- Upload ▶ and ◀ Download buttons under the SCP page's panes copy the selected files to the other side (as F5 does).
- File transfer setting for SSH sessions (Edit Session): Auto uses SFTP when the server has it and SCP otherwise, as before; SFTP only or SCP can be chosen for servers whose SFTP is broken rather than missing.

### Fixed

- Answering "Do the same for the rest of the queue" when a file exists no longer carries on to files queued later: once the queue runs out, the next file that exists is asked about again (or follows the "If it exists" setting).
- Downloading or synchronizing a folder that has links to other folders in it copies what the links lead to, instead of making empty folders. A link that leads back up the tree is skipped. Delete and Permissions still act on a link itself, never on what it leads to.
- The SCP page's local side lists folders in the background, and finds drives without asking each one, so a slow network share or a disconnected mapped drive no longer freezes NOMAD.
- Cancelling an upload in SCP mode (servers without SFTP) removes the part-written file from the server.
- Copies downloaded for Edit With that were left behind (NOMAD didn't close normally) are deleted once they're two days old.
- Cancelling (or pausing) an SCP transfer now stops it straight away. Before, a big download kept going in the background until the rest of the file had arrived, so Cancel seemed to hang; downloads now read ahead 16 MB at a time instead of the whole file, which also keeps memory use down.
- A transfer that stops getting data (Wi-Fi off, VPN dropped, server busy) shows "Stalled: no data for N s" in the queue instead of "Transferring". It carries on by itself if the connection comes back, and Cancel works while it's stalled.
- Browsing on the SCP page gives up after 30 seconds without an answer from the server, instead of showing "Listing..." for ever.
- SSH logins (Terminal and SCP) could occasionally wait 30 seconds and then report a correct password as wrong, when the server answered very quickly. This came from paramiko sending the login request before it was ready to hear the answer.

## [1.6.1] - 2026-09-28

### Added

- SCP wherever SSH is offered in a right-click menu: Open SCP Session on the Sweep and Ports pages (for port 22), SCP on an address on the IP Addresses page, and Open in SCP on SSH sessions in the Terminal page's session list, its Recent list and its tabs. The SCP page offers Open in Terminal the same way.

## [1.6.0] - 2026-09-28

### Changed

- The master password status under the Terminal page's session list ("Master password locked", "unlocked" or "No master password") is now a menu: Unlock or Lock Now, and Master Password Settings (or Set Master Password). It replaces the ⋯ menu: importing sessions moved to File > Import Sessions from PuTTY / SecureCRT, and a new Edit menu has New Session, New Folder and Clear Recent Connections. The master password menu is a button like New.
- File > Import Profiles and Export Profiles are now Import Interface Profiles and Export Interface Profiles, to tell them apart from importing terminal sessions.

### Added

- SCP page (Connect > SCP): a WinSCP-style file manager for SSH servers, using the same saved sessions as the Terminal page (only SSH sessions are listed; editing a session on either page changes it on both). Each tab is its own connection, logging in with the session's saved password, key or agent, and tabs pop out into windows like terminal tabs. This computer's files are on the left and the server's on the right: drag files between them or from Windows Explorer, or select them and press F5; F2 renames, F7 makes a folder, F8 or Delete deletes (local files go to the Recycle Bin), Alt+Enter shows properties, Ctrl+R refreshes and Ctrl+Alt+H shows or hides hidden files. Dragging onto a remote folder moves files there. The last folders used are remembered per server.
- SCP transfers go through a queue with progress, speed and time left, one file at a time on a channel of its own so browsing stays quick. Folders are copied with everything in them. Pause, Cancel and Retry work on the queue: SFTP transfers write to a .filepart file and resume where they stopped (after Pause, a dropped connection or a retry), then replace the target, keeping the replaced file's permissions and the original modification time. When a file exists NOMAD asks (Overwrite, Overwrite if Newer, Rename or Skip, optionally for the rest of the queue), or does what the queue's "If it exists" setting says. Verify compares each file's SHA-256 on both sides after copying (worked out on the server with sha256sum where it can, otherwise by reading the file back), and right-click > Checksum... shows a remote file's SHA-256, MD5 or SHA-1 to compare with an expected value or a local file.
- Editing remote files: F4 or double-click opens a file in NOMAD's editor (find and replace, go to line; the file's encoding and line endings are kept) and Ctrl+S saves it straight back to the server, warning first if it changed there since it was opened. Edit With opens it in another program (Windows' default, or one you choose, such as Notepad++) and uploads it each time it's saved there.
- Synchronize (right-click in the SCP panes, or the tab): compares a local folder with a remote one and lists the differences (only on one side, newer on one side, or different) for review. Choose upload, download or both ways (newer wins); nothing is copied until you press Synchronize, you can untick files or right-click them to choose which copy wins, and replacing a newer file asks first. Compare by checksum finds files edited without changing size.
- Properties and Permissions on remote files: size, modified time, owner and link target, and chmod with checkboxes or an octal value (including set UID, set GID and sticky), optionally for everything inside a folder (folders get Execute wherever Read is set).
- Servers without SFTP still work: NOMAD falls back to SCP, listing folders with ls over SSH (as WinSCP does) and copying files with the scp protocol. Transfers in that mode start again rather than resume.
- Import from SecureCRT (File > Import Sessions from SecureCRT, or right-click the session list): reads a SecureCRT XML export (Tools > Export Settings) and adds its SSH, Telnet, raw TCP and serial sessions under "Imported from SecureCRT", keeping SecureCRT's folders, ports, usernames, descriptions and key files. Saved passwords come across too: NOMAD asks for SecureCRT's configuration passphrase if one was set (Cancel imports the sessions without passwords) and re-encrypts them for your Windows account (and master password, if set). Importing the same file again only adds sessions that aren't there yet.
- Rearranging saved sessions: drag sessions and folders onto a folder to move them there (onto empty space for the top level; a folder takes everything in it). Ctrl-click and Shift-click select several to move, connect or delete at once. Right-click > Move to... picks the folder from a list (and can make a new one), and Move Up a Level lifts a folder out of the one it's in, such as the folders inside "Imported from SecureCRT". Moving a folder where one of the same name exists merges them, and a session whose name is already taken there gets " (2)" added; the folder things came out of stays until you delete it. Dragging is off while the session list is filtered (Move to... still works).
- Recent connections: the last 10 connections (saved sessions and quick connects) are listed under Recent at the top of the session list and in the Sessions ▾ menu. Double-click one to reconnect; right-click a quick connect to save it as a session (the Recent entry then opens the saved one, with its saved password), remove it, or clear the list.

## [1.5.0] - 2026-09-28

### Changed

- The IP Addresses page opens with its subnet list only as wide as its columns need, and a wider window gives the extra room to the addresses. After that the divider stays where it is (drag it to change it).
- Sweep Subnet on the IP Addresses page now sweeps right there instead of switching to the Sweep page: the Last Sweep column fills in as devices answer, with the host names and MAC addresses found shown beside addresses IPAM doesn't have, and Record Answering Devices, Update MACs and the right-click menu save the results to IPAM.

### Added

- IPAM history: History... on an address or subnet (right-click) and Network > Network History list every change with when, who and what changed (such as "Name: sw1 → sw1-core; Status: Used → Reserved"), newest first, filtered to the last day, week, month or all time; an address's history carries on across each time it was freed and recorded again. Network > View As Of shows the network as it was at any moment, read-only, with Back to Now. Laptops keep a copy of the IPAM server's history as they sync, so history works offline (the server needs this version).

## [1.4.0] - 2026-09-28

### Added

- Changing tribe networks offline: while the IPAM server can't be reached, addresses can still be assigned, edited and freed. Each change is kept as pending (shown on the IP Addresses page) and sent in the order made when the server is back, even after a restart; changes the server refuses because someone else got there first are listed under Review Refused Changes, to record at the next free address instead or discard. Subnets and networks still change online only.
- Sweep and ARP compare with IPAM: an IPAM column shows whether each device is in IPAM (with its name), missing, answering with a different MAC address, or answering on a reserved address. Record Not in IPAM records every missing device at once, Recorded but Silent (Sweep) lists recorded addresses that didn't answer, and right-clicking a device records it, updates its MAC or shows it on the IP Addresses page. Where a range is in more than one IPAM network, the one holding the most devices is chosen and another can be picked. Sweep Subnet on the IP Addresses page (or a subnet's right-click menu) sweeps that subnet compared with its IPAM network, listing recorded addresses that don't answer even when nothing does. The IP Addresses page's new Last Sweep column shows what each address did in the latest sweep of it this session, and the subnet's line sums it up.

## [1.3.0] - 2026-09-28

### Added

- IPAM server (for the Tribe: the people sharing your IPAM): the tribe's shared IPAM, run as the NOMAD IPAM Server Windows service on one machine (Tools > IPAM Server installs, updates, starts and removes it and opens port 8443 in the firewall), over HTTPS with a self-signed certificate that laptops pin. Laptops connect with a tribe key file and keep a copy of the tribe's networks for offline lookups, synced the moment anyone changes anything (each laptop keeps a request waiting at the server), with a sync every 5 minutes as a fallback and Sync Now; while online, their changes go straight to the server, which refuses conflicting ones (such as two people taking the same address) and says who got there first. Spreadsheets are imported, and tribe networks added or deleted, only on the server (NOMAD running as administrator there). Every change records the Windows user and computer; the server backs itself up nightly and keeps 14 days.
- The IP Addresses page shows Tribe and Local networks together, with the IPAM server's name and whether it's connected beside the Tribe button. On a laptop, importing a spreadsheet says plainly (on the button, in the import window and afterwards) that it stays on that computer and isn't shared with the tribe.

## [1.2.0] - 2026-09-27

### Added

- IP Addresses page (IPAM): networks kept separate (so air-gapped networks can reuse ranges), each with nested subnets and the addresses used or reserved in them, free addresses worked out rather than stored, Use Next Free, search across every network, and CSV export. Imports the team's addressing workbooks (.xlsx or .csv), asking which to keep wherever a page's summary and Detailed Info disagree, suggesting a gateway wherever the sheet's isn't in its subnet, importing an address written with a mask as the subnet holding it (172.28.101.0/16 is 172.28.0.0/16), and listing rows it can't use; the import window can go full screen; SNMP strings are never imported. Changes are logged with who made them, ready for syncing with a server later.
- Terminal page: an SSH, Telnet, serial and raw TCP client with saved sessions in folders, PuTTY import, tabs that pop out into their own windows, quick connect, select-to-copy and right-click paste, scrollback with find, session logging, and serial break. Saved passwords are encrypted for your Windows account, with an optional master password on top (AES-256, scrypt) that locks after a chosen idle time; host keys are remembered and changes flagged, and older switches that only support SHA-1 SSH algorithms still connect. Sweep and Ports open sessions here.
- SNMP page: walk or get any part of a device's MIB over SNMP v1/v2c, with presets (system, interfaces, IP and ARP tables, MAC address tables, LLDP neighbors, serial numbers) and an Interface Summary of each port's status, speed and error counters. Copy or export to CSV.
- DHCP Servers page: finds every DHCP server answering on the selected adapter's network and flags any besides the one this adapter's lease came from as a possible rogue, and shows every option each server's offer carries (NTP, WINS, static routes, TFTP and boot servers, vendor data and more). Only a request is sent; no address is taken.
- Packet Capture page: capture with Windows' built-in pktmon, filtered by address, port and protocol, and save a pcapng file for Wireshark (needs administrator rights).
- TFTP page: a TFTP server (with upload control and a live transfer list) and a TFTP client for downloading from and uploading to other servers.
- Syslog page: receive syslog over UDP (and optionally TCP), filter by severity or text, and save or log messages to a file.
- Network Reset page: flush DNS, clear ARP, renew all DHCP leases, reset Winsock and TCP/IP, view and turn off a leftover proxy (with Undo), and reset the WinHTTP proxy.
- Sweep's right-click menu can open a host on the SNMP and Packet Capture pages.

### Changed

- More room for terminal sessions: the session list is slimmer (one column, compact New and ⋯ menus) and can be hidden with « beside the tabs, with a Sessions menu in its place; View > Focus Mode (F11) hides the sidebar, adapter bar and status bar on any page; F11 makes a pop-out terminal window full screen.
- The tabs are now a sidebar of pages grouped into This Computer, Test, Discover, DNS & Web and Tools, so every page fits at any text size. Ctrl+Tab and Ctrl+Shift+Tab move between pages. Section headings are shaded bands so the groups stand out, and the sidebar can be hidden (the « button, View > Show Sidebar or Ctrl+B) to give pages more room, leaving a slim strip with a menu of every page.
- The Services tab is now two pages, DNS Servers and Web Check.
- The release steps (README and `python -m nomad.version bump`) now add new files before committing.

### Fixed

- Hiding the Terminal session list and then restarting NOMAD left no way to bring the list back. The list now always shows while no sessions are open.
- An SSH connection whose device hung up just before login could stay on "Connecting..." forever; it now fails straight away with an explanation.
- SSH logins to Cisco IOS XE (and other devices that allow only one "ssh-userauth" service request per connection) were reported as a wrong password even when it was right: the device disconnected before checking it. NOMAD now asks for the service once, as OpenSSH does, and says so plainly if a device does drop the connection during login.

## [1.0.1] - 2026-09-24

### Added

- Ports tab: check whether TCP ports on a host are open, closed or filtered, with presets from common ports up to all 65535. Open web, Remote Desktop and SSH ports straight from the results, or check a web port on the Services tab. Scan Ports is also on the Sweep and ARP tabs.
- ARP tab: the ARP and IPv6 neighbor tables with MAC vendors, warnings about possible ARP spoofing and duplicate IP addresses, and delete entry / clear cache.
- Connections tab: TCP connections and listening TCP/UDP ports with the program that owns each (like netstat -ano), with filtering and optional auto refresh.
- Switch Port tab: find the switch, port and VLAN an adapter is plugged into from LLDP/CDP announcements, using Windows' built-in pktmon (needs administrator rights).
- Services tab: compare DNS servers' response times (add your own, or remove any from the list) and check forward/reverse DNS; check a web server's timings, certificate and reply, following redirects.
- Utilities tab: subnet calculator with subnet splitting, and Wake-on-LAN with saved devices (also on the Sweep and ARP tabs' right-click menus).
- Diagnostics report (Tools > Run Diagnostics Report, Ctrl+R): checks the adapter, gateway, DNS, internet access, route, path MTU and ARP table and saves the findings as a web page.
- View > Text Size makes all text larger or smaller (90% to 200%), with Ctrl+= / Ctrl+- / Ctrl+0 shortcuts. The choice is remembered.
- Help > About is a proper About window with build and runtime details that can be copied.

### Changed

- Traceroute shows loss, latency and jitter for each hop like MTR, probes every hop at once, can keep running until stopped, and copies a text report.
- Sweep shows each host's MAC address, vendor and host name (DNS, or NetBIOS on networks without a DNS server), finds hosts that block ping by using ARP on local subnets, and includes these in the CSV export.
- The MAC vendor list is built in, so vendor lookups work offline. Everything in NOMAD works without internet access.
- The version shows in the title bar and the exe's file name (NOMAD-X.Y.Z.exe).

### Fixed

- After a change that switches an adapter to DHCP, NOMAD keeps checking for up to a minute until the new address arrives, instead of showing the adapter with no address.
- Tracing a host from the Sweep tab while a traceroute is running stops it and starts the new one, instead of refusing.

## [1.0.0] - 2026-09-24

- First versioned release: NIC Manager renamed to NOMAD, with Interfaces, Routing Table, MTU, Ping, Latency, Traceroute, iperf, DNS Lookup and Sweep tabs.

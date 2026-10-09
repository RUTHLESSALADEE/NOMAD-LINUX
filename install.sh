#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_SOURCE="$SCRIPT_DIR/dist/NOMAD-1.24.2-linux-x86_64"

if [ ! -f "$BIN_SOURCE" ]; then
    # Check current directory
    if [ -f "$SCRIPT_DIR/NOMAD-1.24.2-linux-x86_64" ]; then
        BIN_SOURCE="$SCRIPT_DIR/NOMAD-1.24.2-linux-x86_64"
    else
        echo "Building standalone binary with PyInstaller..."
        python3 -m PyInstaller --noconfirm --clean --onefile --name NOMAD-1.24.2-linux-x86_64 --add-data "nomad/data:nomad/data" main.py
        BIN_SOURCE="$SCRIPT_DIR/dist/NOMAD-1.24.2-linux-x86_64"
    fi
fi

echo "Installing NOMAD to /usr/local/bin..."
sudo install -m 755 "$BIN_SOURCE" /usr/local/bin/nomad

echo "Installing icon..."
sudo mkdir -p /usr/local/share/icons/hicolor/256x256/apps/
if [ -f "$SCRIPT_DIR/nomad.png" ]; then
    sudo install -m 644 "$SCRIPT_DIR/nomad.png" /usr/local/share/icons/hicolor/256x256/apps/nomad.png
fi

echo "Installing Desktop Application entry..."
sudo mkdir -p /usr/local/share/applications/
cat << 'DESKTOPEOF' | sudo tee /usr/local/share/applications/nomad.desktop > /dev/null
[Desktop Entry]
Name=NOMAD
Comment=Network Operations, Monitoring And Diagnostics
Exec=nomad %F
Icon=nomad
Terminal=false
Type=Application
Categories=Network;System;Utility;
Keywords=network;diagnostics;ping;traceroute;snmp;ipam;iperf;
StartupNotify=true
DESKTOPEOF

# Create polkit policy for network privileges prompt
sudo mkdir -p /usr/share/polkit-1/actions/
cat << 'POLKITEOF' | sudo tee /usr/share/polkit-1/actions/com.nomad.network.policy > /dev/null
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC
 "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <action id="com.nomad.network.pkexec">
    <description>Run NOMAD with elevated network privileges</description>
    <message>Authentication is required to run NOMAD with administrative privileges</message>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>auth_admin</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/local/bin/nomad</annotate>
    <annotate key="org.freedesktop.policykit.exec.allow_gui">true</annotate>
  </action>
</policyconfig>
POLKITEOF

# Copy desktop shortcut to user's Desktop folder if present
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESKTOP_DIR" ]; then
    cp /usr/local/share/applications/nomad.desktop "$DESKTOP_DIR/"
    chmod +x "$DESKTOP_DIR/nomad.desktop"
    echo "Placed shortcut on your Desktop: $DESKTOP_DIR/nomad.desktop"
fi

which update-desktop-database >/dev/null 2>&1 && sudo update-desktop-database 2>/dev/null || true
which gtk-update-icon-cache >/dev/null 2>&1 && sudo gtk-update-icon-cache -f /usr/local/share/icons/hicolor 2>/dev/null || true

echo ""
echo "=== NOMAD Installation Complete! ==="
echo "Launch NOMAD from your Application Menu, Desktop shortcut, or type 'nomad'."

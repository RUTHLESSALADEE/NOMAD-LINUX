#!/usr/bin/env bash
# ==============================================================================
# NOMAD-LINUX: Automated 0-to-Hero Installer for Ubuntu
# Repository: https://github.com/RUTHLESSALADEE/NOMAD-LINUX.git
# ==============================================================================
set -euo pipefail

echo ""
echo "=========================================================="
echo "   NOMAD Linux: Automated 0-to-Hero Ubuntu Installer      "
echo "=========================================================="
echo ""

# Ensure user has sudo privileges
if [ "$EUID" -eq 0 ]; then
    echo "[-] Please run this script as your regular user (NOT as root or with sudo)."
    echo "    The script will invoke sudo when needed."
    exit 1
fi

sudo -v || { echo "[-] Sudo access required. Exiting."; exit 1; }

# Keep sudo timestamp updated during installation
while true; do sudo -n true; sleep 50; kill -0 "$$" || exit; done 2>/dev/null &
SUDO_KEEPALIVE_PID=$!
trap 'kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true' EXIT

# ------------------------------------------------------------------------------
# 1. Install System Dependencies via APT
# ------------------------------------------------------------------------------
echo "[1/4] Updating APT and installing system dependencies..."
sudo apt update -y
sudo apt install -y \
    curl \
    policykit-1 \
    pkexec \
    x11-xserver-utils \
    xwayland \
    xdg-utils \
    iproute2 \
    iputils-ping \
    net-tools \
    python3-pyqt5.qtsvg \
    libqt5svg5

# ------------------------------------------------------------------------------
# 2. Download Pre-Compiled Standalone Binary & Assets
# ------------------------------------------------------------------------------
INSTALL_DIR="$HOME/NOMAD-LINUX"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

RELEASE_TAG="v1.27.1-linux"
ARCH="$(uname -m)"
BIN_NAME="NOMAD-1.27.1-linux-${ARCH}"
BASE_URL="https://github.com/RUTHLESSALADEE/NOMAD-LINUX/releases/download/${RELEASE_TAG}"

echo "[2/4] Downloading NOMAD 1.27.1 binary and assets from GitHub..."
curl -fSL "${BASE_URL}/${BIN_NAME}" -o "${INSTALL_DIR}/${BIN_NAME}"
curl -fSL "${BASE_URL}/nomad.png" -o "${INSTALL_DIR}/nomad.png" || true
chmod +x "${INSTALL_DIR}/${BIN_NAME}"

# ------------------------------------------------------------------------------
# 3. Install NOMAD System-Wide & Configure Elevation
# ------------------------------------------------------------------------------
echo "[3/4] Installing binary and configuring system elevation..."

# Install binary to /usr/local/bin
sudo install -m 755 "${INSTALL_DIR}/${BIN_NAME}" /usr/local/bin/nomad

# Elevation wrapper (sets xhost root permission and QT_QPA_PLATFORM=xcb)
cat << 'WRAPPEREOF' | sudo tee /usr/local/bin/nomad-pkexec > /dev/null
#!/usr/bin/env bash
TARGET_UID="${PKEXEC_UID:-$SUDO_UID}"
if [ -z "$TARGET_UID" ]; then
    TARGET_UID="$(id -u)"
fi
TARGET_USER="$(id -un "$TARGET_UID")"

# Grant root access to user X11 / XWayland display
if command -v xhost >/dev/null 2>&1; then
    su - "$TARGET_USER" -c "xhost +si:localuser:root" 2>/dev/null || true
fi

export DISPLAY="${DISPLAY:-:0}"
XAUTH_FILE="/home/$TARGET_USER/.Xauthority"
if [ -f "$XAUTH_FILE" ]; then
    export XAUTHORITY="$XAUTH_FILE"
fi
export QT_QPA_PLATFORM="xcb"

exec /usr/local/bin/nomad "$@"
WRAPPEREOF
sudo chmod 755 /usr/local/bin/nomad-pkexec

# Install Desktop Icon
sudo mkdir -p /usr/local/share/icons/hicolor/256x256/apps/
if [ -f "$INSTALL_DIR/nomad.png" ]; then
    sudo install -m 644 "$INSTALL_DIR/nomad.png" /usr/local/share/icons/hicolor/256x256/apps/nomad.png
fi

# Install Desktop Entry
sudo mkdir -p /usr/local/share/applications/
cat << 'DESKTOPEOF' | sudo tee /usr/local/share/applications/nomad.desktop > /dev/null
[Desktop Entry]
Name=NOMAD
Comment=Network Operations, Monitoring And Diagnostics (Administrator)
Exec=pkexec /usr/local/bin/nomad-pkexec %F
Icon=nomad
Terminal=false
Type=Application
Categories=Network;System;Utility;
Keywords=network;diagnostics;ping;traceroute;snmp;ipam;iperf;
StartupNotify=true
DESKTOPEOF

# Configure Polkit Action Policy
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
      <allow_any>yes</allow_any>
      <allow_inactive>yes</allow_inactive>
      <allow_active>yes</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/local/bin/nomad-pkexec</annotate>
    <annotate key="org.freedesktop.policykit.exec.allow_gui">true</annotate>
  </action>
</policyconfig>
POLKITEOF

# Configure Polkit Sudo Rules
sudo mkdir -p /etc/polkit-1/rules.d/
cat << 'RULESEOF' | sudo tee /etc/polkit-1/rules.d/50-nomad.rules > /dev/null
polkit.addRule(function(action, subject) {
    if (action.id == "com.nomad.network.pkexec" && (subject.isInGroup("sudo") || subject.isInGroup("wheel"))) {
        return polkit.Result.YES;
    }
});
RULESEOF
sudo chmod 644 /etc/polkit-1/rules.d/50-nomad.rules

# ------------------------------------------------------------------------------
# 4. Refresh System Caches
# ------------------------------------------------------------------------------
echo "[4/4] Updating desktop and icon caches..."
which update-desktop-database >/dev/null 2>&1 && sudo update-desktop-database 2>/dev/null || true
which gtk-update-icon-cache >/dev/null 2>&1 && sudo gtk-update-icon-cache -f /usr/local/share/icons/hicolor 2>/dev/null || true

echo ""
echo "=========================================================="
echo "   NOMAD v1.27.1 Installation Complete!                   "
echo "=========================================================="
echo ""
echo "How to run NOMAD:"
echo "  1. Terminal:            Type 'nomad'"
echo "  2. Application Menu:    Search for 'NOMAD' and click the icon"
echo "  3. Standard Mode:       Type 'nomad --no-elevate' (without root)"
echo ""

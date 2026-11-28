#!/bin/sh
# Installeert raspISO als systemd-service op de Raspberry Pi.
#   sudo deploy/install.sh [can0] [8080]
# Start daarna automatisch bij het opstarten en haalt dan de laatste versie van GitHub (tractorsim-update.timer);
# webinterface op http://<pi>:8080
set -e
CAN=${1:-can0}
PORT=${2:-8080}
DIR=$(cd "$(dirname "$0")/.." && pwd)
USER_NAME=${SUDO_USER:-$(id -un)}

if [ "$(id -u)" -ne 0 ]; then
  echo "Start met sudo: sudo $0 $*" >&2
  exit 1
fi

python3 -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9 of nieuwer nodig"'

for unit in tractorsim.service tractorsim-update.service tractorsim-update.timer; do
  sed -e "s|@USER@|$USER_NAME|g" -e "s|@DIR@|$DIR|g" -e "s|@CAN@|$CAN|g" -e "s|@PORT@|$PORT|g" \
    "$DIR/deploy/$unit" > "/etc/systemd/system/$unit"
done
chmod +x "$DIR/deploy/update.sh"
systemctl daemon-reload
systemctl enable tractorsim.service tractorsim-update.timer
systemctl restart tractorsim.service
systemctl start tractorsim-update.timer

echo "Service geïnstalleerd: gebruiker $USER_NAME, map $DIR, interface $CAN, poort $PORT"
echo "Status:  systemctl status tractorsim"
echo "Logboek: journalctl -u tractorsim -f"
echo "Na elke start haalt tractorsim-update de laatste versie van GitHub (journalctl -u tractorsim-update);"
echo "nu bijwerken: sudo systemctl start tractorsim-update; uitzetten: sudo systemctl disable --now tractorsim-update.timer"
if ! ip link show "$CAN" >/dev/null 2>&1; then
  echo
  echo "LET OP: interface $CAN bestaat niet. Zet de overlay van je CAN-HAT in /boot/firmware/config.txt"
  echo "(zie README, 'CAN-HAT aansluiten') en herstart de Pi."
fi

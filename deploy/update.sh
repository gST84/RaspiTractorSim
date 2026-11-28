#!/bin/sh
# Haalt de laatste versie van GitHub op. Gestart door tractorsim-update.timer kort na het opstarten (of met de hand:
# sudo systemctl start tractorsim-update). Na een nieuwe versie herstart tractorsim-update.service de simulator.
#
# - Wacht op het netwerk: tot 10 pogingen met 30 s ertussen. Zonder netwerk (bv. op de tractor) blijft de huidige versie
#   gewoon draaien.
# - Enkel vooruitspoelen (--ff-only): met lokale wijzigingen of een afgeweken geschiedenis verandert er niets.
cd "$(dirname "$0")/.." || exit 0
export GIT_TERMINAL_PROMPT=0
MARK=${TRACTORSIM_UPDATED:-/tmp/tractorsim-updated}
old=$(git rev-parse HEAD) || exit 0

T=""
command -v timeout >/dev/null 2>&1 && T="timeout 60"
n=0
until $T git fetch -q; do
  n=$((n + 1))
  if [ "$n" -ge 10 ]; then
    echo "geen update: GitHub niet bereikbaar"
    exit 0
  fi
  sleep 30
done

if ! git merge --ff-only -q '@{u}' 2>/dev/null; then
  echo "geen update: lokale wijzigingen of een andere geschiedenis dan GitHub (git status)"
  exit 0
fi

new=$(git rev-parse HEAD)
if [ "$old" = "$new" ]; then
  echo "al de laatste versie: $(git log -1 --format='%h %s')"
else
  echo "bijgewerkt naar $(git log -1 --format='%h %s')"
  if git diff --name-only "$old" "$new" -- deploy/ | grep -q .; then
    echo "LET OP: deploy/ is gewijzigd, voer sudo deploy/install.sh opnieuw uit"
  fi
  touch "$MARK"
fi

#!/usr/bin/env bash
# Compile et exécute chaque exemple, et compare sa sortie à exemples/*.attendu
cd "$(dirname "$0")"
ok=0 total=0
for f in exemples/*.lut; do
  total=$((total + 1))
  if diff <(python3 lutece.py "$f" 2>&1) "${f%.lut}.attendu" > /dev/null; then
    ok=$((ok + 1)); echo "  ok    $f"
  else
    echo "  ÉCHEC $f"
  fi
done
echo "$ok / $total"
[ "$ok" = "$total" ]

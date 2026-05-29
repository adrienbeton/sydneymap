#!/bin/bash
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$ROOT/data"

echo "=== Sydney Isochrone — Bootstrap ==="

# Load .env
if [ -f "$ROOT/.env" ]; then
  source "$ROOT/.env"
fi

# API key check
if [ -z "$TFNSW_API_KEY" ]; then
  echo ""
  echo "Clé API Transport for NSW requise."
  echo "→ https://opendata.transport.nsw.gov.au → Create App → copie la clé"
  echo ""
  read -p "Colle ta clé API : " TFNSW_API_KEY
  echo "TFNSW_API_KEY=$TFNSW_API_KEY" > "$ROOT/.env"
  echo "Sauvegardée dans .env"
fi

# Download Sydney OSM (BBBike extract, ~50 MB)
if [ ! -f "$DATA/sydney.osm.pbf" ]; then
  echo ""
  echo "=== OSM Sydney (~50 MB) ==="
  curl -L "https://download.bbbike.org/osm/bbbike/Sydney/Sydney.osm.pbf" \
    -o "$DATA/sydney.osm.pbf" --progress-bar
else
  echo "✓ OSM déjà présent"
fi

# Download Complete GTFS TfNSW
if [ ! -f "$DATA/gtfs-complete.zip" ]; then
  echo ""
  echo "=== GTFS Complete TfNSW (~200 MB) ==="
  TMPSTATUS=$(mktemp)
  curl -L \
    "https://api.transport.nsw.gov.au/v1/publictransport/timetables/complete/gtfs" \
    -H "Authorization: apikey $TFNSW_API_KEY" \
    -o "$DATA/gtfs-complete.zip" \
    --progress-bar \
    -w "%{http_code}" > "$TMPSTATUS"
  HTTP_STATUS=$(cat "$TMPSTATUS"); rm -f "$TMPSTATUS"

  if [ "$HTTP_STATUS" != "200" ]; then
    rm -f "$DATA/gtfs-complete.zip"
    echo "Erreur HTTP $HTTP_STATUS — vérifie ta clé API"
    exit 1
  fi
else
  echo "✓ GTFS déjà présent"
fi

# Filter GTFS to Sydney metro
if [ ! -f "$DATA/gtfs-sydney.zip" ]; then
  echo ""
  echo "=== Filtrage GTFS → Sydney metro ==="
  python3 "$ROOT/scripts/filter-gtfs.py"
else
  echo "✓ GTFS filtré déjà présent"
fi

# Remove the unfiltered zip so only the Sydney one remains
rm -f "$DATA/gtfs-complete.zip"

# Start services
echo ""
echo "=== Démarrage R5 + app ==="
echo "Premier lancement : build de l'image R5 (~quelques min) puis"
echo "construction du réseau transit en mémoire au démarrage (~1-2 min)."
docker compose -f "$ROOT/docker-compose.yml" up -d --build

echo ""
echo "=== En attente de R5 ==="
until curl -sf http://localhost:3000/api/health 2>/dev/null | grep -q '"UP"' 2>/dev/null; do
  printf "."
  sleep 5
done
echo ""
echo "✓ R5 prêt"

echo ""
echo "Ouverture http://localhost:3000"
open http://localhost:3000

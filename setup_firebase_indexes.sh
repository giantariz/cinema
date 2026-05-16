#!/usr/bin/env bash
# =============================================================
# setup_firebase_indexes.sh
# Αυτόματη εγκατάσταση Firestore indexes για Athinorama Archive
# =============================================================
set -e

BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}  Athinorama — Firebase Indexes Setup${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# 1. Έλεγχος Node.js
echo -e "${YELLOW}[1/4] Έλεγχος Node.js...${NC}"
if ! command -v node &> /dev/null; then
  echo -e "${RED}✗ Node.js δεν βρέθηκε.${NC}"
  echo "   Κατέβασέ το από https://nodejs.org (LTS έκδοση) και τρέξε ξανά το script."
  exit 1
fi
NODE_VER=$(node --version)
echo -e "${GREEN}✓ Node.js ${NODE_VER}${NC}"

# 2. Εγκατάσταση Firebase CLI αν δεν υπάρχει
echo ""
echo -e "${YELLOW}[2/4] Έλεγχος Firebase CLI...${NC}"
if ! command -v firebase &> /dev/null; then
  echo "   Εγκατάσταση firebase-tools..."
  npm install -g firebase-tools
  echo -e "${GREEN}✓ Firebase CLI εγκαταστάθηκε${NC}"
else
  FB_VER=$(firebase --version)
  echo -e "${GREEN}✓ Firebase CLI ${FB_VER}${NC}"
fi

# 3. Login
echo ""
echo -e "${YELLOW}[3/4] Σύνδεση με Google λογαριασμό...${NC}"
echo "   Θα ανοίξει browser για login. Συνδέσου με τον λογαριασμό"
echo "   που χρησιμοποιείς για το Firebase project."
echo ""
firebase login --no-localhost 2>/dev/null || firebase login

# 4. Επιλογή project και deploy indexes
echo ""
echo -e "${YELLOW}[4/4] Deploy Firestore indexes...${NC}"
echo ""
echo "   Τα διαθέσιμα Firebase projects σου:"
echo ""
firebase projects:list

echo ""
echo -e "Πληκτρολόγησε το ${BLUE}Project ID${NC} του Athinorama project σου"
echo -e "(π.χ. ${BLUE}athinorama-archive${NC}) και πάτα Enter:"
read -r PROJECT_ID

if [ -z "$PROJECT_ID" ]; then
  echo -e "${RED}✗ Δεν δόθηκε Project ID. Ακύρωση.${NC}"
  exit 1
fi

# Χρησιμοποίηση του project
firebase use "$PROJECT_ID"

# Deploy μόνο τα indexes (χωρίς να χρειαστεί firebase.json)
echo ""
echo "   Ανέβασμα indexes στο Firestore..."
firebase deploy --only firestore:indexes --project "$PROJECT_ID"

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  ✅ Τα indexes ανέβηκαν επιτυχώς!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "   Τα indexes χτίζονται στο background — μπορεί να πάρει"
echo "   1–5 λεπτά μέχρι να είναι έτοιμα (ανάλογα με τα δεδομένα)."
echo ""
echo "   Μπορείς να δεις την πρόοδο στο Firebase Console:"
echo "   https://console.firebase.google.com/project/${PROJECT_ID}/firestore/indexes"
echo ""

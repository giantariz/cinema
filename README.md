# 🎬 Αθηνόραμα Αρχείο Ταινιών

Searchable movie database populated by scraping the [Athinorama](https://www.athinorama.gr) movie archive. Features a Python/Flask API, Vanilla JS frontend, and Firebase Firestore database — backend on Google Cloud Run, frontend on Netlify.

---

## Features

- **Αρχείο ταινιών** — φίλτρα (έτος, βαθμολογία IMDb, χώρα, είδος, διάρκεια), ταξινόμηση, pagination
- **Τυχαία ταινία** — "Τυχαία" κουμπί για discovery
- **Η Λίστα μου** — personal watchlist με 4 κατηγορίες (ταινίες/σειρές, είδα/αγαπημένα)
- **Google Sheets Sync** — Apps Script για sync από spreadsheet → Firestore
- **Scraping panel** — full/incremental scraping με progress tracking
- **Dark cinematic UI** — responsive, χωρίς εξωτερικά frameworks

---

## Repo Structure

```
athinorama-archive/
├── backend/
│   ├── app.py               # Flask API server
│   ├── scraper.py           # Athinorama scraper
│   ├── firebase_client.py   # Firestore wrapper
│   ├── sheets_sync.py       # Google Sheets sync logic
│   ├── requirements.txt
│   └── Procfile
├── frontend/
│   └── index.html           # Όλο το UI σε ένα αρχείο
├── apps_script/
│   └── sync_to_backend.gs   # Google Apps Script
├── .github/
│   └── workflows/
│       └── weekly_scrape.yml
├── .env.example
├── firestore.indexes.json
├── netlify.toml
└── railway.json
```

---

## Setup

### 1. Firebase Project

1. Πήγαινε στο [Firebase Console](https://console.firebase.google.com/)
2. **Create project** → δώσε όνομα (π.χ. `athinorama-archive`)
3. **Firestore Database** → Create database → Start in **production mode** → Choose region
4. **Project Settings** → Service Accounts → **Generate new private key** → κατέβασε το JSON
5. **Firestore indexes**: Ανέβασε τα indexes:
   ```bash
   firebase deploy --only firestore:indexes
   ```
   (χρειάζεται το Firebase CLI: `npm install -g firebase-tools`)

### 2. Backend — Google Cloud Run

1. Πήγαινε στο [Google Cloud Console](https://console.cloud.google.com/) → Cloud Run → **Create service**
2. Επέλεξε **"Continuously deploy from a repository"** → σύνδεσε το GitHub repo
3. Source directory: `backend`, Branch: `main`
4. **Environment Variables** (Edit & Deploy New Revision → Variables & Secrets):

| Variable | Τιμή |
|---|---|
| `FIREBASE_SERVICE_ACCOUNT_JSON` | Το περιεχόμενο του service account JSON (ως string) |
| `FLASK_SECRET_KEY` | Τυχαίο string (π.χ. `openssl rand -hex 32`) |
| `ALLOWED_ORIGINS` | `https://your-app.netlify.app,http://localhost:3000` |
| `SCRAPE_API_KEY` | Τυχαίο string για προστασία scrape endpoint |
| `SYNC_API_KEY` | Τυχαίο string για Google Sheets sync |

5. Το `backend/Dockerfile` χρησιμοποιείται αυτόματα για το build.

### 3. Frontend — Netlify

1. Πήγαινε στο [netlify.com](https://netlify.com) → Add new site → Import from Git
2. Επέλεξε αυτό το repo
3. Build settings:
   - **Publish directory**: `frontend`
   - (Χωρίς build command — static HTML)
4. **Άλλαξε το BACKEND_URL** στο `frontend/index.html`:
   ```javascript
   const BACKEND_URL = 'https://your-service-region.run.app';
   ```
   Αντικατάστησε με το Cloud Run URL σου.
5. Ενημέρωσε και το `ALLOWED_ORIGINS` στο Cloud Run με το Netlify URL σου.

### 4. GitHub Secrets (για weekly scraping)

Settings → Secrets → Actions:

| Secret | Τιμή |
|---|---|
| `BACKEND_URL` | URL του Cloud Run service |
| `SCRAPE_API_KEY` | Το ίδιο key που έβαλες στο Cloud Run |

---

## Local Development

### Backend

```bash
cd backend

# Δημιούργησε virtual environment
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Εγκατάσταση dependencies
pip install -r requirements.txt

# Αντέγραψε και συμπλήρωσε .env
cp ../.env.example .env
# Επεξεργάσου το .env με τα Firebase credentials σου

# Εκκίνηση server
python app.py
# → http://localhost:5000
```

### Frontend

```bash
# Επιλογή 1: npx serve
npx serve frontend
# → http://localhost:3000

# Επιλογή 2: Python
python3 -m http.server 3000 --directory frontend
# → http://localhost:3000
```

Στο `frontend/index.html`, άλλαξε:
```javascript
const BACKEND_URL = 'http://localhost:5000';
```

---

## Google Apps Script — Εγκατάσταση

Το Apps Script διαβάζει από το Google Spreadsheet σου και στέλνει δεδομένα στο backend.

### Βήματα

1. Άνοιξε το Google Spreadsheet με τις ταινίες σου
2. Μενού: **Extensions → Apps Script**
3. Διέγραψε τον υπάρχοντα κώδικα και επικόλλησε το περιεχόμενο του `apps_script/sync_to_backend.gs`
4. Ρύθμισε τις μεταβλητές στην αρχή του αρχείου:
   ```javascript
   const BACKEND_URL = 'https://your-service-region.run.app';
   const SYNC_API_KEY = 'your-sync-api-key';
   ```
5. **Αποθήκευσε** (Ctrl+S)
6. Τρέξε τη συνάρτηση `onOpen` μία φορά για να δεις το menu (ή ανανέωσε το spreadsheet)
7. Στο Spreadsheet: θα εμφανιστεί το menu **🎬 Athinorama → Sync τώρα**

### Δομή Spreadsheet

Κάθε sheet πρέπει να έχει τα εξής headers (πρώτη γραμμή):

```
title | year | genre | imdb_score | tmdb_score | rotten_tomatoes | cast | director | description | duration | trailer_link | poster | similar
```

Ονόματα sheets που αναγνωρίζεται αυτόματα:
- `Movies I have seen` → list_type: `seen`
- `Favourite movies` → list_type: `favourite`
- `Series I have seen` → list_type: `series_seen`
- `Favourite Series` → list_type: `series_favourite`

### Αυτόματο εβδομαδιαίο sync

Από το menu: **🎬 Athinorama → Ρύθμιση αυτόματου sync** → τρέχει κάθε Κυριακή 6:00 π.μ.

---

## API Documentation

### Base URL

```
https://your-service-region.run.app
```

### Endpoints

#### `GET /health`
Health check.
```json
{ "status": "ok" }
```

#### `GET /api/movies`
Αναζήτηση ταινιών.

**Query params:**
| Param | Τύπος | Περιγραφή |
|---|---|---|
| `q` | string | Fulltext αναζήτηση τίτλου |
| `year_from` | int | Έτος από |
| `year_to` | int | Έτος έως |
| `imdb_min` | float | Βαθμολογία IMDb από (1–10) |
| `imdb_max` | float | Βαθμολογία IMDb έως |
| `country` | string | Χώρα παραγωγής |
| `genre` | string | Είδος ταινίας |
| `duration_min` | int | Διάρκεια από (λεπτά) |
| `duration_max` | int | Διάρκεια έως |
| `sort_by` | string | `year` \| `stars` \| `title` \| `duration` |
| `sort_dir` | string | `asc` \| `desc` |
| `page` | int | Σελίδα (default: 1) |
| `per_page` | int | 24 \| 48 \| 96 |

**Response:**
```json
{
  "movies": [...],
  "total": 1234,
  "page": 1,
  "per_page": 24,
  "pages": 52
}
```

#### `GET /api/movies/random`
Τυχαία ταινία.

#### `GET /api/movies/<id>`
Λεπτομέρειες ταινίας.

#### `GET /api/filters/meta`
Διαθέσιμες χώρες και είδη.
```json
{ "countries": ["ΗΠΑ", "Γαλλία", ...], "genres": ["Δράμα", "Θρίλερ", ...] }
```

#### `POST /api/scrape/start`
Έναρξη scraping. Requires `SCRAPE_API_KEY`.
```json
{
  "api_key": "your-key",
  "mode": "incremental",
  "full_rescrape": false
}
```

#### `GET /api/scrape/status`
Status scrape job.
```
?scrape_id=<uuid>  (προαιρετικό — αν παραληφθεί, επιστρέφει τελευταίο job)
```

#### `GET /api/user/movies`
Personal watchlist. `?list_type=seen|favourite|series_seen|series_favourite`

#### `POST /api/user/movies`
Προσθήκη στη λίστα.

#### `PUT /api/user/movies/<id>`
Ενημέρωση εγγραφής.

#### `DELETE /api/user/movies/<id>`
Διαγραφή εγγραφής.

#### `POST /api/user/sync`
Sync από Google Sheets. Requires `Authorization: Bearer <SYNC_API_KEY>`.
```json
{
  "sheet": "Movies I have seen",
  "rows": [{ "title": "Fight Club", "year": "1999", ... }]
}
```

---

## Legal Notice

Αυτό το project κάνει scraping δημόσια διαθέσιμου περιεχομένου από το athinorama.gr για προσωπική/εκπαιδευτική χρήση. Σεβόμαστε τους κανόνες:

- Rate limiting: 1–2 δευτερόλεπτα delay μεταξύ requests
- Incremental mode: αποφυγή άσκοπης επιβάρυνσης του server
- Χρησιμοποίησε υπεύθυνα — όχι για εμπορική εκμετάλλευση

Για εμπορική χρήση, επικοινώνησε με το athinorama.gr για API πρόσβαση.

---

## What's Left To Do Manually

| Βήμα | Περιγραφή |
|---|---|
| Firebase project | Δημιουργία project + Firestore + service account key |
| Cloud Run deploy | Σύνδεση repo + environment variables στο Cloud Run |
| Netlify deploy | Σύνδεση repo + ενημέρωση BACKEND_URL στο index.html |
| GitHub Secrets | `BACKEND_URL` + `SCRAPE_API_KEY` για weekly scrape |
| Apps Script | Copy-paste στο Google Spreadsheet + ρύθμιση BACKEND_URL (Cloud Run URL)/SYNC_API_KEY |
| Firestore indexes | `firebase deploy --only firestore:indexes` |
| Πρώτο scraping | Από το admin panel (⚙) → mode: full → Έναρξη |

---

## Changelog

### v1.1 — Modal & Filter Updates
- **Pop-up modal**: εμφανίζει βαθμολογία IMDb, Έτος, Χώρα, Είδος, Σκηνοθεσία, Διάρκεια, Ηθοποιοί, Περιγραφή
- **Αθηνόραμα βαθμολογία** αφαιρέθηκε από το pop-up και το φίλτρο
- **Φίλτρο βαθμολογίας**: μετονομάστηκε σε "Βαθμολογία IMDb" (κλίμακα 1–10), φιλτράρει βάσει `imdb_score`
- **Κουμπί IMDb**: εμφανίζεται με κίτρινο χρώμα (#f5c518) στο modal footer

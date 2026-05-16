/**
 * Google Apps Script — Athinorama Αρχείο Ταινιών
 * Sync από Google Sheets → Flask backend → Firestore
 *
 * ΟΔΗΓΙΕΣ:
 * 1. Άνοιξε το Google Spreadsheet σου
 * 2. Extensions → Apps Script
 * 3. Επικόλλησε αυτόν τον κώδικα
 * 4. Ρύθμισε τις μεταβλητές παρακάτω
 * 5. Αποθήκευσε και τρέξε τη συνάρτηση onOpen() για να δεις το menu
 */

// ============================================================
// ΡΥΘΜΙΣΕΙΣ — αλλάξε αυτές τις τιμές
// ============================================================
const BACKEND_URL = 'https://your-app.railway.app';  // URL του Railway backend
const SYNC_API_KEY = 'your-sync-api-key';             // SYNC_API_KEY από το .env
const SHEETS_TO_SYNC = [
  'Movies I have seen',
  'Favourite movies',
  'Series I have seen',
  'Favourite Series',
];
// ============================================================


/**
 * Δημιουργεί custom menu στο Spreadsheet κατά το άνοιγμα.
 */
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('🎬 Athinorama')
    .addItem('Sync τώρα', 'syncNow')
    .addItem('Sync αυτό το sheet', 'syncCurrentSheet')
    .addSeparator()
    .addItem('Ρύθμιση αυτόματου sync (εβδομαδιαίο)', 'setupWeeklyTrigger')
    .addItem('Διαγραφή αυτόματου sync', 'deleteWeeklyTrigger')
    .addToUi();
}


/**
 * Βασική συνάρτηση sync — αποστέλλει όλα τα sheets στο backend.
 */
function syncNow() {
  const ui = SpreadsheetApp.getUi();
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let totalSynced = 0;
  const allErrors = [];

  SHEETS_TO_SYNC.forEach(function(sheetName) {
    const sheet = ss.getSheetByName(sheetName);
    if (!sheet) {
      Logger.log('Sheet δεν βρέθηκε: ' + sheetName);
      return;
    }

    const rows = _readSheet(sheet);
    if (rows.length === 0) {
      Logger.log('Κενό sheet: ' + sheetName);
      return;
    }

    Logger.log('Sync ' + rows.length + ' εγγραφές από "' + sheetName + '"...');
    const result = _sendToBackend(sheetName, rows);
    totalSynced += (result.synced || 0);
    if (result.errors && result.errors.length > 0) {
      allErrors.push(...result.errors);
    }
  });

  // Εμφάνιση αποτελέσματος
  const msg = '✅ Sync ολοκληρώθηκε!\n\n' +
    'Εγγραφές: ' + totalSynced + '\n' +
    (allErrors.length > 0 ? '⚠ Σφάλματα: ' + allErrors.length + '\n' + allErrors.slice(0, 5).join('\n') : '');
  ui.alert('Athinorama Sync', msg, ui.ButtonSet.OK);
}


/**
 * Sync μόνο του τρέχοντος ενεργού sheet.
 */
function syncCurrentSheet() {
  const ui = SpreadsheetApp.getUi();
  const sheet = SpreadsheetApp.getActiveSheet();
  const sheetName = sheet.getName();

  // Έλεγχος αν το sheet ανήκει στη λίστα
  if (!SHEETS_TO_SYNC.includes(sheetName)) {
    ui.alert('Αυτό το sheet (' + sheetName + ') δεν βρίσκεται στη λίστα SHEETS_TO_SYNC.');
    return;
  }

  const rows = _readSheet(sheet);
  const result = _sendToBackend(sheetName, rows);

  ui.alert(
    'Sync "' + sheetName + '"',
    '✅ ' + (result.synced || 0) + ' εγγραφές αποθηκεύτηκαν.\n' +
    (result.errors && result.errors.length > 0 ? '⚠ Σφάλματα: ' + result.errors.join('\n') : ''),
    ui.ButtonSet.OK
  );
}


/**
 * Διαβάζει ένα sheet και επιστρέφει array από objects.
 * Η πρώτη γραμμή θεωρείται headers.
 */
function _readSheet(sheet) {
  const data = sheet.getDataRange().getValues();
  if (data.length < 2) return [];

  const headers = data[0].map(function(h) {
    // Κανονικοποίηση headers σε snake_case
    return String(h).trim().toLowerCase().replace(/\s+/g, '_');
  });

  const rows = [];
  for (var i = 1; i < data.length; i++) {
    const row = data[i];

    // Παράλειψη τελείως κενών γραμμών
    const isEmpty = row.every(function(cell) { return cell === '' || cell === null; });
    if (isEmpty) continue;

    const obj = {};
    headers.forEach(function(header, idx) {
      obj[header] = row[idx] !== undefined ? row[idx] : '';
    });

    // Προσθήκη list_type βάσει sheet name
    obj['list_type'] = _getListType(sheet.getName());

    rows.push(obj);
  }

  return rows;
}


/**
 * Αντιστοιχεί το όνομα sheet στο list_type του backend.
 */
function _getListType(sheetName) {
  const mapping = {
    'movies i have seen': 'seen',
    'favourite movies':   'favourite',
    'series i have seen': 'series_seen',
    'favourite series':   'series_favourite',
  };
  return mapping[sheetName.toLowerCase().trim()] || 'seen';
}


/**
 * Αποστέλλει rows στο backend με POST /api/user/sync.
 * Επιστρέφει το JSON response.
 */
function _sendToBackend(sheetName, rows) {
  if (!BACKEND_URL || BACKEND_URL.includes('your-app')) {
    Logger.log('ΣΦΑΛΜΑ: Δεν έχεις ρυθμίσει το BACKEND_URL!');
    return { synced: 0, errors: ['BACKEND_URL δεν έχει ρυθμιστεί'] };
  }

  const payload = {
    api_key: SYNC_API_KEY,
    sheet: sheetName,
    rows: rows,
  };

  const options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'Authorization': 'Bearer ' + SYNC_API_KEY,
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  };

  try {
    const response = UrlFetchApp.fetch(BACKEND_URL + '/api/user/sync', options);
    const code = response.getResponseCode();
    const body = response.getContentText();

    if (code !== 200) {
      Logger.log('Backend error ' + code + ': ' + body);
      return { synced: 0, errors: ['HTTP ' + code + ': ' + body.slice(0, 100)] };
    }

    return JSON.parse(body);
  } catch (e) {
    Logger.log('Σφάλμα αίτησης: ' + e.toString());
    return { synced: 0, errors: [e.toString()] };
  }
}


/**
 * Δημιουργεί εβδομαδιαίο time-based trigger (κάθε Κυριακή).
 */
function setupWeeklyTrigger() {
  // Διαγραφή υπαρχόντων triggers για αυτή τη συνάρτηση
  deleteWeeklyTrigger();

  ScriptApp.newTrigger('syncNow')
    .timeBased()
    .onWeekDay(ScriptApp.WeekDay.SUNDAY)
    .atHour(6)  // 6:00 π.μ.
    .create();

  SpreadsheetApp.getUi().alert(
    'Αυτόματο sync ρυθμίστηκε!\nΘα τρέχει κάθε Κυριακή στις 6:00 π.μ.'
  );
}


/**
 * Διαγράφει τους εβδομαδιαίους triggers.
 */
function deleteWeeklyTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (trigger.getHandlerFunction() === 'syncNow') {
      ScriptApp.deleteTrigger(trigger);
    }
  });
}

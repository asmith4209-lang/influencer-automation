/**
 * Amazon Influencer — Google Apps Script receiver
 *
 * SETUP:
 * 1. Go to script.google.com → New project
 * 2. Paste this entire file
 * 3. Replace SPREADSHEET_ID below with your actual sheet ID
 * 4. Click Deploy → New deployment → Web app
 *    - Execute as: Me
 *    - Who has access: Anyone
 * 5. Copy the deployment URL — you'll paste it into the bookmarklet
 */

var SPREADSHEET_ID = "1yKa5Sb1e0ru4YMdzBoc7tFbbeBcFmJyjh28LpbS05Tk";

// Column order in the sheet (1-indexed)
var COL_DATE        = 1;
var COL_PRODUCT     = 2;
var COL_ASIN        = 3;
var COL_PRODUCT_URL = 4;
var COL_SELLER      = 5;
var COL_PRICE       = 6;
var COL_FEE         = 7;
var COL_PAID        = 8;
var COL_STATUS      = 9;

function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);
    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);

    // Get current month tab by name (e.g. "February")
    var monthName = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "MMMM");
    var sheet = ss.getSheetByName(monthName);

    if (!sheet) {
      logError("Sheet tab not found: " + monthName);
      return respond({ error: "Tab '" + monthName + "' not found" });
    }

    var today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "M/d/yyyy");

    // Find first empty row in column A (skipping header row 1)
    var dates = sheet.getRange("A2:A" + sheet.getMaxRows()).getValues();
    var newRow = 2;
    for (var i = 0; i < dates.length; i++) {
      if (dates[i][0] === "") { newRow = i + 2; break; }
    }

    sheet.getRange(newRow, COL_DATE).setValue(today);
    sheet.getRange(newRow, COL_PRODUCT).setValue(data.title || "");
    sheet.getRange(newRow, COL_ASIN).setValue(data.asin || "");
    sheet.getRange(newRow, COL_PRODUCT_URL).setValue(data.url || "");
    sheet.getRange(newRow, COL_SELLER).setValue("");         // she fills in
    sheet.getRange(newRow, COL_PRICE).setValue(data.price || 0);
    sheet.getRange(newRow, COL_FEE).setValue(0);
    sheet.getRange(newRow, COL_PAID).insertCheckboxes();
    sheet.getRange(newRow, COL_STATUS).setValue("Received");

    return respond({ success: true, row: newRow, product: data.title });

  } catch (err) {
    logError(err.message);
    return respond({ error: err.message });
  }
}

// Health check
function doGet(e) {
  return respond({ status: "ok" });
}

function respond(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function logError(msg) {
  var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  var log = ss.getSheetByName("Errors");
  if (!log) {
    log = ss.insertSheet("Errors");
    log.appendRow(["Timestamp", "Error"]);
  }
  log.appendRow([new Date(), msg]);
}

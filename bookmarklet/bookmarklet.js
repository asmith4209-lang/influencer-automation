/**
 * Amazon Influencer Bookmarklet — SOURCE (readable version)
 *
 * The deployed Apps Script URL lives in APPS_SCRIPT_URL below.
 * Do not edit it by hand — run ./set-url.sh <new-exec-url> from the
 * bookmarklet/ folder, which rewrites this file, bookmarklet.min.js and
 * the APPS_SCRIPT_URL entry in watcher/.env together.
 *
 * See bookmarklet.min.js for the ready-to-paste version.
 */

(function () {
  var APPS_SCRIPT_URL = "PASTE_YOUR_APPS_SCRIPT_URL_HERE";

  if (APPS_SCRIPT_URL.indexOf("PASTE_") === 0) {
    showBanner("Bookmarklet not configured — no Apps Script URL", "error");
    return;
  }

  // --- Extract ASIN from URL ---
  var asinMatch =
    window.location.href.match(/\/dp\/([A-Z0-9]{10})/i) ||
    window.location.href.match(/\/gp\/product\/([A-Z0-9]{10})/i) ||
    window.location.href.match(/[?&]asin=([A-Z0-9]{10})/i);

  if (!asinMatch) {
    showBanner("Not an Amazon product page", "error");
    return;
  }

  var asin = asinMatch[1].toUpperCase();

  // --- Scrape page data ---
  var title =
    (document.querySelector("#productTitle") || {}).innerText || "";
  title = title.trim();

  var priceWhole =
    (document.querySelector(".a-price-whole") || {}).innerText || "0";
  var priceFrac =
    (document.querySelector(".a-price-fraction") || {}).innerText || "00";
  var price = parseFloat(
    priceWhole.replace(/[^0-9]/g, "") + "." + priceFrac.replace(/[^0-9]/g, "")
  );

  var cleanUrl = "https://www.amazon.com/dp/" + asin;

  var seller = "";
  var bylineEl = document.querySelector("#bylineInfo");
  if (bylineEl) {
    var firstLink = bylineEl.querySelector("a");
    if (firstLink) {
      seller = firstLink.innerText.trim();
    } else {
      seller = bylineEl.innerText
        .replace(/^Visit the\s+/i, "")
        .replace(/\s+Store$/i, "")
        .replace(/^Brand:\s*/i, "")
        .trim();
    }
  }

  var payload = {
    asin: asin,
    title: title,
    price: isNaN(price) ? 0 : price,
    url: cleanUrl,
    seller: seller,
  };

  showBanner("Sending " + asin + "…", "wait");

  // --- Send to Apps Script ---
  // Content-Type text/plain keeps this a CORS-"simple" request (no preflight,
  // which Apps Script cannot answer). A web app deployed with access "Anyone"
  // returns Access-Control-Allow-Origin: *, so the reply IS readable — which is
  // what lets us tell a real success from a dead deployment. Do NOT switch this
  // back to mode:"no-cors": that makes every response opaque, so fetch resolves
  // even on a 404 and the banner goes green while nothing reaches the sheet.
  fetch(APPS_SCRIPT_URL, {
    method: "POST",
    headers: { "Content-Type": "text/plain;charset=utf-8" },
    body: JSON.stringify(payload),
    redirect: "follow",
  })
    .then(function (res) {
      return res.text().then(function (text) {
        return { status: res.status, text: text };
      });
    })
    .then(function (res) {
      var data = null;
      try { data = JSON.parse(res.text); } catch (e) { /* not JSON */ }

      if (!data) {
        if (res.status === 404 || /Page Not Found|does not exist/i.test(res.text)) {
          showBanner(
            "NOT ADDED — Apps Script deployment is gone (404). It needs redeploying.",
            "error"
          );
        } else {
          showBanner("NOT ADDED — unexpected reply (HTTP " + res.status + ")", "error");
        }
        return;
      }

      if (data.error) {
        showBanner("NOT ADDED — sheet error: " + data.error, "error");
        return;
      }

      showBanner(
        "Added to row " + (data.row || "?") + ": " + (title.substring(0, 40) || asin),
        "success"
      );
    })
    .catch(function (err) {
      showBanner("NOT ADDED — could not reach the sheet: " + err.message, "error");
    });

  // --- Floating notification banner ---
  function showBanner(msg, type) {
    var existing = document.getElementById("__influencer_banner__");
    if (existing) existing.remove();

    var banner = document.createElement("div");
    banner.id = "__influencer_banner__";
    banner.innerText = msg;
    Object.assign(banner.style, {
      position: "fixed",
      top: "20px",
      right: "20px",
      zIndex: "999999",
      padding: "14px 20px",
      borderRadius: "8px",
      fontSize: "15px",
      fontWeight: "bold",
      fontFamily: "sans-serif",
      color: "#fff",
      background: type === "error" ? "#c0392b" : type === "wait" ? "#b8860b" : "#27ae60",
      boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
      maxWidth: "360px",
    });

    document.body.appendChild(banner);
    if (type !== "wait") setTimeout(function () { banner.remove(); }, 6000);
  }
})();

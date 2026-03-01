/**
 * Amazon Influencer Bookmarklet — SOURCE (readable version)
 *
 * Replace APPS_SCRIPT_URL with your deployed Google Apps Script URL.
 * Then minify this and prefix with "javascript:" to install as a bookmark.
 *
 * See bookmarklet.min.js for the ready-to-paste version.
 */

(function () {
  var APPS_SCRIPT_URL = "PASTE_YOUR_APPS_SCRIPT_URL_HERE";

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

  var payload = {
    asin: asin,
    title: title,
    price: isNaN(price) ? 0 : price,
    url: cleanUrl,
  };

  // --- Send to Apps Script ---
  fetch(APPS_SCRIPT_URL, {
    method: "POST",
    mode: "no-cors", // Apps Script doesn't support CORS — data still sends
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then(function () {
      showBanner("Added to sheet: " + (title.substring(0, 40) || asin), "success");
    })
    .catch(function (err) {
      showBanner("Error: " + err.message, "error");
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
      background: type === "error" ? "#c0392b" : "#27ae60",
      boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
      maxWidth: "360px",
    });

    document.body.appendChild(banner);
    setTimeout(function () { banner.remove(); }, 4000);
  }
})();

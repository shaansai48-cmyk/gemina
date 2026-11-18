/**
 * Google Apps Script Multi-Ticker Webhook Buffer for Gemina TraderAgent
 * Buffers simultaneous 15m TradingView alerts by ticker in PropertiesService.
 */
function doPost(e) {
  var raw = e.postData.contents;
  var data = JSON.parse(raw);
  var ticker = (data.ticker || "DEFAULT").toUpperCase();
  var now = new Date().toISOString();
  var props = PropertiesService.getScriptProperties();

  var barsJson = props.getProperty("bars_map");
  var bars = barsJson ? JSON.parse(barsJson) : {};
  bars[ticker] = { timestamp: data.time || now, payload: data };

  props.setProperty("bars_map", JSON.stringify(bars));
  props.setProperty("latest_ts", data.time || now);
  props.setProperty("latest_payload", raw);

  return ContentService.createTextOutput(JSON.stringify({ status: "ok", ticker: ticker }))
    .setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) {
  var props = PropertiesService.getScriptProperties();
  var barsJson = props.getProperty("bars_map");
  var latestPayload = props.getProperty("latest_payload");

  var response = {
    timestamp: props.getProperty("latest_ts") || null,
    payload: latestPayload ? JSON.parse(latestPayload) : null,
    bars: barsJson ? JSON.parse(barsJson) : {}
  };

  return ContentService.createTextOutput(JSON.stringify(response))
    .setMimeType(ContentService.MimeType.JSON);
}

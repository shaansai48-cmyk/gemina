# TradingView Pine Script v6 & Google Apps Script Webhook Setup

## 1. Google Apps Script Webhook Buffer

Paste [google_apps_script.gs](../subprojects/traderagent/integrations/google_apps_script.gs) into your Google Apps Script project and deploy as a Web App (**Execute as: Me**, **Who has access: Anyone**). Whenever you update `Code.gs`, click **Deploy -> Manage deployments -> Edit -> Version: New version -> Deploy**.

## 2. TradingView Pine Script v6 Indicator

1. Open TradingView and switch your chart timeframe to **`15m`**.
2. Open the **Pine Editor**, paste [tradingview_indicator.pine](../subprojects/traderagent/integrations/tradingview_indicator.pine), and click **Add to chart**.
3. For each stock in your watchlist (`NVDA`, `OMXSTO:ERIC_B`, `MSFT`), press **`Alt + A`** to create an alert:
   * **Condition:** `Avanza Deep-Quant 15m Telemetry Engine (v6)` $\rightarrow$ `Any alert() function call`
   * **Webhook URL:** Your Google Apps Script `/exec` URL.

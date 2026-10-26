# Real Exchange Rate (RER) Explorer

A Streamlit web app that compares the **bilateral real exchange rate** between any two
countries (including the **Euro area**, IMF code `G163`) using **monthly IMF CPI and
exchange-rate data** retrieved live from the IMF SDMX 3.0 API.

## What the app does

1. You pick **Country 1** and **Country 2** from the IMF country list (names shown, ISO3/IMF codes used internally).
2. The app downloads monthly CPI and USD exchange rates for both countries from **January 1976** (or the first available month).
3. You choose a **start month** and **end month** (default: first to last valid month).
4. The app computes S(t), RER(t), RER\* and the Real Index, and shows:
   - an interactive Plotly chart — **Real**, **Nominal** or **Both** (two y-axes),
   - a table of all observations and calculations in the sample,
   - a **CSV download** of that table (it updates with the countries and dates).

## Economic definitions

| Symbol | Meaning |
|---|---|
| `E1(t)` | IMF exchange rate of Country 1: units of Country 1 currency per 1 USD |
| `E2(t)` | IMF exchange rate of Country 2: units of Country 2 currency per 1 USD |
| `S(t)` | Nominal exchange rate: **units of Country 2 currency per 1 unit of Country 1 currency** |
| `P1(t)` | Monthly CPI of Country 1 |
| `P2(t)` | Monthly CPI of Country 2 |

```
S(t)            = E2(t) / E1(t)
RER(t)          = S(t) × P1(t) / P2(t)
RER*            = average of RER(t) over the valid months in the selected sample
Real Index(t)   = 100 × RER(t) / RER*
```

**Interpretation.** RER(t) is the price of Country 1's consumption basket relative to Country 2's,
both in Country 2 currency. A **rise** means a **real appreciation of Country 1's currency**
(a real depreciation of Country 2's); a fall means the opposite.

### Converting the IMF exchange rates into S(t)

The IMF series `{ISO3}.XDC_USD.PA_RT.M` is, per the IMF codelists, indicator `XDC_USD` =
*"Domestic currency per US Dollar"* and transformation `PA_RT` = *period average*; `M` = monthly.
So `E_i` has units *(country-i currency) / USD*. Dividing:

```
E2 / E1 = (C2 currency / USD) / (C1 currency / USD) = C2 currency / C1 currency
```

which is exactly units of Country 2 currency per 1 unit of Country 1 currency.

*Check:* Country 1 = United States (E1 = 1), Country 2 = Japan (E2 ≈ 157 JPY/USD) → S ≈ 157
**yen per US dollar**, as required. Reversing the countries gives S ≈ 0.0064 USD per yen.

### Real Index and the sample period

RER\* is computed **only over the valid months between the selected start and end month**,
so the Real Index averages exactly 100 over that sample. Changing either date changes RER\*
and therefore the whole Real Index. Because CPIs are indices with arbitrary base years, the
level of RER(t) has no meaning on its own; the base year only multiplies RER(t) by a constant,
which cancels in the Real Index.

The **nominal** chart shows S(t) in actual currency units (e.g. JPY per USD); it is never normalised.

## IMF data source

- API: `https://api.imf.org/external/sdmx/3.0` (agency `IMF.STA`)
- Request pattern: `GET /data/dataflow/IMF.STA/{FLOW}/+/{KEY}?c[TIME_PERIOD]=ge:1976-01`
  with header `Accept: application/vnd.sdmx.data+json;version=2.0.0` (SDMX-JSON). If the API
  ever answers in SDMX-ML (XML), the app parses the XML instead.

| Data | Dataflow | Key |
|---|---|---|
| Monthly CPI | `CPI` | `{ISO3}.CPI._T.IX.M` (all items, index, monthly) |
| Monthly exchange rate | `ER` | `{ISO3}.XDC_USD.PA_RT.M` (domestic currency per USD, period average) |
| Euro area prices | `CPI` | `G163.HICP._T.IX.M` (see note below) |

- **Euro area (`G163`)**: the IMF publishes no `G163.CPI._T.IX.M` series; euro-area consumer prices
  are published as the HICP. The app therefore falls back to `{ISO3}.HICP._T.IX.M` whenever the
  `CPI` series is empty. Euro-area data start in 1999 (exchange rate) / Dec 1999 (HICP).
- **Euro-area members** (e.g. Germany, `DEU`): the IMF's national-currency-per-USD series ends in
  1998 (legacy currency). For recent years select *Euro Area (EA)*.
- The country list is built from the countries that actually have both series (via
  `attributes=none&measures=none` series-key queries); names come from codelist `IMF.STA:CL_ER_COUNTRY_PUB`.

## Missing data

- The four series are merged by month (outer join).
- A month is used **only if CPI 1, CPI 2, E1 and E2 are all present and positive**; others are dropped
  (the app reports how many months in the sample were excluded).
- If a series is unavailable, or the two countries have no overlapping months, the app shows an
  error message instead of crashing. Network/API failures are retried and then reported.
- Selecting the same country twice, or a start month after the end month, produces a warning.

## Caching

IMF downloads are cached with `st.cache_data` (24 h), so switching chart type or dates does not
re-download data.

## Running the app

**Required Python version: 3.12.**

Online (no local Python needed): deploy on Streamlit Community Cloud (see below).

Locally (optional):
```bash
pip install -r requirements.txt
streamlit run app.py
```

### Deploy on Streamlit Community Cloud
1. Push `app.py`, `requirements.txt`, `README.md` to a public GitHub repository.
2. Go to https://share.streamlit.io, sign in with GitHub, click **Create app** → *Deploy a public app from GitHub*.
3. Repository: `<your-username>/real-exchange-rate-app`, branch `main`, main file `app.py`.
4. Under **Advanced settings**, choose **Python 3.12**. Click **Deploy**.
5. The public URL appears in the browser (e.g. `https://<name>.streamlit.app`).

## Files

| File | Purpose |
|---|---|
| `app.py` | The Streamlit app: IMF API calls, parsing, RER calculations, charts, table, CSV download |
| `requirements.txt` | Python dependencies (streamlit, pandas, requests, plotly) |
| `README.md` | This documentation |

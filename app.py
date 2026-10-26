"""
Real Exchange Rate (RER) Explorer
=================================

Streamlit app that compares the bilateral real exchange rate between any two
countries using monthly IMF data retrieved from the IMF SDMX 3.0 API.

Definitions (Country 1 = "home", Country 2 = "foreign"):

    E_i(t)  = IMF exchange rate of country i, in units of country-i currency per 1 USD
              (IMF ER dataflow, key {ISO3}.XDC_USD.PA_RT.M, monthly period average)
    S(t)    = units of Country 2 currency per 1 unit of Country 1 currency
            = E_2(t) / E_1(t)
    P_i(t)  = monthly CPI of country i (IMF CPI dataflow, key {ISO3}.CPI._T.IX.M)
    RER(t)  = S(t) * P_1(t) / P_2(t)
    RER*    = mean of RER(t) over the valid months in the user-selected sample
    Index(t)= 100 * RER(t) / RER*

Python 3.12 compatible.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_BASE = "https://api.imf.org/external/sdmx/3.0"
AGENCY = "IMF.STA"
START_PERIOD = "1976-01"  # earliest month requested from the IMF

# Data-flow ids and series keys (dimension order verified against the IMF DSDs)
#   CPI flow: COUNTRY.INDEX_TYPE.COICOP_1999.TYPE_OF_TRANSFORMATION.FREQUENCY
#   ER  flow: COUNTRY.INDICATOR.TYPE_OF_TRANSFORMATION.FREQUENCY
CPI_FLOW = "CPI"
ER_FLOW = "ER"
CPI_KEY = "{code}.CPI._T.IX.M"  # all-items consumer price index, monthly
HICP_KEY = "{code}.HICP._T.IX.M"  # fallback (the Euro area G163 publishes HICP, not "CPI")
ER_KEY = "{code}.XDC_USD.PA_RT.M"  # domestic currency per USD, period average, monthly

EURO_AREA = "G163"
COUNTRY_CODELIST = "CL_ER_COUNTRY_PUB"  # names for ISO3 / IMF country codes (incl. G163)

DATA_ACCEPT = "application/vnd.sdmx.data+json;version=2.0.0"
STRUCTURE_ACCEPT = "application/vnd.sdmx.structure+json;version=2.0.0"
HTTP_HEADERS = {"User-Agent": "real-exchange-rate-app/1.0 (Streamlit; educational use)"}
TIMEOUT_SECONDS = 60
MAX_ATTEMPTS = 3
CACHE_TTL_SECONDS = 24 * 60 * 60  # IMF data are updated at most a few times per month

EURO_MEMBERS = {
    "AUT", "BEL", "CYP", "DEU", "ESP", "EST", "FIN", "FRA", "GRC", "HRV",
    "IRL", "ITA", "LTU", "LUX", "LVA", "MLT", "NLD", "PRT", "SVK", "SVN",
}

PERIOD_RE = re.compile(r"^(\d{4})-?M?(\d{1,2})$")


class IMFError(Exception):
    """Raised when the IMF API cannot be reached or returns an unusable response."""


# ---------------------------------------------------------------------------
# IMF SDMX helpers
# ---------------------------------------------------------------------------
def _get_json(path: str, params: dict | None = None, accept: str = DATA_ACCEPT) -> dict:
    """GET an IMF SDMX 3.0 resource and return the decoded SDMX-JSON payload.

    A 404 ("no results") is returned as an empty dict so callers can treat it as
    "no data". Transient errors (timeouts, 429, 5xx) are retried.
    """
    url = f"{API_BASE}/{path}"
    headers = {**HTTP_HEADERS, "Accept": accept}
    last_error = "unknown error"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            last_error = f"network error: {exc}"
        else:
            if resp.status_code == 404:
                return {}
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    # The API serves SDMX-ML (XML) when JSON is not negotiated; parse that instead.
                    try:
                        return _xml_to_sdmx_json(resp.content)
                    except ET.ParseError as exc:
                        raise IMFError(f"The IMF API returned an unreadable response for {url}") from exc
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code not in (429, 500, 502, 503, 504):
                break
        if attempt < MAX_ATTEMPTS:
            time.sleep(1.5 * attempt)
    raise IMFError(f"IMF API request failed ({last_error}): {url}")


def _local(tag: str) -> str:
    """Strip an XML namespace: '{ns}Series' -> 'Series'."""
    return tag.rsplit("}", 1)[-1]


def _xml_to_sdmx_json(content: bytes) -> dict:
    """Convert an SDMX-ML 3.0 StructureSpecificData message into the SDMX-JSON shape
    used by the rest of the app (<Series COUNTRY=...><Obs TIME_PERIOD=... OBS_VALUE=.../>)."""
    root = ET.fromstring(content)
    countries: list[str] = []
    periods: list[str] = []
    series_out: dict[str, dict] = {}
    for n, series in enumerate(el for el in root.iter() if _local(el.tag) == "Series"):
        country = series.attrib.get("COUNTRY", "")
        if country not in countries:
            countries.append(country)
        observations: dict[str, list] = {}
        for obs in series:
            if _local(obs.tag) != "Obs" or "TIME_PERIOD" not in obs.attrib:
                continue
            period = obs.attrib["TIME_PERIOD"]
            if period not in periods:
                periods.append(period)
            observations[str(periods.index(period))] = [obs.attrib.get("OBS_VALUE")]
        series_out[f"{countries.index(country)}:{n}"] = {"observations": observations}
    if not series_out:
        return {}
    return {
        "data": {
            "dataSets": [{"series": series_out}],
            "structures": [{
                "dimensions": {
                    "series": [{"id": "COUNTRY", "values": [{"id": c} for c in countries]},
                               {"id": "SERIES_NO", "values": []}],
                    "observation": [{"id": "TIME_PERIOD", "values": [{"value": p} for p in periods]}],
                },
                "measures": {"observation": [{"id": "OBS_VALUE"}]},
            }],
        }
    }


def _parse_period(period: str) -> pd.Timestamp | None:
    """Convert an SDMX monthly period such as '2025-M01' (or '2025-01') to a Timestamp."""
    match = PERIOD_RE.match(str(period).strip())
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return pd.Timestamp(year=year, month=month, day=1)


def parse_sdmx_series(payload: dict) -> pd.Series:
    """Parse an SDMX-JSON 2.0 data message into a monthly float Series.

    Observations are keyed by an index into the TIME_PERIOD values of the
    structure; the observation array holds measures first (OBS_VALUE) and then
    observation-level attributes.
    """
    empty = pd.Series(dtype="float64", name="value")
    data = (payload or {}).get("data") or {}
    datasets = data.get("dataSets") or []
    structures = data.get("structures") or []
    if not datasets or not structures:
        return empty

    structure = structures[0]
    obs_dims = (structure.get("dimensions") or {}).get("observation") or []
    time_pos = next((i for i, d in enumerate(obs_dims) if d.get("id") == "TIME_PERIOD"), None)
    if time_pos is None:
        return empty
    time_values = obs_dims[time_pos].get("values") or []

    measures = (structure.get("measures") or {}).get("observation") or []
    value_pos = next((i for i, m in enumerate(measures) if m.get("id") == "OBS_VALUE"), 0)

    records: dict[pd.Timestamp, float] = {}
    for series in (datasets[0].get("series") or {}).values():
        for obs_key, obs in (series.get("observations") or {}).items():
            try:
                period_idx = int(str(obs_key).split(":")[time_pos])
                period_info = time_values[period_idx]
            except (ValueError, IndexError):
                continue
            date = _parse_period(period_info.get("value") or period_info.get("id") or "")
            if date is None or not obs or len(obs) <= value_pos:
                continue
            value = pd.to_numeric(obs[value_pos], errors="coerce")
            if pd.notna(value):
                records.setdefault(date, float(value))

    if not records:
        return empty
    return pd.Series(records, name="value", dtype="float64").sort_index()


def _series_key_countries(flow: str, key_pattern: str) -> set[str]:
    """Return the set of country codes for which a (wildcarded) series key exists."""
    payload = _get_json(
        f"data/dataflow/{AGENCY}/{flow}/+/{key_pattern}",
        params={"attributes": "none", "measures": "none"},
    )
    data = payload.get("data") or {}
    datasets = data.get("dataSets") or []
    structures = data.get("structures") or []
    if not datasets or not structures:
        return set()
    series_dims = (structures[0].get("dimensions") or {}).get("series") or []
    country_pos = next((i for i, d in enumerate(series_dims) if d.get("id") == "COUNTRY"), 0)
    country_values = series_dims[country_pos].get("values") or []
    codes: set[str] = set()
    for key in (datasets[0].get("series") or {}):
        try:
            codes.add(country_values[int(key.split(":")[country_pos])]["id"])
        except (ValueError, IndexError, KeyError):
            continue
    return codes


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_country_list() -> pd.DataFrame:
    """Countries that have both a monthly CPI (or HICP) and a monthly USD exchange rate."""
    er_codes = _series_key_countries(ER_FLOW, ER_KEY.format(code="*"))
    cpi_codes = _series_key_countries(CPI_FLOW, CPI_KEY.format(code="*"))
    try:
        hicp_codes = _series_key_countries(CPI_FLOW, HICP_KEY.format(code="*"))
    except IMFError:
        hicp_codes = set()
    codes = er_codes & (cpi_codes | hicp_codes)
    codes.add(EURO_AREA)  # required option; data availability is checked when selected
    if not er_codes:
        raise IMFError("The IMF API returned no exchange-rate series.")

    names: dict[str, str] = {}
    try:
        payload = _get_json(
            f"structure/codelist/{AGENCY}/{COUNTRY_CODELIST}/+", accept=STRUCTURE_ACCEPT
        )
        for codelist in (payload.get("data") or {}).get("codelists") or []:
            for code in codelist.get("codes") or []:
                name = code.get("name")
                if isinstance(name, dict):  # localised form {"en": "..."}
                    name = name.get("en") or next(iter(name.values()), None)
                if code.get("id") and name:
                    names[code["id"]] = str(name)
    except IMFError:
        pass  # fall back to showing codes only
    names.setdefault(EURO_AREA, "Euro Area (EA)")

    df = pd.DataFrame({"code": sorted(codes)})
    df["name"] = df["code"].map(lambda c: names.get(c, c))
    return df.sort_values("name", key=lambda s: s.str.lower()).reset_index(drop=True)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def fetch_series(flow: str, key: str) -> pd.Series:
    """Download one monthly IMF series from START_PERIOD onwards (cached)."""
    payload = _get_json(
        f"data/dataflow/{AGENCY}/{flow}/+/{key}",
        params={"c[TIME_PERIOD]": f"ge:{START_PERIOD}"},
    )
    return parse_sdmx_series(payload)


def fetch_cpi(code: str) -> tuple[pd.Series, str]:
    """Monthly CPI; falls back to HICP when no 'CPI' series exists (e.g. Euro area G163)."""
    key = CPI_KEY.format(code=code)
    series = fetch_series(CPI_FLOW, key)
    if series.empty:
        hicp_key = HICP_KEY.format(code=code)
        hicp = fetch_series(CPI_FLOW, hicp_key)
        if not hicp.empty:
            return hicp, hicp_key
    return series, key


def fetch_er(code: str) -> tuple[pd.Series, str]:
    """Monthly exchange rate in domestic currency per USD (period average)."""
    key = ER_KEY.format(code=code)
    return fetch_series(ER_FLOW, key), key


# ---------------------------------------------------------------------------
# Economics
# ---------------------------------------------------------------------------
def build_rer_frame(cpi1: pd.Series, cpi2: pd.Series, er1: pd.Series, er2: pd.Series) -> pd.DataFrame:
    """Merge the four monthly series by month and compute S(t) and RER(t).

    Only months in which all four inputs are present and strictly positive are kept.
    """
    merged = pd.concat(
        {"CPI1": cpi1, "CPI2": cpi2, "ER1": er1, "ER2": er2}, axis=1, join="outer", sort=True
    ).sort_index()
    valid = merged.notna().all(axis=1) & (merged > 0).all(axis=1)
    out = merged.loc[valid].copy()
    # E_i = country-i currency per USD  =>  S = (C2 per USD) / (C1 per USD) = C2 per 1 unit of C1
    out["S"] = out["ER2"] / out["ER1"]
    # RER = S * P1 / P2  (Country 1 CPI in the numerator, Country 2 CPI in the denominator)
    out["RER"] = out["S"] * out["CPI1"] / out["CPI2"]
    out.index.name = "Date"
    return out


def apply_sample(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, float]:
    """Restrict to [start, end], compute RER* over that window only, and the Real Index."""
    sample = frame.loc[(frame.index >= start) & (frame.index <= end)].copy()
    if sample.empty:
        return sample, float("nan")
    rer_star = float(sample["RER"].mean())
    sample["RER_STAR"] = rer_star
    sample["REAL_INDEX"] = 100.0 * sample["RER"] / rer_star
    return sample, rer_star


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------
def month_label(ts: pd.Timestamp) -> str:
    return ts.strftime("%b %Y")


def describe_coverage(series: pd.Series) -> str:
    if series.empty:
        return "no data"
    return f"{month_label(series.index.min())} – {month_label(series.index.max())} ({len(series)} months)"


def make_chart(sample: pd.DataFrame, view: str, name1: str, name2: str) -> go.Figure:
    nominal_unit = f"{name2} currency per 1 {name1} currency"
    dates = sample.index
    real_trace = go.Scatter(
        x=dates,
        y=sample["REAL_INDEX"],
        name="Real Index (sample average = 100)",
        mode="lines",
        line=dict(color="#1f4e79", width=2.2),
        hovertemplate="%{x|%b %Y}<br>Real Index: %{y:.2f}<extra></extra>",
    )
    nominal_trace = go.Scatter(
        x=dates,
        y=sample["S"],
        name=f"Nominal S(t): {nominal_unit}",
        mode="lines",
        line=dict(color="#c0504d", width=1.8),
        hovertemplate="%{x|%b %Y}<br>S(t): %{y:,.6g}<extra></extra>",
    )

    if view == "Real":
        fig = go.Figure(real_trace)
        fig.add_hline(y=100, line_dash="dot", line_color="grey",
                      annotation_text="RER* = 100", annotation_position="top left")
        fig.update_yaxes(title_text="Real Index (RER* = 100)")
        title = f"Real exchange rate index: {name1} vs {name2}"
    elif view == "Nominal":
        fig = go.Figure(nominal_trace)
        fig.update_yaxes(title_text=nominal_unit, tickformat=",.6g")
        title = f"Nominal exchange rate S(t): {nominal_unit}"
    else:
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(real_trace, secondary_y=False)
        fig.add_trace(nominal_trace, secondary_y=True)
        fig.add_hline(y=100, line_dash="dot", line_color="grey", secondary_y=False)
        fig.update_yaxes(title_text="Real Index (RER* = 100)", secondary_y=False)
        fig.update_yaxes(title_text=f"S(t): {nominal_unit}", tickformat=",.6g",
                         secondary_y=True, showgrid=False)
        title = f"Real Index vs nominal S(t): {name1} vs {name2}"

    fig.update_layout(
        title=title,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="left", x=0),
        margin=dict(l=10, r=10, t=60, b=10),
        height=520,
    )
    fig.update_xaxes(title_text="Month")
    return fig


def build_output_table(sample: pd.DataFrame, name1: str, name2: str) -> pd.DataFrame:
    table = pd.DataFrame(
        {
            "Date": sample.index.strftime("%Y-%m"),
            "Country 1": name1,
            "Country 2": name2,
            "CPI 1": sample["CPI1"].to_numpy(),
            "CPI 2": sample["CPI2"].to_numpy(),
            "ER 1 (Country 1 currency per USD)": sample["ER1"].to_numpy(),
            "ER 2 (Country 2 currency per USD)": sample["ER2"].to_numpy(),
            "S(t) (Country 2 currency per Country 1 currency)": sample["S"].to_numpy(),
            "RER(t) = S x CPI1 / CPI2": sample["RER"].to_numpy(),
            "RER* (sample mean of RER)": sample["RER_STAR"].to_numpy(),
            "Real Index(t) = 100 x RER / RER*": sample["REAL_INDEX"].to_numpy(),
        }
    )
    return table


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="Real Exchange Rate Explorer", page_icon="💱", layout="wide")

    st.title("Real Exchange Rate Explorer")
    st.caption("Monthly bilateral real exchange rates from IMF CPI and exchange-rate data (SDMX 3.0 API)")

    st.markdown(
        "The **real exchange rate (RER)** adjusts the nominal exchange rate for relative price "
        "levels. It measures how expensive Country 1's goods are compared with Country 2's goods "
        "once both are expressed in the same currency. A **rise** means Country 1's currency has "
        "**appreciated in real terms** against Country 2 (Country 1 becomes relatively more "
        "expensive); a **fall** means a real depreciation of Country 1's currency."
    )
    with st.expander("Formulas used", expanded=False):
        st.latex(r"S(t)=\frac{E_2(t)}{E_1(t)}\quad\text{(Country 2 currency per 1 unit of Country 1 currency)}")
        st.markdown(
            "where $E_i(t)$ is the IMF exchange rate of country *i* in **units of country-i currency "
            "per US dollar** (series `{ISO3}.XDC_USD.PA_RT.M`, monthly period average)."
        )
        st.latex(r"RER(t)=S(t)\times\frac{P_1(t)}{P_2(t)}")
        st.latex(r"RER^*=\frac{1}{N}\sum_{t=\text{start}}^{\text{end}}RER(t)\qquad"
                 r"\text{Real Index}(t)=100\times\frac{RER(t)}{RER^*}")
        st.markdown(
            "$P_1, P_2$ are monthly CPIs (`{ISO3}.CPI._T.IX.M`). $RER^*$ is the average over the "
            "**valid months of the sample you select**, so the Real Index averages exactly 100 over "
            "that sample and is recalculated whenever you change the dates. The nominal rate "
            "$S(t)$ is shown in actual currency units (not normalised)."
        )

    # ---- Country list ----------------------------------------------------
    try:
        with st.spinner("Loading IMF country list…"):
            countries = load_country_list()
    except IMFError as exc:
        st.error(f"Could not load the country list from the IMF API. Please try again later.\n\n{exc}")
        st.stop()

    codes = countries["code"].tolist()
    name_of = dict(zip(countries["code"], countries["name"]))

    def fmt(code: str) -> str:
        return f"{name_of.get(code, code)} ({code})"

    st.sidebar.header("1. Countries")
    default1 = codes.index("USA") if "USA" in codes else 0
    default2 = codes.index("JPN") if "JPN" in codes else min(1, len(codes) - 1)
    c1 = st.sidebar.selectbox("Country 1 (numerator CPI, base currency)", codes, index=default1, format_func=fmt)
    c2 = st.sidebar.selectbox("Country 2 (denominator CPI, quote currency)", codes, index=default2, format_func=fmt)
    name1, name2 = name_of.get(c1, c1), name_of.get(c2, c2)
    st.sidebar.caption(f"S(t) = {name2} currency per 1 unit of {name1} currency.")

    if c1 == c2:
        st.warning(
            "Country 1 and Country 2 are the same. The bilateral real exchange rate is then "
            "identically 1 (Real Index = 100). Please choose two different countries."
        )
        st.stop()

    # ---- Data download ---------------------------------------------------
    try:
        with st.spinner(f"Downloading IMF data for {name1} and {name2}…"):
            cpi1, cpi1_key = fetch_cpi(c1)
            cpi2, cpi2_key = fetch_cpi(c2)
            er1, er1_key = fetch_er(c1)
            er2, er2_key = fetch_er(c2)
    except IMFError as exc:
        st.error(f"The IMF API could not be reached or returned an error. Please try again later.\n\n{exc}")
        st.stop()

    missing = [
        label
        for label, s in [
            (f"CPI for {name1} (`{cpi1_key}`)", cpi1),
            (f"CPI for {name2} (`{cpi2_key}`)", cpi2),
            (f"exchange rate for {name1} (`{er1_key}`)", er1),
            (f"exchange rate for {name2} (`{er2_key}`)", er2),
        ]
        if s.empty
    ]
    if missing:
        st.error(
            "The IMF does not provide the following monthly series from "
            f"{START_PERIOD} onwards:\n\n- " + "\n- ".join(missing)
            + "\n\nPlease choose a different country."
        )
        st.stop()

    frame = build_rer_frame(cpi1, cpi2, er1, er2)
    if frame.empty:
        st.error(
            f"There is no month in which CPI and exchange-rate data are available for both "
            f"{name1} and {name2}, so the RER cannot be computed. Please choose different countries."
        )
        st.stop()

    # ---- Sample period ----------------------------------------------------
    months = list(frame.index)
    st.sidebar.header("2. Sample period")
    start = st.sidebar.selectbox("Start month", months, index=0, format_func=month_label)
    end = st.sidebar.selectbox("End month", months, index=len(months) - 1, format_func=month_label)
    st.sidebar.caption(
        f"Valid months for this pair: {month_label(months[0])} – {month_label(months[-1])}."
    )

    st.sidebar.header("3. Chart")
    view = st.sidebar.radio("Series to plot", ["Real", "Nominal", "Both"], index=2, horizontal=True)

    if start > end:
        st.error("The start month is after the end month. Please choose a start month on or before the end month.")
        st.stop()

    sample, rer_star = apply_sample(frame, start, end)
    if sample.empty or not pd.notna(rer_star) or rer_star <= 0:
        st.error("No valid observations in the selected sample period. Please widen the date range.")
        st.stop()

    # ---- Coverage notes -----------------------------------------------------
    full_range = pd.date_range(start, end, freq="MS")
    n_gaps = len(full_range) - len(sample)
    if n_gaps > 0:
        st.info(
            f"{n_gaps} month(s) between {month_label(start)} and {month_label(end)} are excluded "
            "because at least one of the four IMF series is missing for that month. "
            "RER* and the Real Index use only the valid months."
        )
    for code, nm in [(c1, name1), (c2, name2)]:
        if code in EURO_MEMBERS and frame.index.max() < pd.Timestamp("1999-12-01"):
            st.info(
                f"{nm} adopted the euro in or after 1999; the IMF's national-currency-per-USD series "
                "for euro-area members stops when the legacy currency was replaced. For recent data "
                "select **Euro Area (EA)** instead."
            )

    # ---- Headline metrics -------------------------------------------------
    last = sample.iloc[-1]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Valid months in sample", f"{len(sample)}")
    m2.metric("RER* (sample mean of RER)", f"{rer_star:,.6g}")
    m3.metric(f"Real Index, {month_label(sample.index[-1])}", f"{last['REAL_INDEX']:.2f}")
    m4.metric(f"S(t), {month_label(sample.index[-1])}", f"{last['S']:,.6g}",
              help=f"{name2} currency per 1 unit of {name1} currency")

    # ---- Chart ---------------------------------------------------------------
    st.plotly_chart(make_chart(sample, view, name1, name2), width="stretch")

    # ---- Table + download -------------------------------------------------
    st.subheader("Observations and calculations for the selected sample")
    table = build_output_table(sample, name1, name2)
    number_cols = [c for c in table.columns if c not in ("Date", "Country 1", "Country 2")]
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        column_config={c: st.column_config.NumberColumn(c, format="%.6g") for c in number_cols},
    )
    st.download_button(
        label="Download these observations as CSV",
        data=table.to_csv(index=False).encode("utf-8"),
        file_name=f"RER_{c1}_{c2}_{start:%Y-%m}_to_{end:%Y-%m}.csv",
        mime="text/csv",
    )

    with st.expander("Data sources and coverage"):
        st.markdown(
            f"""
| Series | IMF dataflow | Key | Coverage (from {START_PERIOD}) |
|---|---|---|---|
| CPI 1 ({name1}) | `{AGENCY}/{CPI_FLOW}` | `{cpi1_key}` | {describe_coverage(cpi1)} |
| CPI 2 ({name2}) | `{AGENCY}/{CPI_FLOW}` | `{cpi2_key}` | {describe_coverage(cpi2)} |
| ER 1 ({name1}, per USD) | `{AGENCY}/{ER_FLOW}` | `{er1_key}` | {describe_coverage(er1)} |
| ER 2 ({name2}, per USD) | `{AGENCY}/{ER_FLOW}` | `{er2_key}` | {describe_coverage(er2)} |

Source: IMF SDMX 3.0 API, `{API_BASE}`. CPI levels have arbitrary base years; the base year cancels
out in the Real Index because it only rescales RER(t) by a constant.
"""
        )


if __name__ == "__main__":
    main()

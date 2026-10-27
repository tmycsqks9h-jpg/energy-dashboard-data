#!/usr/bin/env python3
"""Fetch public, key-free market data for the North American energy markets dashboard.

Sources
- EIA petroleum and natural gas history workbooks (https://www.eia.gov/dnav/...): daily spot prices,
  weekly supply statistics, monthly rig counts. Public domain, no API key.
- Alberta Economic Dashboard API (https://api.economicdata.alberta.ca): monthly WCS, WTI, differential
  and the Alberta natural gas reference price. Open Government Licence - Alberta.
- Yahoo Finance via the yfinance package: adjusted closes for refiner, E&P and Canadian integrated
  equities and benchmark ETFs, used only to compute equal-weight performance baskets and return tables.

Outputs (relative to this directory)
- data/series.json    compact series bundle embedded into the dashboard page
- data/manifest.json  provenance: source, series id, URL, units, frequency, first/last observation,
                      fetch time, freshness status, SHA-256 of each raw download
- cache/              raw downloads, reused when younger than --max-age-hours or when a source fails

Usage: python3 fetch_data.py [--offline] [--max-age-hours N] [--skip-equities]
Requires pandas plus python-calamine or xlrd (for the EIA .xls files) and yfinance (equities).
"""
from __future__ import annotations
import argparse, datetime as dt, hashlib, io, json, math, sys, time, urllib.request
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / 'cache'
DATA = ROOT / 'data'
UA = 'energy-research-dashboard/1.0 (public research site; contact via energy-research.mv34214.chatgpt.site)'
EIA_PET = 'https://www.eia.gov/dnav/pet/hist_xls/{id}{f}.xls'
EIA_NG = 'https://www.eia.gov/dnav/ng/hist_xls/{id}{f}.xls'
EIA_PAGE = 'https://www.eia.gov/dnav/{cat}/hist/{id}{f}.htm'
ALBERTA = 'https://api.economicdata.alberta.ca/data?table={table}'
FRED = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}'
BOC = 'https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?start_date=2016-01-01'
DAILY_START, WEEKLY_START, MONTHLY_START, EQUITY_START, BASKET_START = '2016-01-01', '2008-01-01', '1990-01-01', '2016-01-01', '2019-01-01'

# key, EIA category, EIA series id (without frequency suffix), frequency letter, label, unit, decimals, group
EIA = [
    ('wti', 'pet', 'RWTC', 'd', 'WTI crude, Cushing OK spot', '$/bbl', 2, 'crude'),
    ('brent', 'pet', 'RBRTE', 'd', 'Brent crude, Europe spot', '$/bbl', 2, 'crude'),
    ('gc_gasoline', 'pet', 'EER_EPMRU_PF4_RGC_DPG', 'd', 'Gulf Coast conventional gasoline spot', '$/gal', 3, 'products'),
    ('gc_ulsd', 'pet', 'EER_EPD2DXL0_PF4_RGC_DPG', 'd', 'Gulf Coast ULSD spot', '$/gal', 3, 'products'),
    ('gc_jet', 'pet', 'EER_EPJK_PF4_RGC_DPG', 'd', 'Gulf Coast jet fuel spot', '$/gal', 3, 'products'),
    ('nyh_gasoline', 'pet', 'EER_EPMRU_PF4_Y35NY_DPG', 'd', 'New York Harbor conventional gasoline spot', '$/gal', 3, 'products'),
    ('nyh_ulsd', 'pet', 'EER_EPD2DXL0_PF4_Y35NY_DPG', 'd', 'New York Harbor ULSD spot', '$/gal', 3, 'products'),
    ('nyh_heating_oil', 'pet', 'EER_EPD2F_PF4_Y35NY_DPG', 'd', 'New York Harbor No. 2 heating oil spot', '$/gal', 3, 'products'),
    ('la_rbob', 'pet', 'EER_EPMRR_PF4_Y05LA_DPG', 'd', 'Los Angeles reformulated RBOB gasoline spot', '$/gal', 3, 'products'),
    ('la_carb_diesel', 'pet', 'EER_EPD2DC_PF4_Y05LA_DPG', 'd', 'Los Angeles CARB ULSD spot', '$/gal', 3, 'products'),
    ('mb_propane', 'pet', 'EER_EPLLPA_PF4_Y44MB_DPG', 'd', 'Mont Belvieu propane spot', '$/gal', 3, 'ngl'),
    ('henry_hub', 'ng', 'RNGWHHD', 'd', 'Henry Hub natural gas spot', '$/MMBtu', 2, 'gas'),
    ('util_us', 'pet', 'WPULEUS3', 'w', 'US refinery utilization', '%', 1, 'refining'),
    ('util_padd3', 'pet', 'W_NA_YUP_R30_PER', 'w', 'Gulf Coast (PADD 3) refinery utilization', '%', 1, 'refining'),
    ('crude_inputs', 'pet', 'WCRRIUS2', 'w', 'US refiner net crude inputs', 'kb/d', 0, 'refining'),
    ('crude_inputs_padd3', 'pet', 'WCRRIP32', 'w', 'Gulf Coast (PADD 3) refiner net crude inputs', 'kb/d', 0, 'refining'),
    ('gross_inputs', 'pet', 'WGIRIUS2', 'w', 'US gross refinery inputs', 'kb/d', 0, 'refining'),
    ('operable_capacity', 'pet', 'WOCLEUS2', 'w', 'US operable crude distillation capacity', 'kb/cd', 0, 'refining'),
    ('gasoline_production', 'pet', 'WGFRPUS2', 'w', 'US finished motor gasoline production', 'kb/d', 0, 'refining'),
    ('distillate_production', 'pet', 'WDIRPUS2', 'w', 'US distillate production', 'kb/d', 0, 'refining'),
    ('crude_stocks_ex_spr', 'pet', 'WCESTUS1', 'w', 'US commercial crude stocks (excluding SPR)', 'kbbl', 0, 'stocks'),
    ('crude_stocks_total', 'pet', 'WCRSTUS1', 'w', 'US crude stocks including SPR', 'kbbl', 0, 'stocks'),
    ('spr', 'pet', 'WCSSTUS1', 'w', 'Strategic Petroleum Reserve', 'kbbl', 0, 'stocks'),
    ('cushing_stocks', 'pet', 'W_EPC0_SAX_YCUOK_MBBL', 'w', 'Cushing OK crude stocks', 'kbbl', 0, 'stocks'),
    ('gasoline_stocks', 'pet', 'WGTSTUS1', 'w', 'US total motor gasoline stocks', 'kbbl', 0, 'stocks'),
    ('distillate_stocks', 'pet', 'WDISTUS1', 'w', 'US distillate stocks', 'kbbl', 0, 'stocks'),
    ('jet_stocks', 'pet', 'WKJSTUS1', 'w', 'US jet fuel stocks', 'kbbl', 0, 'stocks'),
    ('total_stocks', 'pet', 'WTTSTUS1', 'w', 'US crude and product stocks', 'kbbl', 0, 'stocks'),
    ('gasoline_supplied', 'pet', 'WGFUPUS2', 'w', 'US gasoline product supplied', 'kb/d', 0, 'demand'),
    ('distillate_supplied', 'pet', 'WDIUPUS2', 'w', 'US distillate product supplied', 'kb/d', 0, 'demand'),
    ('jet_supplied', 'pet', 'WKJUPUS2', 'w', 'US jet fuel product supplied', 'kb/d', 0, 'demand'),
    ('products_supplied', 'pet', 'WRPUPUS2', 'w', 'US total petroleum products supplied', 'kb/d', 0, 'demand'),
    ('crude_production', 'pet', 'WCRFPUS2', 'w', 'US crude oil production', 'kb/d', 0, 'upstream'),
    ('crude_imports', 'pet', 'WCRIMUS2', 'w', 'US crude imports', 'kb/d', 0, 'trade'),
    ('crude_exports', 'pet', 'WCREXUS2', 'w', 'US crude exports', 'kb/d', 0, 'trade'),
    ('product_exports', 'pet', 'WRPEXUS2', 'w', 'US petroleum product exports', 'kb/d', 0, 'trade'),
    ('product_imports', 'pet', 'WRPIMUS2', 'w', 'US petroleum product imports', 'kb/d', 0, 'trade'),
    ('gasoline_exports', 'pet', 'W_EPM0F_EEX_NUS-Z00_MBBLD', 'w', 'US total motor gasoline exports', 'kb/d', 0, 'trade'),
    ('distillate_exports', 'pet', 'WDIEXUS2', 'w', 'US distillate exports', 'kb/d', 0, 'trade'),
    ('jet_exports', 'pet', 'WKJEXUS2', 'w', 'US jet fuel exports', 'kb/d', 0, 'trade'),
    ('retail_gasoline', 'pet', 'EMM_EPMR_PTE_NUS_DPG', 'w', 'US regular gasoline retail price', '$/gal', 3, 'retail'),
    ('retail_diesel', 'pet', 'EMD_EPD2D_PTE_NUS_DPG', 'w', 'US on-highway diesel retail price', '$/gal', 3, 'retail'),
    ('gas_storage', 'ng', 'NW2_EPG0_SWO_R48_BCF', 'w', 'Lower 48 working gas in storage', 'Bcf', 0, 'gas'),
    ('rigs_us', 'pet', 'E_ERTRR0_XR0_NUS_C', 'm', 'US rotary rigs in operation (EIA monthly)', 'rigs', 0, 'upstream'),
]
TICKERS = {
    'refiners': ['VLO', 'MPC', 'PSX', 'PBF', 'DINO', 'DK', 'CVI', 'PARR'],
    'eandp': ['COP', 'EOG', 'DVN', 'OXY', 'FANG', 'APA', 'CTRA', 'OVV', 'PR', 'CHRD'],
    'canada': ['SU', 'CVE', 'IMO', 'CNQ'],
    'benchmarks': ['XLE', 'XOP', 'CRAK', 'SPY'],
}
BASKET_LABELS = {'refiners': 'US independent refiners (equal weight)', 'eandp': 'US E&Ps (equal weight)', 'canada': 'Canadian large-cap oil producers (equal weight)'}
EPOCH = dt.date(1970, 1, 1)

def days(d: dt.date) -> int:
    return (d - EPOCH).days

def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def fetch(url: str, name: str, max_age_h: float, offline: bool, log: dict, timeout: int = 60, validate=None) -> bytes | None:
    """Download with cache. `validate(bytes)` must raise on malformed content; nothing is promoted to the cache until it passes,
    so a provider error page can never replace the last good copy. Returns bytes or None and records freshness in log."""
    CACHE.mkdir(exist_ok=True)
    raw = CACHE / name
    meta = CACHE / (name + '.json')
    cached = json.loads(meta.read_text()) if meta.exists() and raw.exists() else None
    age_h = (time.time() - cached['fetched_at_epoch']) / 3600 if cached else None
    if cached and (offline or age_h <= max_age_h):
        log.update(status='cached', fetched_at=cached['fetched_at'], sha256=cached['sha256'], age_hours=round(age_h, 1))
        return raw.read_bytes()
    if offline:
        log.update(status='missing', error='offline and no cache'); return None
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read()
        if len(b) < 500:
            raise ValueError(f'response too small ({len(b)} bytes)')
        if validate is not None:
            validate(b)
        raw.write_bytes(b)
        now = dt.datetime.now(dt.timezone.utc)
        meta.write_text(json.dumps({'url': url, 'fetched_at': now.isoformat(timespec='seconds'), 'fetched_at_epoch': now.timestamp(), 'sha256': sha(b), 'bytes': len(b)}))
        log.update(status='fresh', fetched_at=now.isoformat(timespec='seconds'), sha256=sha(b), age_hours=0.0)
        return b
    except Exception as e:  # download or validation failed: keep last-good data, flag staleness
        if cached:
            log.update(status='stale', fetched_at=cached['fetched_at'], sha256=cached['sha256'], age_hours=round(age_h, 1), error=f'{type(e).__name__}: {e}')
            return raw.read_bytes()
        log.update(status='failed', error=f'{type(e).__name__}: {e}'); return None

def read_eia(b: bytes) -> pd.Series:
    """Parse an EIA history workbook; raises on anything that is not a workbook with a dated 'Data 1' sheet."""
    if b[:4] not in (b'\xd0\xcf\x11\xe0', b'PK\x03\x04'):
        raise ValueError('not an Excel workbook (HTML or error page?)')
    bio = io.BytesIO(b)
    try:
        df = pd.read_excel(bio, sheet_name='Data 1', skiprows=2, engine='calamine')
    except Exception:
        bio.seek(0); df = pd.read_excel(bio, sheet_name='Data 1', skiprows=2, engine='xlrd')
    s = pd.Series(pd.to_numeric(df.iloc[:, 1], errors='coerce').values, index=pd.to_datetime(df.iloc[:, 0]), name=str(df.columns[1])).dropna()
    if len(s) < 10:
        raise ValueError('workbook has fewer than 10 dated observations')
    return s[~s.index.duplicated(keep='last')].sort_index()

def validate_alberta(b: bytes) -> None:
    rows = json.loads(b)
    if not isinstance(rows, list) or len(rows) < 100 or 'Value' not in rows[0] or 'Date' not in rows[0]:
        raise ValueError('unexpected Alberta API payload')

def cut_splices(px: pd.DataFrame, threshold: float = 1.0) -> tuple[pd.DataFrame, dict]:
    """A one-day move beyond `threshold` (100%) in a vendor price history is a restructuring splice (old equity cancelled, new
    equity listed), not a shareholder return. History up to and including the splice is excluded and the event is reported
    for review in the manifest. Known case: Oasis Petroleum / Chord Energy (CHRD), new common stock from 20 Nov 2020."""
    px = px.copy(); splices = {}
    for t in px.columns:
        breaks = px[t].pct_change().abs() > threshold
        if breaks.any():
            cut = px.index[breaks][-1]; px.loc[:cut, t] = float('nan'); splices[t] = str(cut.date())
    return px, splices

def pack(s: pd.Series, start: str, decimals: int, freq: str) -> dict:
    """Regular-cadence encoding: business days ('b'), 7-day steps ('w') or calendar months ('m') from `start`, null for gaps."""
    s = s[s.index >= pd.Timestamp(start)].dropna()
    conv = (lambda v: None if pd.isna(v) else round(float(v), decimals)) if decimals else (lambda v: None if pd.isna(v) else int(round(float(v))))
    if s.empty: return {'cad': 'b', 'start': 0, 'v': []}
    if freq == 'd':
        idx = pd.bdate_range(s.index[0], s.index[-1]); s2 = s.reindex(idx); start_code = days(idx[0].date())
    elif freq == 'w':
        idx = pd.date_range(s.index[0], s.index[-1], freq='7D'); s2 = s.reindex(idx); start_code = days(idx[0].date())
    else:
        s2 = s.copy(); s2.index = s2.index.to_period('M').to_timestamp(); s2 = s2[~s2.index.duplicated(keep='last')]
        idx = pd.date_range(s2.index[0], s2.index[-1], freq='MS'); s2 = s2.reindex(idx); start_code = idx[0].year * 100 + idx[0].month
    dropped = int(len(s) - s2.notna().sum())
    if dropped: print(f'     note: {dropped} observations off-cadence dropped from a {len(s)}-point series', file=sys.stderr)
    return {'cad': {'d': 'b', 'w': 'w', 'm': 'm'}[freq], 'start': start_code, 'v': [conv(v) for v in s2.values]}

def seasonal(s: pd.Series, decimals: int, years: int = 5) -> dict | None:
    """Week-of-year table: current and prior year lines plus the min/max/avg band of the prior `years` full years."""
    if s.empty: return None
    if (s.index.to_series().diff().dt.days.median() or 7) < 6:
        last_daily = s.index[-1]; s = s.resample('W-FRI').mean().dropna()
        if last_daily < s.index[-1]: s = s.iloc[:-1]  # the current week is incomplete
    last_year = s.index[-1].year
    by_week = {}
    for ts, v in s.items():
        w = min((ts.dayofyear - 1) // 7, 52)
        by_week.setdefault(ts.year, {})[w] = float(v)
    band_years = [y for y in range(last_year - years, last_year) if y in by_week]
    if len(band_years) < 3: return None
    rows = []
    for w in range(53):
        vals = [by_week[y][w] for y in band_years if w in by_week[y]]
        rows.append((min(vals), max(vals), sum(vals) / len(vals)) if len(vals) >= 3 else (None, None, None))
    r = lambda x: None if x is None else round(x, max(decimals, 1))
    line = lambda y: [r(by_week.get(y, {}).get(w)) for w in range(53)]
    return {'band_years': f'{band_years[0]}–{band_years[-1]}', 'min': [r(a) for a, _, _ in rows], 'max': [r(b) for _, b, _ in rows], 'avg': [r(c) for _, _, c in rows],
            'current_year': last_year, 'current': line(last_year), 'prior_year': last_year - 1, 'prior': line(last_year - 1)}

def align(*series: pd.Series) -> pd.DataFrame:
    return pd.concat(series, axis=1, join='inner').dropna()

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--offline', action='store_true'); ap.add_argument('--max-age-hours', type=float, default=6); ap.add_argument('--skip-equities', action='store_true')
    a = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    out, manifest, raw = {}, [], {}
    prev = json.loads((DATA / 'series.json').read_text()) if (DATA / 'series.json').exists() else {}
    for key, cat, sid, f, label, unit, dec, group in EIA:
        url = (EIA_PET if cat == 'pet' else EIA_NG).format(id=sid, f=f)
        log = {'key': key, 'source': 'EIA', 'series_id': sid, 'frequency': {'d': 'daily', 'w': 'weekly', 'm': 'monthly'}[f], 'url': url, 'page': EIA_PAGE.format(cat=cat, id=sid, f=f), 'label': label, 'unit': unit}
        b = fetch(url, f'eia_{sid}{f}.xls', a.max_age_hours, a.offline, log, validate=read_eia)
        if b is None:
            manifest.append(log); print(f'  !! {key}: {log["error"]}', file=sys.stderr); continue
        try:
            s = read_eia(b)
        except Exception as e:
            log.update(status='failed', error=f'parse: {e}'); manifest.append(log); continue
        raw[key] = s
        start = {'d': DAILY_START, 'w': WEEKLY_START, 'm': MONTHLY_START}[f]
        out[key] = {'label': label, 'unit': unit, 'freq': f, 'group': group, 'source': 'EIA', 'id': sid, **pack(s, start, dec, f)}
        log.update(eia_name=s.name, first_obs=str(s.index[0].date()), last_obs=str(s.index[-1].date()), observations=int(len(s)))
        manifest.append(log); print(f'  ok {key:24s} {log["status"]:7s} last {log["last_obs"]} = {s.iloc[-1]}')

    # Alberta monthly WCS / WTI / differential and natural gas reference price
    for table, keys in (('OilPrices', {'WCS': ('wcs', 'Western Canadian Select, monthly average', '$/bbl'), 'WTI': ('wti_monthly_alberta', 'WTI, monthly average (Alberta source)', '$/bbl')}),
                        ('NaturalGasPrices', {'NatGas': ('alberta_gas_reference', 'Alberta natural gas reference price', 'C$/GJ')})):
        url = ALBERTA.format(table=table)
        log = {'source': 'Alberta Economic Dashboard', 'series_id': table, 'frequency': 'monthly', 'url': url, 'page': 'https://economicdashboard.alberta.ca/OilPrice' if table == 'OilPrices' else 'https://economicdashboard.alberta.ca/NaturalGasPrice'}
        fl = dict(log); b = fetch(url, f'alberta_{table}.json', a.max_age_hours, a.offline, fl, validate=validate_alberta)
        rows = json.loads(b) if b else []
        for typ, (key, label, unit) in keys.items():
            l = dict(log, key=key, label=label, unit=unit)
            recs = [(r['Date'], r['Value']) for r in rows if str(next((v for k, v in r.items() if k.strip() == 'Type'), '')).strip() == typ and r.get('Value') is not None]
            if not recs:
                l.update(status='failed', error='no rows'); manifest.append(l); continue
            s = pd.Series([float(v) for _, v in recs], index=pd.to_datetime([d for d, _ in recs])).sort_index()
            raw[key] = s
            out[key] = {'label': label, 'unit': unit, 'freq': 'm', 'group': 'crude' if typ != 'NatGas' else 'gas', 'source': 'Alberta', 'id': f'{table}/{typ}', **pack(s, MONTHLY_START, 2, 'm')}
            l.update({k: fl[k] for k in ('status', 'fetched_at', 'sha256', 'age_hours', 'error') if k in fl}, first_obs=str(s.index[0].date()), last_obs=str(s.index[-1].date()), observations=int(len(s)))
            manifest.append(l); print(f'  ok {key:24s} last {l["last_obs"]} = {s.iloc[-1]}')

    # Canadian dollars per US dollar, used only to express the Alberta gas price in US$/MMBtu. Bank of Canada first, FRED as fallback.
    fx_log = {'key': 'cad_usd', 'source': 'Bank of Canada (Valet API)', 'series_id': 'FXUSDCAD', 'frequency': 'daily', 'url': BOC, 'page': 'https://www.bankofcanada.ca/rates/exchange/daily-exchange-rates/', 'label': 'Canadian dollars per US dollar', 'unit': 'C$/US$'}
    s = None
    b = fetch(BOC, 'boc_FXUSDCAD.json', a.max_age_hours, a.offline, fx_log, timeout=30, validate=lambda b: float(json.loads(b)['observations'][-1]['FXUSDCAD']['v']))
    if b is not None:
        try:
            obs = json.loads(b)['observations']; s = pd.Series([float(o['FXUSDCAD']['v']) for o in obs if o.get('FXUSDCAD', {}).get('v')], index=pd.to_datetime([o['d'] for o in obs if o.get('FXUSDCAD', {}).get('v')])).sort_index()
        except Exception as e:
            fx_log.update(status='failed', error=f'parse: {e}'); s = None
    if s is None or s.empty:
        url = FRED.format(id='DEXCAUS'); fx_log = {'key': 'cad_usd', 'source': 'FRED (Federal Reserve Board H.10)', 'series_id': 'DEXCAUS', 'frequency': 'daily', 'url': url, 'page': 'https://fred.stlouisfed.org/series/DEXCAUS', 'label': 'Canadian dollars per US dollar', 'unit': 'C$/US$', 'note': 'Bank of Canada unavailable: ' + str(fx_log.get('error'))}
        b = fetch(url, 'fred_DEXCAUS.csv', a.max_age_hours, a.offline, fx_log, timeout=30, validate=lambda b: pd.read_csv(io.BytesIO(b)).iloc[100, 1])
        if b is not None:
            try:
                df = pd.read_csv(io.BytesIO(b)); s = pd.Series(pd.to_numeric(df.iloc[:, 1], errors='coerce').values, index=pd.to_datetime(df.iloc[:, 0])).dropna()
            except Exception as e:
                fx_log.update(status='failed', error=f'parse: {e}'); s = None
    if s is not None and not s.empty:
        raw['cad_usd'] = s; out['cad_usd'] = {'label': fx_log['label'], 'unit': 'C$/US$', 'freq': 'd', 'group': 'fx', 'source': fx_log['source'], 'id': fx_log['series_id'], **pack(s, DAILY_START, 4, 'd')}
        fx_log.update(first_obs=str(s.index[0].date()), last_obs=str(s.index[-1].date()), observations=int(len(s))); print(f'  ok cad_usd                  {fx_log["status"]:7s} last {fx_log["last_obs"]} = {s.iloc[-1]}')
    manifest.append(fx_log)

    # Derived series (all in $/bbl unless noted)
    def put(key, s, label, unit, dec, group, formula, start=DAILY_START, freq='d'):
        s = s.dropna()
        if s.empty: return
        raw[key] = s
        out[key] = {'label': label, 'unit': unit, 'freq': freq, 'group': group, 'source': 'derived', 'id': formula, **pack(s, start, dec, freq)}
        manifest.append({'key': key, 'source': 'derived', 'series_id': formula, 'label': label, 'unit': unit, 'frequency': {'d': 'daily', 'w': 'weekly', 'm': 'monthly'}[freq], 'first_obs': str(s.index[0].date()), 'last_obs': str(s.index[-1].date()), 'observations': int(len(s)), 'status': 'derived'})
    r = raw
    if {'wti', 'brent'} <= r.keys():
        d = align(r['brent'].rename('b'), r['wti'].rename('w')); put('brent_wti', d.b - d.w, 'Brent minus WTI', '$/bbl', 2, 'crude', 'brent − wti')
    if {'wcs', 'wti_monthly_alberta'} <= r.keys():
        d = align(r['wcs'].rename('c'), r['wti_monthly_alberta'].rename('w')); put('wcs_wti', d.c - d.w, 'WCS minus WTI, monthly average', '$/bbl', 2, 'crude', 'wcs − wti (Alberta monthly)', MONTHLY_START, 'm')
    if {'gc_gasoline', 'gc_ulsd', 'wti'} <= r.keys():
        d = align(r['gc_gasoline'].rename('g'), r['gc_ulsd'].rename('u'), r['wti'].rename('w'))
        put('crack_gc_321', (2 * d.g * 42 + d.u * 42) / 3 - d.w, 'Gulf Coast 3-2-1 crack spread vs WTI', '$/bbl', 2, 'cracks', '(2 × gc_gasoline + gc_ulsd) × 42 ÷ 3 − wti')
        put('crack_gc_211', (d.g * 42 + d.u * 42) / 2 - d.w, 'Gulf Coast 2-1-1 crack spread vs WTI', '$/bbl', 2, 'cracks', '(gc_gasoline + gc_ulsd) × 42 ÷ 2 − wti')
        put('crack_gc_gasoline', d.g * 42 - d.w, 'Gulf Coast gasoline crack vs WTI', '$/bbl', 2, 'cracks', 'gc_gasoline × 42 − wti')
        put('crack_gc_ulsd', d.u * 42 - d.w, 'Gulf Coast ULSD crack vs WTI', '$/bbl', 2, 'cracks', 'gc_ulsd × 42 − wti')
    if {'gc_jet', 'wti'} <= r.keys():
        d = align(r['gc_jet'].rename('j'), r['wti'].rename('w')); put('crack_gc_jet', d.j * 42 - d.w, 'Gulf Coast jet crack vs WTI', '$/bbl', 2, 'cracks', 'gc_jet × 42 − wti')
    if {'nyh_gasoline', 'nyh_ulsd', 'brent'} <= r.keys():
        d = align(r['nyh_gasoline'].rename('g'), r['nyh_ulsd'].rename('u'), r['brent'].rename('b'))
        put('crack_nyh_321_brent', (2 * d.g * 42 + d.u * 42) / 3 - d.b, 'New York Harbor 3-2-1 crack spread vs Brent', '$/bbl', 2, 'cracks', '(2 × nyh_gasoline + nyh_ulsd) × 42 ÷ 3 − brent')
    if {'la_rbob', 'la_carb_diesel', 'wti'} <= r.keys():
        d = align(r['la_rbob'].rename('g'), r['la_carb_diesel'].rename('u'), r['wti'].rename('w'))
        put('crack_la_321', (2 * d.g * 42 + d.u * 42) / 3 - d.w, 'Los Angeles 3-2-1 crack spread vs WTI', '$/bbl', 2, 'cracks', '(2 × la_rbob + la_carb_diesel) × 42 ÷ 3 − wti')
    if {'nyh_gasoline', 'gc_gasoline'} <= r.keys():
        d = align(r['nyh_gasoline'].rename('n'), r['gc_gasoline'].rename('g')); put('nyh_gc_gasoline_premium', (d.n - d.g) * 42, 'New York Harbor gasoline premium over Gulf Coast', '$/bbl', 2, 'products', '(nyh_gasoline − gc_gasoline) × 42')
    if {'nyh_ulsd', 'gc_ulsd'} <= r.keys():
        d = align(r['nyh_ulsd'].rename('n'), r['gc_ulsd'].rename('g')); put('nyh_gc_diesel_premium', (d.n - d.g) * 42, 'New York Harbor ULSD premium over Gulf Coast', '$/bbl', 2, 'products', '(nyh_ulsd − gc_ulsd) × 42')
    if {'la_rbob', 'gc_gasoline'} <= r.keys():
        d = align(r['la_rbob'].rename('n'), r['gc_gasoline'].rename('g')); put('la_gc_gasoline_premium', (d.n - d.g) * 42, 'Los Angeles RBOB premium over Gulf Coast gasoline', '$/bbl', 2, 'products', '(la_rbob − gc_gasoline) × 42')
    if {'alberta_gas_reference', 'cad_usd', 'henry_hub'} <= r.keys():
        fx = r['cad_usd'].resample('MS').mean(); hh = r['henry_hub'].resample('MS').mean()
        if hh.index[-1].to_period('M') == pd.Timestamp(dt.date.today()).to_period('M'): hh = hh.iloc[:-1]  # month to date is not a monthly average
        ab = r['alberta_gas_reference'].copy(); ab.index = ab.index.to_period('M').to_timestamp()
        d = align(ab.rename('a'), fx.rename('f')); put('alberta_gas_usd', d.a * 1.055056 / d.f, 'Alberta natural gas reference price in US$/MMBtu', '$/MMBtu', 2, 'gas', 'alberta_gas_reference (C$/GJ) × 1.055056 GJ/MMBtu ÷ monthly mean cad_usd', MONTHLY_START, 'm')
        put('henry_hub_monthly', hh, 'Henry Hub spot, monthly average', '$/MMBtu', 2, 'gas', 'henry_hub monthly mean', MONTHLY_START, 'm')
    if {'gc_ulsd', 'gc_gasoline'} <= r.keys():
        d = align(r['gc_ulsd'].rename('u'), r['gc_gasoline'].rename('g')); put('ulsd_gasoline_spread', (d.u - d.g) * 42, 'Gulf Coast ULSD minus gasoline', '$/bbl', 2, 'products', '(gc_ulsd − gc_gasoline) × 42')
    if {'retail_gasoline', 'gc_gasoline'} <= r.keys():
        spot = r['gc_gasoline'].resample('W-MON').last().dropna()
        d = align(r['retail_gasoline'].rename('r'), spot.rename('s')); put('retail_gasoline_margin', d.r - d.s, 'Retail regular gasoline minus Gulf Coast spot (includes taxes, transport and retail margin)', '$/gal', 3, 'retail', 'retail_gasoline − gc_gasoline (same Monday)', WEEKLY_START, 'w')
    if {'retail_diesel', 'gc_ulsd'} <= r.keys():
        spot = r['gc_ulsd'].resample('W-MON').last().dropna()
        d = align(r['retail_diesel'].rename('r'), spot.rename('s')); put('retail_diesel_margin', d.r - d.s, 'Retail diesel minus Gulf Coast ULSD spot (includes taxes, transport and retail margin)', '$/gal', 3, 'retail', 'retail_diesel − gc_ulsd (same Monday)', WEEKLY_START, 'w')
    if {'crude_stocks_ex_spr', 'crude_inputs'} <= r.keys():
        d = align(r['crude_stocks_ex_spr'].rename('s'), r['crude_inputs'].rename('i')); put('crude_days_supply', d.s / d.i, 'Commercial crude stocks, days of refinery input', 'days', 1, 'stocks', 'crude_stocks_ex_spr ÷ crude_inputs', WEEKLY_START, 'w')
    if {'gasoline_stocks', 'gasoline_supplied'} <= r.keys():
        d = align(r['gasoline_stocks'].rename('s'), r['gasoline_supplied'].rename('i')); put('gasoline_days_supply', d.s / d.i, 'Gasoline stocks, days of supply', 'days', 1, 'stocks', 'gasoline_stocks ÷ gasoline_supplied', WEEKLY_START, 'w')
    if {'distillate_stocks', 'distillate_supplied'} <= r.keys():
        d = align(r['distillate_stocks'].rename('s'), r['distillate_supplied'].rename('i')); put('distillate_days_supply', d.s / d.i, 'Distillate stocks, days of supply', 'days', 1, 'stocks', 'distillate_stocks ÷ distillate_supplied', WEEKLY_START, 'w')
    for k in ('gasoline_supplied', 'distillate_supplied', 'jet_supplied', 'products_supplied'):
        if k in r: put(k + '_4wk', r[k].rolling(4).mean(), out[k]['label'] + ', 4-week average', 'kb/d', 0, 'demand', f'{k} rolling 4-week mean', WEEKLY_START, 'w')

    # Seasonal tables (week-of-year, 5-year band)
    seas = {}
    for k in ('crude_stocks_ex_spr', 'cushing_stocks', 'gasoline_stocks', 'distillate_stocks', 'jet_stocks', 'total_stocks', 'util_us', 'util_padd3', 'crude_inputs', 'gasoline_supplied_4wk', 'distillate_supplied_4wk', 'jet_supplied_4wk', 'products_supplied_4wk', 'gas_storage', 'crack_gc_321', 'crack_gc_gasoline', 'crack_gc_ulsd', 'crack_gc_jet', 'crude_production', 'crude_exports', 'product_exports', 'crude_days_supply', 'gasoline_days_supply', 'distillate_days_supply'):
        if k in r:
            t = seasonal(r[k], 2 if out[k]['unit'].startswith('$') else (1 if out[k]['unit'] in ('%', 'days') else 0))
            if t: seas[k] = t

    # Equities (Yahoo Finance via yfinance). The full-precision daily matrix is kept in cache/ for replay and is never
    # published; the page receives only indexed levels and percentage returns.
    equities = None
    eq_log = {'key': 'equities', 'source': 'Yahoo Finance via yfinance', 'series_id': '', 'frequency': 'daily (weekly for members)', 'url': 'https://finance.yahoo.com', 'label': 'Equity prices for performance baskets and returns table', 'unit': 'dividend-adjusted close; published only as indexed levels and percentage returns'}
    snap = CACHE / 'equities_daily.csv'
    if not a.skip_equities:
        try:
            import zoneinfo
            tickers = sorted({t for v in TICKERS.values() for t in v})
            now_et = dt.datetime.now(zoneinfo.ZoneInfo('America/New_York'))
            if a.offline:
                if not snap.exists():
                    raise FileNotFoundError('offline and no equities snapshot in cache/')
                px = pd.read_csv(snap, index_col=0, parse_dates=True); eq_log['status'] = 'cached'
            else:
                import yfinance as yf
                px = yf.download(tickers, start=EQUITY_START, auto_adjust=True, progress=False, threads=True)['Close']
                px = px.sort_index().dropna(axis=1, how='all'); px.index = pd.to_datetime(px.index).tz_localize(None)
                if px.index[-1].date() == now_et.date() and now_et.hour * 60 + now_et.minute < 16 * 60 + 30:
                    px = px.iloc[:-1]; print('     note: dropped the partial intraday bar for today', file=sys.stderr)
                CACHE.mkdir(exist_ok=True); px.to_csv(snap, float_format='%.6f'); eq_log['status'] = 'fresh'
            eq_log.update(fetched_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), sha256=sha(snap.read_bytes()), snapshot='cache/equities_daily.csv (local, not published)', first_obs=str(px.index[0].date()), last_obs=str(px.index[-1].date()), observations=int(len(px)))
            missing = sorted(set(tickers) - set(px.columns))
            if missing: print(f'     note: no price history for {missing}; excluded', file=sys.stderr)
            TICKERS.update({k: [t for t in v if t in px.columns] for k, v in TICKERS.items()})
            px, splices = cut_splices(px)
            for t_, d_ in splices.items(): print(f'     note: {t_} history through {d_} excluded (restructuring splice); review in manifest', file=sys.stderr)
            raw_px = px  # actual observations only
            filled = px.ffill()  # bounded carry-forward, used solely to weight the daily-rebalanced baskets
            rets = filled.pct_change()
            baskets = {}
            for name in ('refiners', 'eandp', 'canada'):
                cols = [t_ for t_ in TICKERS[name] if t_ in filled.columns]
                b = (1 + rets[cols].mean(axis=1).fillna(0)).cumprod() * 100; b.iloc[0] = 100
                worst = b.pct_change().abs().max()
                if worst > 0.5: print(f'     warning: basket {name} has a daily move of {worst:.0%}; check for splices', file=sys.stderr)
                baskets[name] = b
            for name, b in baskets.items():
                bb = b[b.index >= BASKET_START]; bb = bb / bb.iloc[0] * 100
                out['basket_' + name] = {'label': BASKET_LABELS[name], 'unit': 'index', 'freq': 'd', 'group': 'equities', 'source': 'Yahoo Finance (via yfinance), equal-weight total return, index', 'id': ', '.join(TICKERS[name]), **pack(bb, BASKET_START, 2, 'd')}
            if 'XLE' in raw_px.columns:
                s = raw_px['XLE'].dropna(); s = s[s.index >= BASKET_START]; s = s / s.iloc[0] * 100
                out['px_XLE'] = {'label': 'XLE', 'unit': 'index', 'freq': 'd', 'group': 'equities', 'source': 'Yahoo Finance (via yfinance), dividend-adjusted, index', 'id': 'XLE', **pack(s, BASKET_START, 2, 'd')}
            weekly = raw_px.resample('W-FRI').last().dropna(how='all')
            members = {}
            for name, lst in TICKERS.items():
                for t_ in lst:
                    if t_ in weekly.columns:
                        s = weekly[t_].dropna()
                        if len(s): members[t_] = {'group': name, **pack(s / s.iloc[0] * 100, EQUITY_START, 2, 'w')}
            def ret(series, delta):
                try:
                    base = series[:series.index[-1] - delta]
                    return round((series.iloc[-1] / base.iloc[-1] - 1) * 100, 1) if len(base) else None
                except Exception: return None
            table = []
            rows = [(t_, raw_px[t_].dropna(), name, False) for name, lst in TICKERS.items() for t_ in lst if t_ in raw_px.columns] + [('Basket: ' + BASKET_LABELS[n], baskets[n], 'basket', False) for n in baskets]
            if 'wti' in r: rows.append(('WTI spot', r['wti'], 'commodity', True))
            if 'brent' in r: rows.append(('Brent spot', r['brent'], 'commodity', True))
            for label, s, grp, show_last in rows:
                s = s.dropna()
                if s.empty: continue
                ytd = s[s.index < pd.Timestamp(s.index[-1].year, 1, 1)]
                row = {'name': label, 'group': grp, 'as_of': str(s.index[-1].date()), '1w': ret(s, pd.Timedelta(days=7)), '1m': ret(s, pd.DateOffset(months=1)), '3m': ret(s, pd.DateOffset(months=3)),
                       'ytd': round((s.iloc[-1] / ytd.iloc[-1] - 1) * 100, 1) if len(ytd) else None, '1y': ret(s, pd.DateOffset(years=1)), '3y': ret(s, pd.DateOffset(years=3))}
                if show_last: row['last'] = round(float(s.iloc[-1]), 2)
                table.append(row)
            equities = {'as_of': str(raw_px.index[-1].date()), 'members': members, 'table': table, 'groups': TICKERS, 'excluded': missing, 'splices': splices}
            eq_log.update(series_id=', '.join(px.columns), excluded=missing, splices=splices)
            print(f'  ok equities {len(px.columns)} tickers, last {raw_px.index[-1].date()}')
        except Exception as e:
            eq_log.update(status='failed', error=f'{type(e).__name__}: {e}'); print(f'  !! equities: {e}', file=sys.stderr)
            prev = DATA / 'series.json'
            if prev.exists():
                old = json.loads(prev.read_text())
                equities = old.get('equities'); out.update({k: v for k, v in old['series'].items() if k.startswith(('basket_', 'px_'))})
                eq_log.update(status='stale', note='reused previous equities bundle')
    manifest.append(eq_log)

    # Any series that could not be produced this run (source down and no cache, e.g. in a fresh CI runner) is carried over
    # from the previous committed bundle and marked stale, so the page never loses a chart because of one bad fetch.
    carried = []
    for k, v in (prev.get('series') or {}).items():
        if k not in out:
            out[k] = v; carried.append(k)
    for k, v in (prev.get('seasonal') or {}).items():
        if k not in seas: seas[k] = v
    for k in carried:
        m = next((m for m in manifest if m.get('key') == k), None)
        if m is None: manifest.append({'key': k, 'source': out[k].get('source', ''), 'series_id': out[k].get('id', ''), 'label': out[k].get('label', k), 'unit': out[k].get('unit', '')})
        else: m.pop('error', None) if False else None
        (m or manifest[-1]).update(status='stale', note='reused the previous bundle because this run could not produce the series')
    if carried: print(f'     note: carried over {len(carried)} series from the previous bundle: {carried[:8]}{"…" if len(carried) > 8 else ""}', file=sys.stderr)
    bundle = {'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'series': out, 'seasonal': seas, 'equities': equities}
    text = json.dumps(bundle, separators=(',', ':'), ensure_ascii=False)
    (DATA / 'series.json').write_text(text)
    man = {'generated_at': bundle['generated_at'], 'bundle_sha256': sha(text.encode()), 'bundle_bytes': len(text.encode()), 'series': manifest,
           'notes': ['EIA data are public domain (https://www.eia.gov/about/copyrights_reuse.php).', 'Alberta Economic Dashboard data are released under the Open Government Licence - Alberta.', 'Equity prices come from Yahoo Finance through the yfinance package; the published bundle contains only indexed levels and percentage returns, no price levels.', next((m['source'] for m in manifest if m.get('key') == 'cad_usd'), 'No exchange-rate source') + ' supplies the Canadian dollar exchange rate, used only to express the Alberta gas price in US$/MMBtu.', 'Baker Hughes weekly rig counts are not fetched: the download requires a browser session; EIA monthly rig counts are used instead.']}
    (DATA / 'manifest.json').write_text(json.dumps(man, indent=1, ensure_ascii=False))
    (DATA / 'bundle.json').write_text(json.dumps({**bundle, 'manifest': manifest, 'bundle_sha256': man['bundle_sha256'], 'notes': man['notes']}, separators=(',', ':'), ensure_ascii=False))
    failed = [m['key'] for m in manifest if m.get('status') in ('failed', 'missing')]
    if failed and not prev: pass
    stale = [m['key'] for m in manifest if m.get('status') == 'stale']
    print(f'wrote {len(out)} series, {len(seas)} seasonal tables, {len(text)//1024} KB; failed={failed or "none"}; stale={stale or "none"}')
    return 1 if failed else 0

if __name__ == '__main__':
    sys.exit(main())

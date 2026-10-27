#!/usr/bin/env python3
"""Fixture tests for the dashboard formulas and the cadence encoding. Run: python3 test_dashboard.py (needs pandas)."""
import datetime as dt, json, sys
import pandas as pd
import fetch_data as fd

def decode(obj):
    """Mirror of the page's JavaScript decoder."""
    pts = []
    if obj['cad'] == 'b':
        d = dt.date(1970, 1, 1) + dt.timedelta(days=obj['start'])
        for v in obj['v']:
            if v is not None: pts.append((d, v))
            d += dt.timedelta(days=3 if d.weekday() == 4 else 1)
    elif obj['cad'] == 'w':
        for i, v in enumerate(obj['v']):
            if v is not None: pts.append((dt.date(1970, 1, 1) + dt.timedelta(days=obj['start'] + 7 * i), v))
    else:
        y, m = divmod(obj['start'], 100)
        for i, v in enumerate(obj['v']):
            if v is not None: pts.append((dt.date(y + (m - 1 + i) // 12, (m - 1 + i) % 12 + 1, 1), v))
    return pts

def test_cracks():
    g, d, w = 2.0, 3.0, 70.0
    assert abs((2 * g * 42 + d * 42) / 3 - w - 28) < 1e-9
    assert abs((g * 42 + d * 42) / 2 - w - 35) < 1e-9
    assert abs(g * 42 - w - 14) < 1e-9 and abs(d * 42 - w - 56) < 1e-9

def test_gas_conversion():
    assert abs(2.0 * 1.055056 / 1.25 - 1.6880896) < 1e-6

def test_pack_roundtrip_business_days():
    idx = pd.bdate_range('2024-12-30', '2025-01-10'); s = pd.Series(range(len(idx)), index=idx, dtype=float).drop(pd.Timestamp('2025-01-01'))
    obj = fd.pack(s, '2024-01-01', 2, 'd'); pts = decode(obj)
    assert [p[0] for p in pts] == [t.date() for t in s.index] and [p[1] for p in pts] == list(s.values)

def test_pack_roundtrip_weekly_and_monthly():
    idx = pd.date_range('2025-01-03', periods=6, freq='7D'); s = pd.Series([1, 2, 3, 4, 5, 6], index=idx, dtype=float).drop(idx[2])
    pts = decode(fd.pack(s, '2024-01-01', 0, 'w')); assert [p[0] for p in pts] == [t.date() for t in s.index]
    m = pd.Series([10.0, 30.0], index=pd.to_datetime(['2025-11-15', '2026-01-15'])); obj = fd.pack(m, '1990-01-01', 1, 'm')
    assert obj['start'] == 202511 and obj['v'] == [10.0, None, 30.0] and decode(obj)[-1][0] == dt.date(2026, 1, 1)

def test_seasonal_band_excludes_current_year():
    weeks = pd.date_range('2020-01-03', '2026-10-16', freq='7D'); s = pd.Series([float(t.year - 2019) * 10 for t in weeks], index=weeks)
    s[s.index.year == 2026] = 100.0
    T = fd.seasonal(s, 1)
    assert T['band_years'] == '2021–2025' and T['current_year'] == 2026 and T['prior_year'] == 2025
    assert T['min'][10] == 20 and T['max'][10] == 60 and T['avg'][10] == 40 and T['current'][10] == 100
    assert T['min'][52] is None  # week 53 has fewer than three observations

def test_negative_or_zero_values_survive_packing():
    s = pd.Series([0.0, -37.63, 20.0], index=pd.to_datetime(['2020-04-17', '2020-04-20', '2020-04-21']))
    assert fd.pack(s, '2020-01-01', 2, 'd')['v'] == [0.0, -37.63, 20.0]

def test_cut_splices_excludes_pre_emergence_history():
    idx = pd.bdate_range('2020-11-16', periods=7)
    px = pd.DataFrame({'CHRD': [0.07, 0.07, 0.07, 0.07, 18.84, 19.0, 19.2], 'VLO': [50, 51, 52, 53, 54, 55.0, 56.0]}, index=idx)
    cut, splices = fd.cut_splices(px)
    assert splices == {'CHRD': '2020-11-20'} and cut['CHRD'].first_valid_index() == pd.Timestamp('2020-11-23') and cut['VLO'].notna().all()  # the splice day itself is excluded too
    assert (cut.ffill().pct_change()['CHRD'].abs().max() < 0.1)

def test_read_eia_rejects_html():
    try:
        fd.read_eia(b'<html><body>Service unavailable</body></html>' + b' ' * 600); assert False, 'HTML accepted'
    except ValueError:
        pass

if __name__ == '__main__':
    tests = [v for k, v in globals().items() if k.startswith('test_')]
    for t in tests: t(); print('ok', t.__name__)
    print(f'{len(tests)} tests passed')

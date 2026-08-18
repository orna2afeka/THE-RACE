"""
pt1000.py — motor temperature sensor: resistance (Ω) → temperature (°C)
=======================================================================
The motor's temperature sensor is a PT1000 RTD: a platinum element whose
resistance rises with temperature, passing through exactly 1000.0 Ω at 0 °C.
The Silixcon controller reports the RAW RESISTANCE over CAN; turning that into
degrees is our job, and this module is the single place it happens (the driver
HUD and the pit both call in here, so they can never disagree).

WHY A TABLE AND NOT A FORMULA
The HUD previously used the Callendar-Van Dusen equation. Its familiar
quadratic form is only valid at or above 0 °C — below zero CVD needs a further
term, so the old code drifted increasingly wrong as it got colder and could
return nonsense near the bottom of the range. A datasheet lookup table is
exact at every listed point and trivially auditable against the paper the
sensor shipped with, which is what matters when a number is used to decide
whether to pit a car.

TABLE LAYOUT — READ THIS BEFORE EDITING
Each key is a base temperature and each row holds 10 resistances, one per unit
digit. The direction of that unit digit DIFFERS BY SIGN, which is easy to get
backwards and produces a plausible-looking but completely wrong curve:

    row  40 → [40 °C, 41 °C, ... 49 °C]     column ADDS      (40 + col)
    row -40 → [-40 °C, -41 °C, ... -49 °C]  column SUBTRACTS (-40 - col)

You can confirm the negative direction from the numbers themselves: row -70
starts at 723.3 Ω and falls to 687.3 Ω across the row. Resistance falls as a
PT1000 gets colder, so that last column must be -79 °C, not -61 °C. Reading it
the wrong way would make the curve fold back on itself — which is exactly what
the monotonicity check at the bottom of this file refuses to let happen.

GAP AT -1 °C … -9 °C
Standard RTD tables print those nine values in a separate "-0" row, which this
table does not include (its 0 row runs upward, 0 → 9 °C). Nothing is broken:
the interpolator simply bridges -10 °C (960.9 Ω) to 0 °C (1000.0 Ω) as one
straight segment. A PT1000 is very nearly linear there, so the error is under
about 0.05 °C — far below sensor tolerance, and well outside any temperature a
motor on a race track will ever reach.

EDITING THE TABLE
Replace numbers freely — the checks at import time will reject a table that is
non-monotonic or has a short row, so a typo fails loudly at startup instead of
quietly reporting a wrong motor temperature mid-race. To verify by hand:

    python3 SolarRace_OS/modules/pt1000.py
"""

import bisect

# --------------------------------------------------------------------------- #
# Datasheet table: base temperature (°C) -> 10 resistances (Ω), one per unit
# digit. See "TABLE LAYOUT" above for the sign rule.
# --------------------------------------------------------------------------- #
OHM_TO_TEMP_TABLE = {
    -70: [723.3, 719.3, 715.3, 711.3, 707.3, 703.3, 699.3, 695.3, 691.3, 687.3],
    -60: [763.3, 759.3, 755.3, 751.3, 747.3, 743.3, 739.3, 735.3, 731.3, 727.3],
    -50: [803.1, 799.1, 795.1, 791.1, 787.2, 783.2, 779.2, 775.2, 771.2, 767.3],
    -40: [842.7, 838.7, 834.8, 830.8, 826.9, 822.9, 818.9, 815.0, 811.0, 807.0],
    -30: [882.2, 878.3, 874.3, 870.4, 866.4, 862.5, 858.5, 854.6, 850.6, 846.7],
    -20: [921.6, 917.7, 913.7, 909.8, 905.9, 901.9, 898.0, 894.0, 890.1, 886.2],
    -10: [960.9, 956.9, 953.0, 949.1, 945.2, 941.2, 937.3, 933.4, 929.5, 925.5],
    0:   [1000.0, 1003.9, 1007.8, 1011.7, 1015.6, 1019.5, 1023.4, 1027.3, 1031.2, 1035.1],
    10:  [1039.0, 1042.9, 1046.8, 1050.7, 1054.6, 1058.5, 1062.4, 1066.3, 1070.2, 1074.0],
    20:  [1077.9, 1081.8, 1085.7, 1089.6, 1093.5, 1097.3, 1101.2, 1105.1, 1109.0, 1112.9],
    30:  [1116.7, 1120.6, 1124.5, 1128.3, 1132.2, 1136.1, 1140.0, 1143.8, 1147.7, 1151.5],
    40:  [1155.4, 1159.3, 1163.1, 1167.0, 1170.8, 1174.7, 1178.6, 1182.4, 1186.3, 1190.1],
    50:  [1194.0, 1197.8, 1201.7, 1205.5, 1209.4, 1213.2, 1217.1, 1220.9, 1224.7, 1228.6],
    60:  [1232.4, 1236.3, 1240.1, 1243.9, 1247.8, 1251.6, 1255.4, 1259.3, 1263.1, 1266.9],
    70:  [1270.8, 1274.6, 1278.4, 1282.2, 1286.1, 1289.9, 1293.7, 1297.5, 1301.3, 1305.2],
    80:  [1309.0, 1312.8, 1316.6, 1320.4, 1324.2, 1328.0, 1331.8, 1335.7, 1339.5, 1343.3],
    90:  [1347.1, 1350.9, 1354.7, 1358.5, 1362.3, 1366.1, 1369.9, 1373.7, 1377.5, 1381.3],
    100: [1385.1, 1388.8, 1392.6, 1396.4, 1400.2, 1404.0, 1407.8, 1411.6, 1415.4, 1419.1],
    110: [1422.9, 1426.7, 1430.5, 1434.3, 1438.0, 1441.8, 1445.6, 1449.4, 1453.1, 1456.9],
    120: [1460.7, 1464.4, 1468.2, 1472.0, 1475.7, 1479.5, 1483.3, 1487.0, 1490.8, 1494.6],
    130: [1498.3, 1502.1, 1505.8, 1509.6, 1513.3, 1517.1, 1520.8, 1524.6, 1528.3, 1532.1],
    140: [1535.8, 1539.6, 1543.3, 1547.1, 1550.8, 1554.6, 1558.3, 1562.0, 1565.8, 1569.5],
    150: [1573.3, 1577.0, 1580.7, 1584.5, 1588.2, 1591.9, 1595.6, 1599.4, 1603.1, 1606.8],
    160: [1610.5, 1614.3, 1618.0, 1621.7, 1625.4, 1629.1, 1632.9, 1636.6, 1640.3, 1644.0],
    170: [1647.7, 1651.4, 1655.1, 1658.9, 1662.6, 1666.3, 1670.0, 1673.7, 1677.4, 1681.1],
    180: [1684.8, 1688.5, 1692.2, 1695.9, 1699.6, 1703.3, 1707.0, 1710.7, 1714.3, 1718.0],
    190: [1721.7, 1725.4, 1729.1, 1732.8, 1736.5, 1740.2, 1743.8, 1747.5, 1751.2, 1754.9],
    200: [1758.6, 1762.2, 1765.9, 1769.6, 1773.3, 1776.9, 1780.6, 1784.3, 1787.9, 1791.6],
    210: [1795.3, 1798.9, 1802.6, 1806.3, 1809.9, 1813.6, 1817.2, 1820.9, 1824.6, 1828.2],
    220: [1831.9, 1835.5, 1839.2, 1842.8, 1846.5, 1850.1, 1853.8, 1857.4, 1861.1, 1864.7],
    230: [1868.4, 1872.0, 1875.6, 1879.3, 1882.9, 1886.6, 1890.2, 1893.8, 1897.5, 1901.1],
    240: [1904.7, 1908.4, 1912.0, 1915.6, 1919.2, 1922.9, 1926.5, 1930.1, 1933.7, 1937.4],
    250: [1941.0, 1944.6, 1948.2, 1951.8, 1955.5, 1959.1, 1962.7, 1966.3, 1969.9, 1973.5],
    260: [1977.1, 1980.7, 1984.3, 1987.9, 1991.5, 1995.1, 1998.7, 2002.3, 2005.9, 2009.5],
    270: [2013.1, 2016.7, 2020.3, 2023.9, 2027.5, 2031.1, 2034.7, 2038.3, 2041.9, 2045.5],
    280: [2049.0, 2052.6, 2056.2, 2059.8, 2063.4, 2067.0, 2070.5, 2074.1, 2077.7, 2081.3],
    290: [2084.8, 2088.4, 2092.0, 2095.6, 2099.1, 2102.7, 2106.3, 2109.8, 2113.4, 2117.0],
    300: [2120.5, 2124.1, 2127.6, 2131.2, 2134.8, 2138.3, 2141.9, 2145.4, 2149.0, 2152.5],
    310: [2156.1, 2159.6, 2163.2, 2166.7, 2170.3, 2173.8, 2177.4, 2180.9, 2184.4, 2188.0],
    320: [2191.5, 2195.1, 2198.6, 2202.1, 2205.7, 2209.2, 2212.7, 2216.3, 2219.8, 2223.3],
    330: [2226.8, 2230.4, 2233.9, 2237.4, 2240.9, 2244.5, 2248.0, 2251.5, 2255.0, 2258.5],
    340: [2262.1, 2265.6, 2269.1, 2272.6, 2276.1, 2279.6, 2283.1, 2286.6, 2290.2, 2293.7],
    350: [2297.2, 2300.7, 2304.2, 2307.7, 2311.2, 2314.7, 2318.2, 2321.7, 2325.2, 2328.7],
    360: [2332.1, 2335.6, 2339.1, 2342.6, 2346.1, 2349.6, 2353.1, 2356.6, 2360.0, 2363.5],
    370: [2367.0, 2370.5, 2374.0, 2377.4, 2380.9, 2384.4, 2387.9, 2391.3, 2394.8, 2398.3],
    380: [2401.8, 2405.2, 2408.7, 2412.2, 2415.6, 2419.1, 2422.6, 2426.0, 2429.5, 2432.9],
    390: [2436.4, 2439.9, 2443.3, 2446.8, 2450.2, 2453.7, 2457.1, 2460.6, 2464.0, 2467.5],
    400: [2470.9, 2474.4, 2477.8, 2481.3, 2484.7, 2488.1, 2491.6, 2495.0, 2498.5, 2501.9],
    410: [2505.3, 2508.8, 2512.2, 2515.6, 2519.1, 2522.5, 2525.9, 2529.3, 2532.8, 2536.2],
    420: [2539.6, 2543.0, 2546.5, 2549.9, 2553.3, 2556.7, 2560.1, 2563.5, 2567.0, 2570.4],
    430: [2573.8, 2577.2, 2580.6, 2584.0, 2587.4, 2590.8, 2594.2, 2597.6, 2601.0, 2604.4],
    440: [2607.8, 2611.2, 2614.6, 2618.0, 2621.4, 2624.8, 2628.2, 2631.6, 2635.0, 2638.4],
    450: [2641.8, 2645.2, 2648.6, 2652.0, 2655.3, 2658.7, 2662.1, 2665.5, 2668.9, 2672.2],
    460: [2675.6, 2679.0, 2682.4, 2685.7, 2689.1, 2692.5, 2695.9, 2699.2, 2702.6, 2706.0],
    470: [2709.3, 2712.7, 2716.1, 2719.4, 2722.8, 2726.1, 2729.5, 2732.9, 2736.2, 2739.6],
    480: [2742.9, 2746.3, 2749.6, 2753.0, 2756.3, 2759.7, 2763.0, 2766.4, 2769.7, 2773.1],
    490: [2776.4, 2779.8, 2783.1, 2786.4, 2789.8, 2793.1, 2796.4, 2799.8, 2803.1, 2806.4],
    500: [2809.8, 2813.1, 2816.4, 2819.8, 2823.1, 2826.4, 2829.7, 2833.1, 2836.4, 2839.7],
}

# Readings at or below this are treated as "no sensor" rather than a cold
# motor: a shorted probe, an unpopulated field, or the gauges being zeroed on
# CAN silence all show up as 0. A real PT1000 cannot go near it (it is 1000 Ω
# at 0 °C and the table bottoms out at 687.3 Ω).
SHORT_CIRCUIT_OHMS = 100.0

# Status strings returned alongside the temperature.
STATUS_OK = "ok"
STATUS_NO_READING = "no_reading"      # nothing sent / shorted / zeroed
STATUS_BELOW_RANGE = "below_range"    # colder than the table's coldest entry
STATUS_ABOVE_RANGE = "above_range"    # hotter than the table, or open circuit


def _temperature_for(base: int, column: int) -> int:
    """Temperature that a (row, column) cell represents.

    The unit digit counts DOWN for sub-zero rows and UP for the rest — see the
    module docstring. This one function is the whole sign convention.
    """
    return base - column if base < 0 else base + column


def _build_curve():
    """Flatten the table into two parallel, ascending lists (ohms, temps).

    Raises ValueError on a malformed table so a datasheet typo is caught at
    import — on the Pi that means the app refuses to start with a bad table
    rather than reporting a wrong motor temperature all race.
    """
    points = []
    for base, resistances in OHM_TO_TEMP_TABLE.items():
        if len(resistances) != 10:
            raise ValueError(
                f"pt1000 table row {base} has {len(resistances)} entries, expected 10"
            )
        for column, ohms in enumerate(resistances):
            points.append((float(ohms), _temperature_for(base, column)))

    points.sort()                       # by resistance, ascending
    ohms = [p[0] for p in points]
    temps = [float(p[1]) for p in points]

    # Both axes must rise together. If they don't, the table has a typo or the
    # row-direction rule above no longer matches the data — either way the
    # interpolation below would be meaningless, so refuse to load.
    for i in range(1, len(ohms)):
        if ohms[i] <= ohms[i - 1]:
            raise ValueError(
                f"pt1000 table is not monotonic in resistance: "
                f"{ohms[i-1]}Ω ({temps[i-1]:.0f}°C) then {ohms[i]}Ω ({temps[i]:.0f}°C)"
            )
        if temps[i] <= temps[i - 1]:
            raise ValueError(
                f"pt1000 table is not monotonic in temperature: "
                f"{temps[i-1]:.0f}°C at {ohms[i-1]}Ω then {temps[i]:.0f}°C at {ohms[i]}Ω"
            )
    return ohms, temps


_OHMS, _TEMPS = _build_curve()

MIN_OHMS, MAX_OHMS = _OHMS[0], _OHMS[-1]
MIN_CELSIUS, MAX_CELSIUS = _TEMPS[0], _TEMPS[-1]


def celsius_from_ohms(ohms):
    """Convert a PT1000 resistance to °C, or None if it can't be converted.

    Linear interpolation between the two bracketing datasheet points. The table
    is spaced 1 °C apart, so within range the interpolation error is negligible.

    Deliberately returns None — never a guess — outside the table. Extrapolating
    past 500 °C or below -79 °C would turn a disconnected sensor into a
    confident-looking number, and a motor temperature is something the pit acts
    on. Use `read()` when you want to know WHY it returned None.
    """
    if ohms is None:
        return None
    try:
        r = float(ohms)
    except (TypeError, ValueError):
        return None
    if r <= SHORT_CIRCUIT_OHMS or r < MIN_OHMS or r > MAX_OHMS:
        return None

    i = bisect.bisect_left(_OHMS, r)
    if i < len(_OHMS) and _OHMS[i] == r:
        return _TEMPS[i]                       # exact datasheet point

    lo_r, hi_r = _OHMS[i - 1], _OHMS[i]
    lo_t, hi_t = _TEMPS[i - 1], _TEMPS[i]
    span = hi_r - lo_r
    if span <= 0:                              # unreachable: _build_curve checks
        return lo_t
    return round(lo_t + (r - lo_r) * (hi_t - lo_t) / span, 2)


def read(ohms):
    """Convert, and also say what happened: (celsius_or_None, status).

    `status` is one of the STATUS_* constants. The UIs use it to distinguish
    "sensor not connected" from "we simply have no frame yet", so the driver is
    never shown a temperature the data doesn't support.
    """
    if ohms is None:
        return None, STATUS_NO_READING
    try:
        r = float(ohms)
    except (TypeError, ValueError):
        return None, STATUS_NO_READING
    if r <= SHORT_CIRCUIT_OHMS:
        return None, STATUS_NO_READING
    if r < MIN_OHMS:
        return None, STATUS_BELOW_RANGE
    if r > MAX_OHMS:
        return None, STATUS_ABOVE_RANGE       # very likely an open circuit
    return celsius_from_ohms(r), STATUS_OK


def ohms_from_celsius(celsius):
    """Inverse lookup — the resistance a given temperature should produce.

    Only used for bench checks and the self-test below (feed a known °C, see
    what the sensor ought to read), never on the hot path.
    """
    if celsius is None:
        return None
    t = float(celsius)
    if t < MIN_CELSIUS or t > MAX_CELSIUS:
        return None
    i = bisect.bisect_left(_TEMPS, t)
    if i < len(_TEMPS) and _TEMPS[i] == t:
        return _OHMS[i]
    lo_t, hi_t = _TEMPS[i - 1], _TEMPS[i]
    lo_r, hi_r = _OHMS[i - 1], _OHMS[i]
    return round(lo_r + (t - lo_t) * (hi_r - lo_r) / (hi_t - lo_t), 2)


# --------------------------------------------------------------------------- #
# Self-check:  python3 SolarRace_OS/modules/pt1000.py
# Run this after editing the table — it re-derives the curve and prints the
# anchor points you can compare straight against the datasheet.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    print(f"table loaded: {len(_OHMS)} points, "
          f"{MIN_CELSIUS:.0f}..{MAX_CELSIUS:.0f} °C, "
          f"{MIN_OHMS:.1f}..{MAX_OHMS:.1f} Ω")
    print("monotonic in both axes: OK (checked at import)\n")

    print("  anchor points from the table")
    for t in (-79, -70, -10, 0, 25, 100, 150, 500):
        print(f"    {t:>5} °C  ->  {ohms_from_celsius(t)!s:>8} Ω")

    print("\n  conversions")
    for r in (687.3, 1000.0, 1097.3, 1385.1, 2839.7):
        c, s = read(r)
        print(f"    {r:>8.1f} Ω  ->  {c!s:>8} °C   [{s}]")

    print("\n  edge cases")
    for r in (None, 0, 50, 500.0, 3000.0):
        c, s = read(r)
        print(f"    {str(r):>8} Ω  ->  {c!s:>8} °C   [{s}]")

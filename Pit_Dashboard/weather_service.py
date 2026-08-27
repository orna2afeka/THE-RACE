import requests
import pandas as pd
import streamlit as st

# (connect, read) seconds. requests defaults to NO timeout at all, which meant a
# hung link to open-meteo blocked indefinitely — and it blocked on the script-run
# thread, which Streamlit also uses to redraw every live tile on the pit wall.
# The hourly cache made that rare rather than harmless: rare and unbounded is how
# you get one inexplicable freeze a day that nobody can reproduce.
_TIMEOUT = (3.05, 10)


def fetch_zolder_weather():
    """Next 24h of Zolder weather as a DataFrame, or None if it can't be fetched.

    DELIBERATELY NOT CACHED — the cache is one level down, on _fetch_or_raise.

    The split exists because st.cache_data memoises whatever a function returns,
    including a None. With the try/except inside the cached function, a single
    dropped packet blanked the weather tab for the full hour and the only cure
    was restarting the dashboard. A cached function that RAISES is not memoised,
    so failure falls through to here, gets swallowed, and the next tick simply
    tries again — while a success is still cached for the full hour.

    Never raises: the weather tab is informational and must not be able to take
    the dashboard down with it.
    """
    try:
        return _fetch_or_raise()
    except Exception:
        return None


@st.cache_data(ttl=3600)  # Saves the answer for 1 hour to avoid unnecessary API calls
def _fetch_or_raise():
    """Fetch and shape the forecast. Raises on any failure — see the caller."""
    # Latitude and longitude of the Zolder track, Belgium
    url = ("https://api.open-meteo.com/v1/forecast"
           "?latitude=50.9895&longitude=5.2568"
           "&hourly=temperature_2m,cloudcover,direct_radiation&forecast_days=2")
    response = requests.get(url, timeout=_TIMEOUT)
    # A 4xx/5xx still parses as JSON and would otherwise KeyError further down
    # with a message that says nothing about the request having failed.
    response.raise_for_status()
    data = response.json()
    df = pd.DataFrame({
        "Time": pd.to_datetime(data["hourly"]["time"]),
        "Temp (°C)": data["hourly"]["temperature_2m"],
        "Cloud Cover (%)": data["hourly"]["cloudcover"],
        "Solar Radiation (W/m²)": data["hourly"]["direct_radiation"]
    })
    # Cutting the data from the current time to 24 hours ahead
    current_time = pd.Timestamp.now().tz_localize(None)
    df = df[df["Time"] >= current_time].head(24)
    return df

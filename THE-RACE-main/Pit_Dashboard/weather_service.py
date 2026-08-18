import requests
import pandas as pd
import streamlit as st

@st.cache_data(ttl=3600) # Saves the answer for 1 hour to avoid unnecessary API calls
def fetch_zolder_weather():
    # Latitude and longitude of the Zolder track, Belgium
    url = "https://api.open-meteo.com/v1/forecast?latitude=50.9895&longitude=5.2568&hourly=temperature_2m,cloudcover,direct_radiation&forecast_days=2"
    try:
        response = requests.get(url)
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
    except Exception:
        return None
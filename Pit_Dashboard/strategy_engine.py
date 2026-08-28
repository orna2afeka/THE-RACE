import math
import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import streamlit as st

# Default velocity profile lives next to this file, so a no-arg call works no
# matter what the launch directory is (the dashboard passes an absolute path).
_DEFAULT_PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "210s.xlsx")


@st.cache_data
def load_velocity_profile(filepath=_DEFAULT_PROFILE):
    try:
        df = pd.read_excel(filepath)
        # convert velocity from m/s to km/h for easier interpretation
        df['V(km/h)'] = df['V(m/s)'] * 3.6
        return df[['d(m)', 'V(km/h)', 'section']].copy()
    except Exception:
        return None

def get_target_speed(profile_df, current_dist_m):
    if profile_df is None or profile_df.empty:
        return 68.0 
    distances = profile_df['d(m)'].values
    speeds = profile_df['V(km/h)'].values
    # Find the target speed using linear interpolation based on the current distance
    return np.interp(current_dist_m, distances, speeds)

def get_track_section(profile_df, current_dist_m):
    if profile_df is None or profile_df.empty:
        return "Unknown"
    idx = (np.abs(profile_df['d(m)'] - current_dist_m)).argmin()
    return profile_df.iloc[idx]['section']

def calculate_all_strategies(time_left_min, current_available_wh, current_lap, consumption_table, track_length_km=4.0):
    CHARGE_RATE_WH_MIN = 150
    MAX_STOPS = 3
    MIN_STOP_DURATION = 30
    DRIVER_STINT_LIMIT_MIN = 120
    DRIVER_CHANGE_TIME_MIN = 5
    BATTERY_FLOOR_WH = 450  
    FULL_CHARGE_WH = 8550  
    FULL_STINT_USABLE_WH = FULL_CHARGE_WH - BATTERY_FLOOR_WH  

    current_usable_wh = max(0, current_available_wh - BATTERY_FLOOR_WH)
    all_strategies = []

    for option in consumption_table:
        label = option['label']
        lap_time_min = option['lap_time_min']
        energy_per_lap = option['energy_wh']

        laps_possible = int(time_left_min // lap_time_min)
        valid_strategy = False
        best_stops = 0
        best_pit_time = 0
        best_driver_changes = 0
        stint_laps_list = []

        while laps_possible > 0 and not valid_strategy:
            energy_needed = laps_possible * energy_per_lap

            if energy_needed <= current_usable_wh:
                stint_time = laps_possible * lap_time_min
                driver_changes = int((stint_time - 0.001) // DRIVER_STINT_LIMIT_MIN)
                if driver_changes < 0: driver_changes = 0
                total_race_time = stint_time + (driver_changes * DRIVER_CHANGE_TIME_MIN)

                if total_race_time <= time_left_min:
                    valid_strategy = True
                    best_stops = 0
                    best_pit_time = 0
                    best_driver_changes = driver_changes
                    stint_laps_list = [laps_possible]
            else:
                remaining_energy_needed = energy_needed - current_usable_wh
                min_stops = math.ceil(remaining_energy_needed / FULL_STINT_USABLE_WH)

                if 1 <= min_stops <= MAX_STOPS:
                    for s in range(min_stops, MAX_STOPS + 1):
                        min_mandatory_pit_time = s * MIN_STOP_DURATION
                        actual_charge_time_needed = remaining_energy_needed / CHARGE_RATE_WH_MIN
                        total_pit_time = max(min_mandatory_pit_time, actual_charge_time_needed)

                        laps_first_stint = int(current_usable_wh // energy_per_lap)
                        temp_stint_laps = [laps_first_stint]
                        laps_left = laps_possible - laps_first_stint
                        
                        if laps_left < 0: continue
                        laps_per_full_stint = laps_left // s

                        for _ in range(s - 1):
                            temp_stint_laps.append(laps_per_full_stint)
                        temp_stint_laps.append(laps_left - (laps_per_full_stint * (s - 1)))

                        total_driver_changes = 0
                        for stint_laps in temp_stint_laps:
                            stint_time = stint_laps * lap_time_min
                            changes = int((stint_time - 0.001) // DRIVER_STINT_LIMIT_MIN)
                            if changes < 0: changes = 0
                            total_driver_changes += changes

                        total_driver_time_lost = total_driver_changes * DRIVER_CHANGE_TIME_MIN
                        total_race_time = (laps_possible * lap_time_min) + total_pit_time + total_driver_time_lost

                        if total_race_time <= time_left_min:
                            valid_strategy = True
                            best_stops = s
                            best_pit_time = total_pit_time
                            best_driver_changes = total_driver_changes
                            stint_laps_list = temp_stint_laps
                            break
            if valid_strategy:
                break
            laps_possible -= 1

        if valid_strategy:
            speed_kmh = track_length_km / (lap_time_min / 60)
            pit_msg = f"{best_stops} Stops" if best_stops > 0 else "No Stops"
            driver_msg = f"{best_driver_changes} Swaps" if best_driver_changes > 0 else "0 Swaps"
            strategy_data = {
                'Label': label, 'Lap Time': f"{lap_time_min:.2f} m", 'Speed (km/h)': f"{speed_kmh:.1f}",
                'Total Laps': laps_possible, 'Energy/Lap (Wh)': energy_per_lap,
                'Pit Strategy': pit_msg, 'Driver Swaps': driver_msg,
                '_graph_data': {
                    'label': label, 'start_wh': current_available_wh, 'stints': stint_laps_list,
                    'lap_time': lap_time_min, 'energy_per_lap': energy_per_lap,
                    'pit_time': best_pit_time / best_stops if best_stops > 0 else 0, 'total_time': time_left_min
                }
            }
        else:
            strategy_data = {
                'Label': label, 'Lap Time': f"{lap_time_min:.2f} m", 'Speed (km/h)': "-", 
                'Total Laps': 0, 'Energy/Lap (Wh)': energy_per_lap,
                'Pit Strategy': "-", 'Driver Swaps': "-", '_graph_data': None
            }
        all_strategies.append(strategy_data)
    return all_strategies

def create_combined_graph(graph_data_list):
    fig, ax = plt.subplots(figsize=(10, 3))
    colors = ['#e74c3c', '#e67e22', '#f1c40f', '#3498db', '#9b59b6']
    fig.patch.set_facecolor('#0e1117')
    ax.set_facecolor('#0e1117')
    ax.tick_params(colors='white')
    ax.xaxis.label.set_color('white')
    ax.yaxis.label.set_color('white')
    ax.title.set_color('white')
    
    CHARGE_RATE = 150
    DRIVER_STINT = 120
    DRIVER_SWAP = 5

    for idx, graph_data in enumerate(graph_data_list):
        if not graph_data: continue
        t_points, wh_points = [0], [graph_data['start_wh']]
        t_current, wh_current = 0, graph_data['start_wh']
        lap_time, energy_per_lap = graph_data['lap_time'], graph_data['energy_per_lap']

        for i, stint_laps in enumerate(graph_data['stints']):
            t_stint_rem = stint_laps * lap_time
            discharge_rate = energy_per_lap / lap_time

            while t_stint_rem > 0.001:
                if t_stint_rem > DRIVER_STINT:
                    t_current += DRIVER_STINT
                    wh_current -= DRIVER_STINT * discharge_rate
                    t_points.append(t_current)
                    wh_points.append(wh_current)
                    t_current += DRIVER_SWAP
                    t_points.append(t_current)
                    wh_points.append(wh_current)
                    t_stint_rem -= DRIVER_STINT
                else:
                    t_current += t_stint_rem
                    wh_current -= t_stint_rem * discharge_rate
                    t_points.append(t_current)
                    wh_points.append(wh_current)
                    t_stint_rem = 0

            if i < len(graph_data['stints']) - 1:
                t_current += graph_data['pit_time']
                wh_current += graph_data['pit_time'] * CHARGE_RATE
                if wh_current > 8550: wh_current = 8550
                t_points.append(t_current)
                wh_points.append(wh_current)

        x_points = [max(0, graph_data['total_time'] - t) for t in t_points]
        ax.plot(x_points, wh_points, label=graph_data['label'], color=colors[idx], linewidth=2)

    ax.axhline(y=450, color='#e74c3c', linestyle='--', linewidth=1.5, label="450 Wh Floor")
    ax.invert_xaxis()
    ax.set_title("Battery SoC Forecast (Wh vs Mins Left)")
    ax.grid(True, linestyle='--', alpha=0.3, color='gray')
    ax.legend(loc='upper left', fontsize='small', facecolor='#1a1a1a', edgecolor='none', labelcolor='white')
    
    return fig

    # ==============================================================================
# LIVE TRACK MAPPING (Based on the defined Track Segments)
# ==============================================================================
Sections = [
{"segment_id": 1, "start_m": 0, "end_m": 600, "turns": [1], "max_speed_kmh": 75},
{"segment_id": 2, "start_m": 600, "end_m": 1000, "turns": [2, 3], "max_speed_kmh": 80},
{"segment_id": 3, "start_m": 1000, "end_m": 1800, "turns": [4], "max_speed_kmh": 110},
{"segment_id": 4, "start_m": 1800, "end_m": 1910, "turns": [5, 6], "max_speed_kmh": 60},
{"segment_id": 5, "start_m": 1910, "end_m": 2400, "turns": [7], "max_speed_kmh": 78},
{"segment_id": 6, "start_m": 2400, "end_m": 2500, "turns": [8, 9], "max_speed_kmh": 45},
{"segment_id": 7, "start_m": 2500, "end_m": 3000, "turns": [10, 11], "max_speed_kmh": 78},
{"segment_id": 8, "start_m": 3000, "end_m": 3430, "turns": [12], "max_speed_kmh": 40},
{"segment_id": 9, "start_m": 3430, "end_m": 4000, "turns": [15, 16], "max_speed_kmh": 54}
]


TRACK_LANDMARKS = [
    {"name": "Turn 1", "dist_m": 600, "max_speed": 75, "desc": "Slow down to 75 km/h"},
    {"name": "Turn 2", "dist_m": 710, "max_speed": 80, "desc": "Ends at 800 meters, accelerate slowly downhill"},
    {"name": "Start of Uphill", "dist_m": 1010, "max_speed": 110, "desc": "Uphill section, Turn 4 ahead"},
    {"name": "Chicane (Turns 5,6)", "dist_m": 1860, "max_speed": 60, "desc": "Left turn followed by sharp right"},
    {"name": "Turn 7", "dist_m": 2400, "max_speed": 78, "desc": "The turn feels almost straight"},
    {"name": "Turns 8,9", "dist_m": 2500, "max_speed": 45, "desc": "Very slow turns"},
    {"name": "Turns 10,11", "dist_m": 3000, "max_speed": 78, "desc": "Followed by a slow turn"},
    {"name": "Turn 12", "dist_m": 3430, "max_speed": 40, "desc": "Slow turn, followed by uphill acceleration"},
    {"name": "Chicane 15,16", "dist_m": 3900, "max_speed": 54, "desc": "Large chicane near the finish line"},
    {"name": "Finish Line", "dist_m": 4000, "max_speed": 100, "desc": "End of lap"}
]

SECTIONS_INFO = {
    1: {"range": (0, 600), "name": "Section 1"},
    2: {"range": (600, 1000), "name": "Section 2"},
    3: {"range": (1000, 1800), "name": "Section 3"},
    4: {"range": (1800, 1910), "name": "Section 4"},
    5: {"range": (1910, 2400), "name": "Section 5"},
    6: {"range": (2400, 2500), "name": "Section 6"},
    7: {"range": (2500, 3000), "name": "Section 7"},
    8: {"range": (3000, 3430), "name": "Section 8"},
    9: {"range": (3430, 4000), "name": "Section 9"},
}

def get_live_track_status(current_dist_m, profile_df=None):
    """
    Returns the current section, target speed from the profile, and next track landmark.
    """
    current_dist_m = current_dist_m % 4000.0  # Ensure we stay within 0-4000m

    # 1. Identify Current Section
    current_sec_name = "לא ידוע"
    for sec_id, info in SECTIONS_INFO.items():
        if info["range"][0] <= current_dist_m < info["range"][1]:
            current_sec_name = info["name"]
            break

    # 2. Get target speed from Excel profile
    target_speed = 68.0 
    if profile_df is not None and not profile_df.empty:
        target_speed = get_target_speed(profile_df, current_dist_m)

    # 3. Find next landmark
    next_landmark = None
    dist_to_landmark = 0

    for landmark in TRACK_LANDMARKS:
        if landmark["dist_m"] > current_dist_m:
            next_landmark = landmark
            dist_to_landmark = landmark["dist_m"] - current_dist_m
            break

    if not next_landmark:  # Failsafe
        next_landmark = TRACK_LANDMARKS[0]
        dist_to_landmark = (4000 - current_dist_m) + TRACK_LANDMARKS[0]["dist_m"]

    return {
        "section": current_sec_name,
        "target_speed": target_speed,
        "next_feature": next_landmark["name"],
        "next_feature_desc": next_landmark["desc"],
        "next_feature_speed": next_landmark["max_speed"],
        "distance_to_next": dist_to_landmark
    }

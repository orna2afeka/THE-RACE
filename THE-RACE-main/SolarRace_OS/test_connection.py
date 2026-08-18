"""
test_connection.py — Quick CAN connection probe
================================================
Run this on the Pi to check the shared CAN bus without launching the
whole dashboard:

    python SolarRace_OS/test_connection.py

It tries each connection in config.CAN_CANDIDATES (CAN HAT, then USB
adapter), reports which opens, and shows a few live frames so you can
confirm the MMS / BMS / temp devices are all on the bus.
"""
from config import open_bus


def test_connection():
    print("--- CAN CONNECTION PROBE ---")
    bus, label, err = open_bus()
    if bus is None:
        print(f"❌ No CAN bus available: {err}")
        print("⚠️ The dashboard will run in log-simulation mode.")
        return

    print(f"✅ OPENED ({label}). Listening up to 5s for frames...\n")
    try:
        seen = 0
        for _ in range(50):
            msg = bus.recv(timeout=5.0 if seen == 0 else 0.2)
            if msg is None:
                break
            seen += 1
            print(f"  📡 id=0x{msg.arbitration_id:X} "
                  f"({'ext' if msg.is_extended_id else 'std'}) "
                  f"data={bytes(msg.data).hex()}")
        if seen == 0:
            print("  ⚠️ Bus opened but silent "
                  "(devices off, or BMS needs polling — note the BMS only "
                  "replies to queries, which this probe does not send).")
        else:
            print(f"\n✅ Saw {seen} frame(s).")
    finally:
        try:
            bus.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    test_connection()

# config.py -- fill these in, then upload to the Pico alongside the other files.
# Keep this file private: your WiFi password and station API key both live here.

WIFI_SSID = "your-wifi-name"
WIFI_PASSWORD = "your-wifi-password"

# From the "Register station" flow on livequakes.com -- shown once, save it
# then. Register a new station for this device (don't reuse a test key).
STATION_API_KEY = "PASTE_YOUR_STATION_API_KEY_HERE"

INGEST_URL = "https://us-central1-livequakes.cloudfunctions.net/ingestEvent"

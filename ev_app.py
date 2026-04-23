import streamlit as st
import hashlib
import numpy as np
import pandas as pd
import os
import time
import random
import plotly.express as px
import plotly.graph_objects as go
import math
import requests
import threading
import re


try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    _PG_AVAILABLE = True
except ImportError:
    _PG_AVAILABLE = False


def _pg_conn():
    """Return a new psycopg2 connection using Streamlit secrets."""
    cfg = st.secrets.get("postgres", {})
    return psycopg2.connect(
        host     = cfg.get("host",     "localhost"),
        port     = int(cfg.get("port", 5432)),
        dbname   = cfg.get("dbname",   "evee_db"),
        user     = cfg.get("user",     "postgres"),
        password = cfg.get("password", ""),
    )


@st.cache_resource
def _init_pg_schema():
    """Create tables if they don't exist. Called once per process."""
    if not _PG_AVAILABLE:
        return False
    try:
        conn = _pg_conn()
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username        TEXT PRIMARY KEY,
                password_hash   TEXT NOT NULL,
                role            TEXT NOT NULL DEFAULT 'User',
                skill           TEXT DEFAULT 'Intermediate',
                car_model       TEXT,
                car_plate       TEXT UNIQUE,
                phone           TEXT,
                email           TEXT,
                vehicle_type    TEXT DEFAULT 'UNKNOWN',
                battery_kwh     REAL,
                voltage_v       INTEGER,
                max_ac_kw       REAL,
                max_dc_kw       REAL,
                voltage_tier    TEXT,
                specs_confirmed BOOLEAN DEFAULT FALSE,
                created_at      TIMESTAMP DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS policy_settings (
                policy_name  TEXT PRIMARY KEY,
                enabled      BOOLEAN DEFAULT TRUE,
                notes        TEXT DEFAULT ''
            );
        """)
        # Seed default admin + demo user if table is empty
        cur.execute("SELECT COUNT(*) FROM users;")
        if cur.fetchone()[0] == 0:
            import hashlib as _hl
            cur.execute("""
                INSERT INTO users (username, password_hash, role, phone, email)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING;
            """, ("owner", _hl.sha256(b"adminpass").hexdigest(), "Owner", "", ""))
            cur.execute("""
                INSERT INTO users
                  (username, password_hash, role, skill, car_model, car_plate,
                   phone, email, vehicle_type, battery_kwh, voltage_v,
                   max_ac_kw, max_dc_kw, voltage_tier, specs_confirmed)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING;
            """, ("rluser1", _hl.sha256(b"userpass").hexdigest(), "User",
                  "Intermediate", "Tata Nexon EV", "TN01AB1234",
                  "9876543210", "user@example.com",
                  "BEV", 30.2, 320, 7.2, 50.0, "400V Fast", True))
        conn.commit()
        cur.close(); conn.close()
        return True
    except Exception as e:
        return False


def _pg_get_user(username: str) -> dict | None:
    """Fetch one user row as a dict. Returns None if not found or PG unavailable."""
    if not _PG_AVAILABLE:
        return None
    try:
        conn = _pg_conn()
        cur  = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE username = %s;", (username,))
        row  = cur.fetchone()
        cur.close(); conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def _pg_get_all_users() -> list[dict]:
    """Fetch all users for Owner registry."""
    if not _PG_AVAILABLE:
        return []
    try:
        conn = _pg_conn()
        cur  = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users ORDER BY created_at;")
        rows = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close()
        return rows
    except Exception:
        return []


def _pg_username_exists(username: str) -> bool:
    if not _PG_AVAILABLE:
        return False
    try:
        conn = _pg_conn()
        cur  = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE username = %s;", (username,))
        exists = cur.fetchone() is not None
        cur.close(); conn.close()
        return exists
    except Exception:
        return False


def _pg_plate_exists(plate: str) -> bool:
    """Check uniqueness of number plate across all users."""
    if not _PG_AVAILABLE:
        return False
    try:
        conn = _pg_conn()
        cur  = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE UPPER(car_plate) = UPPER(%s);", (plate,))
        exists = cur.fetchone() is not None
        cur.close(); conn.close()
        return exists
    except Exception:
        return False


def _pg_insert_user(data: dict) -> bool:
    if not _PG_AVAILABLE:
        return False
    try:
        conn = _pg_conn()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO users
              (username, password_hash, role, skill, car_model, car_plate,
               phone, email, vehicle_type, battery_kwh, voltage_v,
               max_ac_kw, max_dc_kw, voltage_tier, specs_confirmed)
            VALUES (%(username)s, %(password_hash)s, %(role)s, %(skill)s,
                    %(car_model)s, %(car_plate)s, %(phone)s, %(email)s,
                    %(vehicle_type)s, %(battery_kwh)s, %(voltage_v)s,
                    %(max_ac_kw)s, %(max_dc_kw)s, %(voltage_tier)s, %(specs_confirmed)s);
        """, data)
        conn.commit()
        cur.close(); conn.close()
        return True
    except Exception:
        return False


def _pg_update_skill(username: str, skill: str):
    if not _PG_AVAILABLE:
        return
    try:
        conn = _pg_conn()
        cur  = conn.cursor()
        cur.execute("UPDATE users SET skill = %s WHERE username = %s;", (skill, username))
        conn.commit()
        cur.close(); conn.close()
    except Exception:
        pass


def _use_pg() -> bool:
    """True if PostgreSQL is available AND secrets are configured."""
    if not _PG_AVAILABLE:
        return False
    try:
        cfg = st.secrets.get("postgres", {})
        return bool(cfg.get("host"))
    except Exception:
        return False


# ─────────────────────────────────────────────
# INDIAN VEHICLE PLATE VALIDATOR
# Formats accepted:
#   New BH series  : BH 01 AA 1234  →  BH01AA1234
#   Standard new   : TN 01 AB 1234  →  TN01AB1234
#   Old pre-2000   : TN 01 A 1234   →  TN01A1234
# ─────────────────────────────────────────────
_PLATE_RE = re.compile(
    r'^([A-Z]{2})\s*'          # State/BH code (2 letters)
    r'(\d{1,2})\s*'           # District / year code (1-2 digits)
    r'([A-Z]{1,3})\s*'         # Series letters (1-3 letters)
    r'(\d{4})$',               # 4-digit number
    re.IGNORECASE
)

def validate_indian_plate(plate: str) -> tuple[bool, str, str]:
    """
    Returns (is_valid, normalised_plate, error_message).
    Normalised plate is upper-cased with no spaces: TN01AB1234
    """
    if not plate or not plate.strip():
        return False, "", "Number plate is required."
    clean = plate.strip().upper().replace(" ", "").replace("-", "")
    m = _PLATE_RE.match(clean)
    if not m:
        return False, "", (
            "Invalid plate format. Use Indian standard: "
            "TN01AB1234 (State + District + Series + Number). "
            "E.g. TN01AB1234, MH02CD5678, BH01AA1234."
        )
    normalised = "".join(m.groups())
    return True, normalised, ""

# ─────────────────────────────────────────────
# PAGE CONFIG — must be FIRST Streamlit call
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="EVEE Pricing Agent",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
BASE_PRICE_PER_KWH = 15.0 

MODEL_PATHS = {
    "PPO": "ppo_ev_pricing.zip",
    "SAC": "sac_ev_pricing.zip",
    "TD3": "td3_ev_pricing.zip",
}

DRIVER_PROFILES = {
    "Novice":         {"desc": "New to EVs. Prefers nearby stations with clear guidance.",    "price_weight": 0.3, "distance_weight": 0.5, "charger_min_kw": 0},
    "Intermediate":   {"desc": "Comfortable with EVs. Balances cost and distance.",           "price_weight": 0.5, "distance_weight": 0.3, "charger_min_kw": 22},
    "Expert":         {"desc": "Power user. Prioritises fast charging and cost savings.",     "price_weight": 0.6, "distance_weight": 0.2, "charger_min_kw": 50},
    "Fleet Operator": {"desc": "Manages many vehicles. Optimises for availability and cost.", "price_weight": 0.7, "distance_weight": 0.1, "charger_min_kw": 75},
}

OCM_API_URL = "https://api.openchargemap.io/v3/poi/"
OCM_API_KEY = ""

OCM_LEVEL_KW = {1: 3.7, 2: 22, 3: 50, 4: 100, 5: 150, 6: 350}

VEHICLE_DB: dict = {
    # ── TATA ──────────────────────────────────
    "tata nexon ev":           {"battery_kwh": 30.2,  "voltage_v": 320,  "max_ac_kw": 7.2,  "max_dc_kw": 50,  "type": "BEV",  "display": "Tata Nexon EV"},
    "tata nexon ev max":       {"battery_kwh": 40.5,  "voltage_v": 350,  "max_ac_kw": 11.0, "max_dc_kw": 50,  "type": "BEV",  "display": "Tata Nexon EV Max"},
    "tata tiago ev":           {"battery_kwh": 24.0,  "voltage_v": 320,  "max_ac_kw": 3.3,  "max_dc_kw": 50,  "type": "BEV",  "display": "Tata Tiago EV"},
    "tata tigor ev":           {"battery_kwh": 26.0,  "voltage_v": 320,  "max_ac_kw": 3.3,  "max_dc_kw": 25,  "type": "BEV",  "display": "Tata Tigor EV"},
    "tata punch ev":           {"battery_kwh": 25.0,  "voltage_v": 320,  "max_ac_kw": 7.2,  "max_dc_kw": 50,  "type": "BEV",  "display": "Tata Punch EV"},
    "tata nexon":              {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Tata Nexon (Petrol/Diesel)"},
    "tata harrier":            {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Tata Harrier (Diesel)"},
    "tata safari":             {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Tata Safari (Diesel)"},
    "tata altroz":             {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Tata Altroz (Petrol)"},
    # ── MG ────────────────────────────────────
    "mg zs ev":                {"battery_kwh": 50.3,  "voltage_v": 400,  "max_ac_kw": 7.4,  "max_dc_kw": 76,  "type": "BEV",  "display": "MG ZS EV"},
    "mg comet ev":             {"battery_kwh": 17.3,  "voltage_v": 72,   "max_ac_kw": 3.3,  "max_dc_kw": 0,   "type": "BEV",  "display": "MG Comet EV"},
    "mg windsor ev":           {"battery_kwh": 38.0,  "voltage_v": 350,  "max_ac_kw": 11.0, "max_dc_kw": 60,  "type": "BEV",  "display": "MG Windsor EV"},
    "mg hector":               {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "MG Hector (Petrol)"},
    "mg astor":                {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "MG Astor (Petrol)"},
    # ── HYUNDAI ───────────────────────────────
    "hyundai ioniq 5":         {"battery_kwh": 72.6,  "voltage_v": 800,  "max_ac_kw": 11.0, "max_dc_kw": 220, "type": "BEV",  "display": "Hyundai Ioniq 5"},
    "hyundai ioniq 6":         {"battery_kwh": 77.4,  "voltage_v": 800,  "max_ac_kw": 11.0, "max_dc_kw": 230, "type": "BEV",  "display": "Hyundai Ioniq 6"},
    "hyundai kona electric":   {"battery_kwh": 39.2,  "voltage_v": 400,  "max_ac_kw": 7.2,  "max_dc_kw": 100, "type": "BEV",  "display": "Hyundai Kona Electric"},
    "hyundai creta electric":  {"battery_kwh": 51.4,  "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 60,  "type": "BEV",  "display": "Hyundai Creta Electric"},
    "hyundai i20":             {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Hyundai i20 (Petrol)"},
    "hyundai creta":           {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Hyundai Creta (Petrol/Diesel)"},
    "hyundai verna":           {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Hyundai Verna (Petrol)"},
    "hyundai alcazar":         {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Hyundai Alcazar (Petrol/Diesel)"},
    "hyundai tucson":          {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Hyundai Tucson (Petrol/Diesel)"},
    # ── KIA ───────────────────────────────────
    "kia ev6":                 {"battery_kwh": 77.4,  "voltage_v": 800,  "max_ac_kw": 11.0, "max_dc_kw": 233, "type": "BEV",  "display": "Kia EV6"},
    "kia ev9":                 {"battery_kwh": 99.8,  "voltage_v": 800,  "max_ac_kw": 11.0, "max_dc_kw": 233, "type": "BEV",  "display": "Kia EV9"},
    "kia seltos":              {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Kia Seltos (Petrol/Diesel)"},
    "kia sonet":               {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Kia Sonet (Petrol/Diesel)"},
    "kia carens":              {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Kia Carens (Petrol/Diesel)"},
    # ── BYD ───────────────────────────────────
    "byd atto 3":              {"battery_kwh": 60.5,  "voltage_v": 400,  "max_ac_kw": 7.0,  "max_dc_kw": 80,  "type": "BEV",  "display": "BYD Atto 3"},
    "byd seal":                {"battery_kwh": 82.56, "voltage_v": 800,  "max_ac_kw": 11.0, "max_dc_kw": 150, "type": "BEV",  "display": "BYD Seal"},
    "byd e6":                  {"battery_kwh": 71.7,  "voltage_v": 400,  "max_ac_kw": 7.0,  "max_dc_kw": 40,  "type": "BEV",  "display": "BYD e6"},
    # ── VOLVO ─────────────────────────────────
    "volvo xc40 recharge":     {"battery_kwh": 78.0,  "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 150, "type": "BEV",  "display": "Volvo XC40 Recharge"},
    "volvo c40 recharge":      {"battery_kwh": 78.0,  "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 150, "type": "BEV",  "display": "Volvo C40 Recharge"},
    "volvo xc60":              {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Volvo XC60 (Petrol/Diesel)"},
    # ── BMW ───────────────────────────────────
    "bmw i4":                  {"battery_kwh": 83.9,  "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 205, "type": "BEV",  "display": "BMW i4"},
    "bmw ix":                  {"battery_kwh": 111.5, "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 200, "type": "BEV",  "display": "BMW iX"},
    "bmw i7":                  {"battery_kwh": 101.7, "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 195, "type": "BEV",  "display": "BMW i7"},
    "bmw 3 series":            {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "BMW 3 Series (Petrol)"},
    "bmw 5 series":            {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "BMW 5 Series (Petrol/Diesel)"},
    "bmw x5":                  {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "BMW X5 (Petrol/Diesel)"},
    # ── MERCEDES ──────────────────────────────
    "mercedes eqs":            {"battery_kwh": 107.8, "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 200, "type": "BEV",  "display": "Mercedes EQS"},
    "mercedes eqb":            {"battery_kwh": 66.5,  "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 100, "type": "BEV",  "display": "Mercedes EQB"},
    "mercedes eqe":            {"battery_kwh": 90.6,  "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 170, "type": "BEV",  "display": "Mercedes EQE"},
    "mercedes c class":        {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Mercedes C-Class (Petrol)"},
    "mercedes e class":        {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Mercedes E-Class (Petrol/Diesel)"},
    # ── AUDI ──────────────────────────────────
    "audi e-tron":             {"battery_kwh": 95.0,  "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 150, "type": "BEV",  "display": "Audi e-tron"},
    "audi q8 e-tron":          {"battery_kwh": 114.0, "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 170, "type": "BEV",  "display": "Audi Q8 e-tron"},
    "audi a6 e-tron":          {"battery_kwh": 100.0, "voltage_v": 800,  "max_ac_kw": 11.0, "max_dc_kw": 270, "type": "BEV",  "display": "Audi A6 e-tron"},
    "audi a4":                 {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Audi A4 (Petrol)"},
    "audi q5":                 {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Audi Q5 (Petrol/Diesel)"},
    # ── TESLA ─────────────────────────────────
    "tesla model 3":           {"battery_kwh": 82.0,  "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 250, "type": "BEV",  "display": "Tesla Model 3"},
    "tesla model y":           {"battery_kwh": 82.0,  "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 250, "type": "BEV",  "display": "Tesla Model Y"},
    "tesla model s":           {"battery_kwh": 100.0, "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 250, "type": "BEV",  "display": "Tesla Model S"},
    "tesla model x":           {"battery_kwh": 100.0, "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 250, "type": "BEV",  "display": "Tesla Model X"},
    # ── MARUTI / SUZUKI ───────────────────────
    "maruti suzuki e vitara":  {"battery_kwh": 61.0,  "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 100, "type": "BEV",  "display": "Maruti Suzuki e Vitara"},
    "maruti swift":            {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Maruti Swift (Petrol)"},
    "maruti baleno":           {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Maruti Baleno (Petrol)"},
    "maruti alto":             {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Maruti Alto (Petrol)"},
    "maruti brezza":           {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Maruti Brezza (Petrol)"},
    "maruti ertiga":           {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Maruti Ertiga (Petrol)"},
    "maruti vitara":           {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Maruti Vitara (Petrol)"},
    # ── HONDA ─────────────────────────────────
    "honda city":              {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Honda City (Petrol)"},
    "honda amaze":             {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Honda Amaze (Petrol/Diesel)"},
    "honda elevate":           {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Honda Elevate (Petrol)"},
    # ── TOYOTA ────────────────────────────────
    "toyota camry hybrid":     {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "HYBRID","display": "Toyota Camry Hybrid (no plug)"},
    "toyota innova hycross":   {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "HYBRID","display": "Toyota Innova HyCross (no plug)"},
    "toyota fortuner":         {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Toyota Fortuner (Diesel)"},
    "toyota glanza":           {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Toyota Glanza (Petrol)"},
    "toyota bz4x":             {"battery_kwh": 71.4,  "voltage_v": 400,  "max_ac_kw": 6.6,  "max_dc_kw": 150, "type": "BEV",  "display": "Toyota bZ4X"},
    # ── MAHINDRA ──────────────────────────────
    "mahindra xuv400 ev":      {"battery_kwh": 39.4,  "voltage_v": 350,  "max_ac_kw": 7.2,  "max_dc_kw": 50,  "type": "BEV",  "display": "Mahindra XUV400 EV"},
    "mahindra be 6e":          {"battery_kwh": 79.0,  "voltage_v": 800,  "max_ac_kw": 11.0, "max_dc_kw": 175, "type": "BEV",  "display": "Mahindra BE 6e"},
    "mahindra xuv9e":          {"battery_kwh": 79.0,  "voltage_v": 800,  "max_ac_kw": 11.0, "max_dc_kw": 175, "type": "BEV",  "display": "Mahindra XUV 9e"},
    "mahindra xuv700":         {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Mahindra XUV700 (Petrol/Diesel)"},
    "mahindra scorpio":        {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Mahindra Scorpio (Diesel)"},
    "mahindra thar":           {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Mahindra Thar (Petrol/Diesel)"},
    # ── VOLKSWAGEN ────────────────────────────
    "volkswagen id.4":         {"battery_kwh": 77.0,  "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 135, "type": "BEV",  "display": "Volkswagen ID.4"},
    "volkswagen id.3":         {"battery_kwh": 77.0,  "voltage_v": 400,  "max_ac_kw": 11.0, "max_dc_kw": 135, "type": "BEV",  "display": "Volkswagen ID.3"},
    "volkswagen polo":         {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Volkswagen Polo (Petrol)"},
    "volkswagen taigun":       {"battery_kwh": 0,     "voltage_v": 0,    "max_ac_kw": 0,    "max_dc_kw": 0,   "type": "ICE",  "display": "Volkswagen Taigun (Petrol)"},
    # ── NISSAN ────────────────────────────────
    "nissan leaf":             {"battery_kwh": 40.0,  "voltage_v": 360,  "max_ac_kw": 6.6,  "max_dc_kw": 50,  "type": "BEV",  "display": "Nissan Leaf"},
    "nissan ariya":            {"battery_kwh": 87.0,  "voltage_v": 400,  "max_ac_kw": 22.0, "max_dc_kw": 130, "type": "BEV",  "display": "Nissan Ariya"},
    # ── OLA / ATHER / HERO (2-wheelers) ───────
    "ola s1 pro":              {"battery_kwh": 4.0,   "voltage_v": 72,   "max_ac_kw": 0.9,  "max_dc_kw": 0,   "type": "BEV",  "display": "Ola S1 Pro (Scooter)"},
    "ather 450x":              {"battery_kwh": 3.7,   "voltage_v": 60,   "max_ac_kw": 0.75, "max_dc_kw": 0,   "type": "BEV",  "display": "Ather 450X (Scooter)"},
    "ather rizta":             {"battery_kwh": 4.8,   "voltage_v": 60,   "max_ac_kw": 0.9,  "max_dc_kw": 0,   "type": "BEV",  "display": "Ather Rizta (Scooter)"},
    "bajaj chetak":            {"battery_kwh": 3.0,   "voltage_v": 60,   "max_ac_kw": 0.72, "max_dc_kw": 0,   "type": "BEV",  "display": "Bajaj Chetak (Scooter)"},
    "tvs iqube":               {"battery_kwh": 5.1,   "voltage_v": 72,   "max_ac_kw": 0.9,  "max_dc_kw": 0,   "type": "BEV",  "display": "TVS iQube (Scooter)"},
}

ICE_KEYWORDS = [
    "petrol", "diesel", "cng", "hybrid" , "hycross", "camry hybrid",
    "swift", "alto", "wagonr", "baleno", "dzire", "celerio",
    "city", "amaze", "jazz", "wr-v", "elevate",
    "i10", "i20", "venue", "grand i10",
    "punch", "altroz", "harrier", "safari",  
    "thar", "scorpio", "bolero", "xuv300", "xuv700",
    "fortuner", "innova", "hilux", "glanza", "urban cruiser",
    "seltos", "sonet", "carens", "carnival",
    "polo", "vento", "taigun", "virtus",
    "hector", "gloster", "astor",
    "duster", "kwid", "triber", "kiger",
    "compass", "meridian",
]

EV_KEYWORDS = ["ev", "electric", "bev", "ioniq", "e-tron", "etron", "recharge",
               "id.", "ariya", "leaf", "zs ev", "e vitara"]


def lookup_vehicle(car_model_raw: str) -> dict:

    key = car_model_raw.strip().lower()


    for db_key, specs in VEHICLE_DB.items():
        if db_key in key or key in db_key:
            return {**specs, "found": True}


    key_lower = key
    has_ice_kw = any(kw in key_lower for kw in ICE_KEYWORDS)
    has_ev_kw  = any(kw in key_lower for kw in EV_KEYWORDS)

    if has_ev_kw and not has_ice_kw:
        return {
            "type": "UNKNOWN_EV", "battery_kwh": None, "voltage_v": None,
            "max_ac_kw": None, "max_dc_kw": None,
            "display": car_model_raw,
            "found": False,
        }
    if has_ice_kw and not has_ev_kw:
        return {
            "type": "ICE", "battery_kwh": 0, "voltage_v": 0,
            "max_ac_kw": 0, "max_dc_kw": 0,
            "display": car_model_raw,
            "found": False,
        }

    return {
        "type": "UNKNOWN", "battery_kwh": None, "voltage_v": None,
        "max_ac_kw": None, "max_dc_kw": None,
        "display": car_model_raw,
        "found": False,
    }


def is_ev_vehicle(car_model: str) -> tuple[bool, str, dict]:
    specs = lookup_vehicle(car_model)
    vtype = specs["type"]

    if vtype == "BEV":
        return True, "BEV", specs
    if vtype == "PHEV":
        return True, "PHEV", specs
    if vtype == "UNKNOWN_EV":
        return True, "UNKNOWN_EV", specs
    if vtype == "ICE":
        return False, "ICE", specs
    if vtype == "HYBRID":
        return False, "HYBRID", specs
    return True, "UNKNOWN", specs


def get_voltage_tier(voltage_v) -> str:
    """Human-readable voltage tier label."""
    if voltage_v is None:
        return "Unknown"
    if voltage_v >= 700:
        return "800V Ultra-Fast"
    if voltage_v >= 350:
        return "400V Fast"
    if voltage_v >= 200:
        return "350V Standard"
    if voltage_v >= 60:
        return "Low-Voltage (2-Wheeler)"
    return "Unknown"


STATION_TEMPLATES = [
    {"name": "EV Max",      "lat_offset":  0.015, "lon_offset":  0.020, "price_offset":  0.8, "charger_kw": 50,  "slots": 6},
    {"name": "VoltPoint",   "lat_offset": -0.010, "lon_offset":  0.030, "price_offset":  1.5, "charger_kw": 22,  "slots": 4},
    {"name": "GreenCharge", "lat_offset":  0.025, "lon_offset": -0.010, "price_offset": -0.5, "charger_kw": 150, "slots": 8},
    {"name": "RapidCharge", "lat_offset": -0.030, "lon_offset": -0.020, "price_offset":  0.2, "charger_kw": 75,  "slots": 5},
    {"name": "PowerFill",   "lat_offset":  0.008, "lon_offset": -0.025, "price_offset": -0.3, "charger_kw": 60,  "slots": 5},
    {"name": "ZapZone",     "lat_offset": -0.018, "lon_offset":  0.012, "price_offset":  1.0, "charger_kw": 100, "slots": 3},
]


_model_cache: dict = {}
_model_lock = threading.Lock()


def _load_model_worker(policy_name: str):
    """Runs in a background thread — loads model into _model_cache."""
    try:
        import gymnasium as gym
        from stable_baselines3 import PPO, SAC, TD3

        path = MODEL_PATHS.get(policy_name)
        if not path or not os.path.exists(path):
            with _model_lock:
                _model_cache[policy_name] = None
            return

        obs_space = gym.spaces.Box(low=0.0, high=1.0, shape=(7,), dtype=np.float32)
        act_space = gym.spaces.Box(low=-1.0, high=1.0, shape=(1,), dtype=np.float32)
        custom_objects = {
            "observation_space": obs_space,
            "action_space":      act_space,
            "lr_schedule":       lambda _: 3e-4,
            "clip_range":        lambda _: 0.2,
        }

        if policy_name == "PPO":
            model = PPO.load(path, custom_objects=custom_objects)
        elif policy_name == "SAC":
            model = SAC.load(path, custom_objects=custom_objects)
        elif policy_name == "TD3":
            model = TD3.load(path, custom_objects=custom_objects)
        else:
            model = None

        with _model_lock:
            _model_cache[policy_name] = model

    except Exception:
        with _model_lock:
            _model_cache[policy_name] = None


def _start_background_preload():
    for policy_name in MODEL_PATHS:
        if policy_name not in _model_cache:
            t = threading.Thread(target=_load_model_worker, args=(policy_name,), daemon=True)
            t.start()


@st.cache_resource
def _preload_trigger():
    """Called once per process. Starts background model loading."""
    _start_background_preload()
    return True

_preload_trigger() 


def get_model(policy_name: str):
    """
    Return the model if it's ready. If still loading, wait up to 3 s
    (the model was preloading since app start, so this is usually 0 s).
    Falls back to None (rule-based pricing) if unavailable.
    """
    deadline = time.time() + 3.0
    while policy_name not in _model_cache and time.time() < deadline:
        time.sleep(0.05)
    return _model_cache.get(policy_name)


def load_rl_model(policy_name):
    """Public API — returns preloaded model or None."""
    return get_model(policy_name)


def get_dynamic_price(policy_name):
    model = get_model(policy_name)
    state = generate_customer_state()

    if model is not None:
        try:
            action, _ = model.predict(state, deterministic=True)
            multiplier = float(np.clip(action[0], 0.8, 1.5))
        except Exception:
            multiplier = _tou_multiplier()
    else:
        multiplier = _tou_multiplier()

    return round(BASE_PRICE_PER_KWH * multiplier, 2), multiplier, state


def _tou_multiplier():
    hour = pd.Timestamp.now().hour
    if 7 <= hour < 9 or 17 <= hour < 19:
        return 1.4
    elif 23 <= hour or hour < 5:
        return 0.85
    return 1.1


def generate_customer_state():
    hour        = pd.Timestamp.now().hour
    utilization = np.clip(np.random.uniform(0.3, 0.9), 0, 1)
    traffic     = np.clip(0.6 if 7 <= hour < 10 or 17 <= hour < 20 else 0.3 + np.random.uniform(-0.1, 0.1), 0, 1)
    is_weekend  = float(pd.Timestamp.now().dayofweek >= 5)
    rainfall    = float(np.clip(np.random.exponential(0.5), 0, 1))
    temperature = float(np.clip((np.random.uniform(25, 40) - 30) / 10, -1, 1))
    queue       = float(np.clip(np.random.poisson(2) / 10, 0, 1))
    energy      = float(np.clip(np.random.uniform(0.1, 1.0), 0, 1))
    return np.array([utilization, traffic, is_weekend, rainfall, temperature, queue, energy], dtype=np.float32)


@st.cache_data(ttl=300)
def simulate_daily_prices(active_policy):
    """Cache for 5 min so repeated tab switches are instant."""
    model = get_model(active_policy)
    data  = []
    for hour in range(24):
        tou_mult = _tou_mult_for_hour(hour)
        state    = generate_customer_state()
        if model is not None:
            try:
                action, _ = model.predict(state, deterministic=True)
                rl_mult = float(np.clip(action[0], 0.8, 1.5))
            except Exception:
                rl_mult = tou_mult
        else:
            rl_mult = tou_mult
        data.append({
            "Hour": hour,
            "Static Pricing":            BASE_PRICE_PER_KWH,
            "Time-of-Use (ToU) Pricing": BASE_PRICE_PER_KWH * tou_mult,
            "Dynamic (RL) Pricing":      BASE_PRICE_PER_KWH * rl_mult,
        })
    return pd.DataFrame(data).set_index("Hour")


def _tou_mult_for_hour(hour):
    if 7 <= hour < 9 or 17 <= hour < 19:
        return 1.4
    elif 23 <= hour or hour < 5:
        return 0.85
    return 1.1


# ─────────────────────────────────────────────
# ROUTING HELPERS
# ─────────────────────────────────────────────
def haversine_km(lat1, lon1, lat2, lon2):
    R    = 6371
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a    = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def score_station(row, driver_skill, dynamic_price):
    profile    = DRIVER_PROFILES[driver_skill]
    norm_dist  = min(row["distance_km"] / 15, 1.0)
    norm_price = min(row["Price (Rs/kWh)"] / 25, 1.0)
    norm_kw    = 1 - min(row["charger_kw"] / 150, 1.0)
    pw, dw     = profile["price_weight"], profile["distance_weight"]
    kw_w       = 1 - pw - dw
    score      = pw * norm_price + dw * norm_dist + kw_w * norm_kw
    if row["Status"] != "Available":
        score += 0.3
    if row["charger_kw"] < profile["charger_min_kw"]:
        score += 0.2
    return round(score, 4)


def rank_stations(station_df, driver_skill, dynamic_price, user_lat, user_lon):
    df = station_df.copy()
    df["distance_km"]  = df.apply(lambda r: round(haversine_km(user_lat, user_lon, r["lat"], r["lon"]), 2), axis=1)
    df["score"]        = df.apply(lambda r: score_station(r, driver_skill, dynamic_price), axis=1)
    df["est_time_min"] = df["distance_km"].apply(lambda d: max(1, round(d / 30 * 60)))
    df = df.sort_values("score").reset_index(drop=True)
    df["rank"] = df.index + 1
    return df


def _ocm_status(status_type_id):
    mapping = {
        0: "Unknown", 1: "Available", 2: "Occupied",
        3: "Offline",  4: "Available", 5: "Planned", 50: "Available",
        75: "Busy", 100: "Offline", 150: "Available",
        200: "Offline", 210: "Offline",
    }
    return mapping.get(status_type_id, "Unknown")


def _ocm_charger_kw(connections):
    kw_vals = []
    for c in (connections or []):
        if c.get("PowerKW") and c["PowerKW"] > 0:
            kw_vals.append(c["PowerKW"])
        elif c.get("Level") and c["Level"].get("ID"):
            kw_vals.append(OCM_LEVEL_KW.get(c["Level"]["ID"], 22))
    return round(max(kw_vals)) if kw_vals else 22


@st.cache_data(ttl=300, show_spinner=False)
def fetch_ocm_stations(user_lat, user_lon, dynamic_price, radius_km=10, max_results=25):
    """
    Fetch real EV charging stations from Open Charge Map API.
    Cached 5 min. Falls back to simulated data gracefully.
    Returns (DataFrame, source_label).
    """
    api_key = OCM_API_KEY
    try:
        api_key = st.secrets.get("OCM_API_KEY", OCM_API_KEY)
    except Exception:
        pass

    params = {
        "output":          "json",
        "latitude":        round(user_lat, 5),
        "longitude":       round(user_lon, 5),
        "distance":        radius_km,
        "distanceunit":    "KM",
        "maxresults":      max_results,
        "compact":         True,
        "verbose":         False,
        "includecomments": False,
    }
    if api_key:
        params["key"] = api_key

    try:
        resp = requests.get(OCM_API_URL, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()

        if not isinstance(data, list) or len(data) == 0:
            return _fallback_stations(user_lat, user_lon, dynamic_price), "Simulated (no stations found nearby)"

        random.seed(42)
        rows = []
        for poi in data:
            addr_info = poi.get("AddressInfo") or {}
            conns     = poi.get("Connections") or []
            status_id = (poi.get("StatusType") or {}).get("ID", 1)

            name     = addr_info.get("Title") or "EV Station"
            lat      = addr_info.get("Latitude")  or user_lat
            lon      = addr_info.get("Longitude") or user_lon
            kw       = _ocm_charger_kw(conns)
            slots    = max(1, len([c for c in conns if c.get("Quantity", 1)]))
            status   = _ocm_status(status_id)
            operator = (poi.get("OperatorInfo") or {}).get("Title") or "Unknown Operator"
            address  = ", ".join(filter(None, [
                addr_info.get("AddressLine1", ""),
                addr_info.get("Town", ""),
                addr_info.get("StateOrProvince", ""),
            ]))

            conn_types = list(set(
                (c.get("ConnectionType") or {}).get("FormalName") or
                (c.get("ConnectionType") or {}).get("Title") or "Unknown"
                for c in conns
            ))
            conn_label = ", ".join(conn_types[:3]) if conn_types else "Standard"

            price_adj = random.uniform(-2.0, 2.0)
            price     = round(max(8.0, dynamic_price + price_adj), 2)

            rows.append({
                "Station Name":   name,
                "lat":            float(lat),
                "lon":            float(lon),
                "Price (Rs/kWh)": price,
                "Status":         status,
                "charger_kw":     kw,
                "slots":          slots,
                "distance_km":    0,
                "Operator":       operator,
                "Address":        address,
                "Connector":      conn_label,
                "OCM_ID":         str(poi.get("ID", "")),
            })

        if not rows:
            return _fallback_stations(user_lat, user_lon, dynamic_price), "Simulated (parse error)"

        return pd.DataFrame(rows), "Open Charge Map — Live Data"

    except requests.exceptions.ConnectionError:
        return _fallback_stations(user_lat, user_lon, dynamic_price), "Simulated (offline — no internet)"
    except requests.exceptions.Timeout:
        return _fallback_stations(user_lat, user_lon, dynamic_price), "Simulated (OCM API timeout)"
    except requests.exceptions.HTTPError as e:
        return _fallback_stations(user_lat, user_lon, dynamic_price), f"Simulated (API error {e.response.status_code})"
    except Exception:
        return _fallback_stations(user_lat, user_lon, dynamic_price), "Simulated (unexpected error)"


def _fallback_stations(user_lat, user_lon, dynamic_price):
    random.seed(int(user_lat * 1000) % 999)
    rows = []
    for s in STATION_TEMPLATES:
        rows.append({
            "Station Name":   s["name"],
            "lat":            user_lat + s["lat_offset"] + random.uniform(-0.004, 0.004),
            "lon":            user_lon + s["lon_offset"] + random.uniform(-0.004, 0.004),
            "Price (Rs/kWh)": round(max(10, dynamic_price + s["price_offset"] + random.uniform(-0.3, 0.3)), 2),
            "Status":         random.choice(["Available", "Busy", "Available", "Offline"]),
            "charger_kw":     s["charger_kw"],
            "slots":          s["slots"],
            "distance_km":    0,
            "Operator":       "Simulated",
            "Address":        "",
            "Town":           "",
            "OCM_ID":         "",
        })
    return pd.DataFrame(rows)


def build_stations_from_gps(user_lat, user_lon, dynamic_price):
    df, _ = fetch_ocm_stations(user_lat, user_lon, dynamic_price)
    return df


def google_maps_url(origin_lat, origin_lon, dest_lat, dest_lon):
    return (
        f"https://www.google.com/maps/dir/?api=1"
        f"&origin={origin_lat},{origin_lon}"
        f"&destination={dest_lat},{dest_lon}"
        f"&travelmode=driving"
    )


def route_narrative(driver_skill, row):
    msgs = {
        "Novice":         f"Recommended: {row['Station Name']} — {row['distance_km']:.1f} km away, approx {row['est_time_min']} min. Easy access, clearly marked.",
        "Intermediate":   f"{row['Station Name']} — {row['charger_kw']} kW at Rs {row['Price (Rs/kWh)']}/kWh. {row['distance_km']:.1f} km, ~{row['est_time_min']} min. Good cost-speed balance.",
        "Expert":         f"{row['Station Name']} — {row['charger_kw']} kW DC fast charger, Rs {row['Price (Rs/kWh)']}/kWh. {row['distance_km']:.1f} km. Optimal for rapid top-up.",
        "Fleet Operator": f"Optimal: {row['Station Name']} — {row['charger_kw']} kW, Rs {row['Price (Rs/kWh)']}/kWh, {row['distance_km']:.1f} km. Minimises fleet idle time.",
    }
    return msgs.get(driver_skill, "")



def prefetch_stations_async(lat: float, lon: float, price: float):
    """Fire-and-forget: prime the @st.cache_data cache in background."""
    def _worker():
        try:
            fetch_ocm_stations(lat, lon, price)
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()


# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────
def init_session():
    defaults = {
        "active_policy":       "PPO",
        "auth_stage":          "role_select",
        "is_logged_in":        False,
        "role":                None,
        "username":            None,
        "driver_skill":        "Intermediate",
        "gps_lat":             None,
        "gps_lon":             None,
        "gps_source":          None,
        "stations_prefetched": False,
        "pg_available":        False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    # Try to init PG schema; fall back to in-memory dict
    if _use_pg():
        ok = _init_pg_schema()
        st.session_state.pg_available = ok
    else:
        st.session_state.pg_available = False

    # In-memory fallback user store (used when PG is not configured)
    if "user_db" not in st.session_state:
        st.session_state.user_db = {
            "owner":   {
                "password": hashlib.sha256(b"adminpass").hexdigest(),
                "role": "Owner", "phone": "", "email": "",
            },
            "rluser1": {
                "password": hashlib.sha256(b"userpass").hexdigest(),
                "role": "User", "skill": "Intermediate",
                "car_model": "Tata Nexon EV", "car_plate": "TN01AB1234",
                "phone": "9876543210", "email": "user@example.com",
                "vehicle_type": "BEV", "battery_kwh": 30.2,
                "voltage_v": 320, "max_ac_kw": 7.2, "max_dc_kw": 50,
                "voltage_tier": "400V Fast", "specs_confirmed": True,
            },
        }
    if "policy_notes" not in st.session_state:
        st.session_state.policy_notes   = {k: "" for k in MODEL_PATHS}
    if "policy_enabled" not in st.session_state:
        st.session_state.policy_enabled = {k: True for k in MODEL_PATHS}


init_session()


def _get_user_record(username: str) -> dict | None:
    """Unified read: PostgreSQL when available, otherwise session dict."""
    if st.session_state.get("pg_available"):
        return _pg_get_user(username)
    return st.session_state.user_db.get(username)


def check_login(username: str, password: str) -> str | None:
    record = _get_user_record(username)
    if record:
        pwd_key = "password_hash" if "password_hash" in record else "password"
        if hashlib.sha256(password.encode()).hexdigest() == record[pwd_key]:
            return record.get("role")
    return None


def signup_user(username, password, skill, car_model, car_plate, phone, email):
    # ── Basic validation ──────────────────────────────────
    if not username or not password:
        return False, "Username and password are required.", None
    if len(password) < 6:
        return False, "Password must be at least 6 characters.", None

    # ── Indian plate format check ─────────────────────────
    plate_ok, plate_norm, plate_err = validate_indian_plate(car_plate)
    if not plate_ok:
        return False, plate_err, None

    # ── Duplicate username check ──────────────────────────
    if st.session_state.get("pg_available"):
        if _pg_username_exists(username):
            return False, "Username already taken.", None
    else:
        if username in st.session_state.user_db:
            return False, "Username already taken.", None

    # ── Duplicate plate check ─────────────────────────────
    if st.session_state.get("pg_available"):
        if _pg_plate_exists(plate_norm):
            return False, f"Number plate {plate_norm} is already registered.", None
    else:
        for rec in st.session_state.user_db.values():
            if rec.get("car_plate", "").upper() == plate_norm.upper():
                return False, f"Number plate {plate_norm} is already registered.", None

    # ── Vehicle EV validation ─────────────────────────────
    ev_ok, reason, specs = is_ev_vehicle(car_model)
    if not ev_ok:
        vdisplay = specs.get("display", car_model)
        if reason == "ICE":
            return False, (
                f"'{vdisplay}' runs on petrol/diesel and cannot use EV charging stations. "
                "Please select a Battery Electric Vehicle (BEV) or Plug-in Hybrid (PHEV)."
            ), None
        if reason == "HYBRID":
            return False, (
                f"'{vdisplay}' is a non-plug-in hybrid with no charging port. "
                "It cannot use EV charging stations."
            ), None

    # ── Build user record ─────────────────────────────────
    user_data = {
        "username":        username,
        "password_hash":   hashlib.sha256(password.encode()).hexdigest(),
        "password":        hashlib.sha256(password.encode()).hexdigest(),  # fallback key
        "role":            "User",
        "skill":           skill,
        "car_model":       specs.get("display", car_model),
        "car_plate":       plate_norm,
        "phone":           phone,
        "email":           email,
        "vehicle_type":    specs.get("type", "UNKNOWN"),
        "battery_kwh":     specs.get("battery_kwh"),
        "voltage_v":       specs.get("voltage_v"),
        "max_ac_kw":       specs.get("max_ac_kw"),
        "max_dc_kw":       specs.get("max_dc_kw"),
        "voltage_tier":    get_voltage_tier(specs.get("voltage_v")),
        "specs_confirmed": specs.get("found", False),
    }

    # ── Persist ───────────────────────────────────────────
    if st.session_state.get("pg_available"):
        ok = _pg_insert_user(user_data)
        if not ok:
            return False, "Database error — could not save account. Try again.", None
    else:
        st.session_state.user_db[username] = user_data

    return True, "Account created.", specs


# ─────────────────────────────────────────────
# PLOT HELPERS
# ─────────────────────────────────────────────
def plot_pricing_comparison(df):
    fig = go.Figure()
    colors = {
        "Static Pricing":            "#94a3b8",
        "Time-of-Use (ToU) Pricing": "#d97706",
        "Dynamic (RL) Pricing":      "#0369a1",
    }
    for col, color in colors.items():
        fig.add_trace(go.Scatter(
            x=df.index, y=df[col], mode="lines+markers",
            name=col, line=dict(color=color, width=2.5), marker=dict(size=5)
        ))
    fig.update_layout(
        paper_bgcolor="#ffffff", plot_bgcolor="#f8fafc",
        font=dict(color="#1e293b", family="DM Sans, sans-serif", size=13),
        xaxis=dict(title="Hour of Day", gridcolor="#e2e8f0", linecolor="#cbd5e1"),
        yaxis=dict(title="Price (Rs/kWh)", gridcolor="#e2e8f0", linecolor="#cbd5e1"),
        legend=dict(bgcolor="#f1f5f9", bordercolor="#cbd5e1", borderwidth=1),
        margin=dict(t=16, b=40, l=10, r=10), height=340,
    )
    st.plotly_chart(fig, use_container_width=True)


def build_station_map(station_df, user_lat, user_lon, zoom=13, top_station_name=None):
    df = station_df.copy()
    fig = px.scatter_mapbox(
        df, lat="lat", lon="lon",
        hover_name="Station Name",
        hover_data={"Price (Rs/kWh)": True, "Status": True, "charger_kw": True,
                    "distance_km": True, "lat": False, "lon": False},
        zoom=zoom, height=460,
        color_discrete_sequence=["#dc2626"],
    )
    fig.add_trace(go.Scattermapbox(
        lat=[user_lat], lon=[user_lon],
        mode="markers+text",
        marker=dict(size=18, color="#1d4ed8"),
        text=["YOU"], textposition="top right",
        textfont=dict(size=12, color="#1d4ed8"),
        name="Your Location",
    ))
    if top_station_name:
        top_rows = df[df["Station Name"] == top_station_name]
        if not top_rows.empty:
            t = top_rows.iloc[0]
            fig.add_trace(go.Scattermapbox(
                lat=[t["lat"]], lon=[t["lon"]],
                mode="markers",
                marker=dict(size=20, color="#16a34a"),
                name="Recommended",
            ))
    fig.update_layout(
        mapbox_style="open-street-map",
        margin={"r": 0, "t": 0, "l": 0, "b": 0},
        hovermode="closest",
        paper_bgcolor="#ffffff",
        legend=dict(bgcolor="#f1f5f9", bordercolor="#cbd5e1", font=dict(color="#1e293b")),
    )
    return fig


# ─────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&family=Barlow+Condensed:wght@600;700;800&display=swap');

html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; background-color: #f0f4f8; color: #0f172a; }
.stApp { background: #f0f4f8; }

.ev-header {
    background: #0f172a; padding: 20px 36px;
    display: flex; align-items: center; gap: 20px;
    margin: -1rem -1rem 2rem -1rem; border-bottom: 4px solid #0ea5e9;
}
.ev-header-title { font-family: 'Barlow Condensed', sans-serif; font-size: 30px; font-weight: 800; color: #f0f9ff; letter-spacing: 1px; margin: 0; text-transform: uppercase; }
.ev-header-sub { font-family: 'DM Mono', monospace; font-size: 11px; color: #7dd3fc; letter-spacing: 2px; text-transform: uppercase; margin-top: 2px; }
.ev-pill { background:#0ea5e9; color:#fff; font-size:11px; font-weight:700; padding:4px 14px; border-radius:4px; letter-spacing:1.5px; text-transform:uppercase; font-family:'DM Mono',monospace; }
.ev-pill-owner { background:#b45309; color:#fff; font-size:11px; font-weight:700; padding:4px 14px; border-radius:4px; letter-spacing:1.5px; text-transform:uppercase; font-family:'DM Mono',monospace; }
.ev-spacer { flex:1; }

.section-label { font-family:'Barlow Condensed',sans-serif; font-size:22px; font-weight:700; color:#0f172a; text-transform:uppercase; letter-spacing:0.5px; border-left:5px solid #0ea5e9; padding-left:14px; margin:24px 0 16px 0; }
.rec-card { background:#ffffff; border:2px solid #16a34a; border-left:6px solid #16a34a; border-radius:10px; padding:24px 28px; margin:20px 0; box-shadow:0 2px 12px rgba(22,163,74,0.10); }
.rec-card-title { font-family:'Barlow Condensed',sans-serif; font-size:18px; font-weight:700; color:#15803d; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:8px; }
.rec-card-body { font-size:15px; color:#1e293b; line-height:1.6; margin-bottom:16px; }
.rec-stat-label { font-family:'DM Mono',monospace; font-size:10px; color:#64748b; text-transform:uppercase; letter-spacing:1.5px; display:block; margin-top:4px; }
.rec-stat-value { font-family:'DM Mono',monospace; font-size:20px; font-weight:500; color:#0f172a; display:block; }

.gps-card { background:#eff6ff; border:1.5px solid #93c5fd; border-radius:10px; padding:18px 22px; margin:16px 0; }
.gps-card-title { font-family:'Barlow Condensed',sans-serif; font-size:16px; font-weight:700; color:#1d4ed8; text-transform:uppercase; margin-bottom:6px; }

.badge-skill { display:inline-block; background:#eff6ff; color:#1d4ed8; border:1px solid #bfdbfe; border-radius:4px; padding:3px 12px; font-size:12px; font-weight:600; font-family:'DM Mono',monospace; }
.badge-owner { display:inline-block; background:#fff7ed; color:#c2410c; border:1px solid #fdba74; border-radius:4px; padding:3px 12px; font-size:12px; font-weight:600; font-family:'DM Mono',monospace; }
.badge-available { background:#dcfce7; color:#15803d; border:1px solid #86efac; border-radius:4px; padding:2px 10px; font-size:12px; font-weight:600; font-family:'DM Mono',monospace; }
.badge-busy { background:#fef3c7; color:#b45309; border:1px solid #fcd34d; border-radius:4px; padding:2px 10px; font-size:12px; font-weight:600; font-family:'DM Mono',monospace; }
.badge-offline { background:#fee2e2; color:#dc2626; border:1px solid #fca5a5; border-radius:4px; padding:2px 10px; font-size:12px; font-weight:600; font-family:'DM Mono',monospace; }

.model-status-bar { background:#f0fdf4; border:1px solid #86efac; border-radius:8px; padding:8px 16px; font-family:'DM Mono',monospace; font-size:12px; color:#15803d; display:flex; gap:20px; flex-wrap:wrap; margin-bottom:10px; }
.model-loading { color:#b45309; }

.stButton > button { background:#0f172a; border:2px solid #0f172a; color:#f0f9ff; border-radius:6px; font-weight:600; font-family:'DM Sans',sans-serif; font-size:14px; padding:10px 20px; transition:all 0.15s ease; text-transform:uppercase; letter-spacing:0.5px; }
.stButton > button:hover { background:#0ea5e9; border-color:#0ea5e9; color:#fff; }

div[data-testid="stMetricValue"] { font-family:'DM Mono',monospace; font-size:28px; font-weight:500; color:#0f172a; }
div[data-testid="stMetricLabel"] { font-size:12px; color:#475569; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; }

.stTabs [data-baseweb="tab-list"] { background:#e2e8f0; border-radius:8px; padding:4px; gap:4px; }
.stTabs [data-baseweb="tab"] { background:transparent; color:#475569; font-weight:600; font-size:13px; border-radius:6px; padding:8px 18px; text-transform:uppercase; letter-spacing:0.5px; }
.stTabs [aria-selected="true"] { background:#0f172a !important; color:#f0f9ff !important; }

section[data-testid="stSidebar"] { background:#ffffff; border-right:1px solid #e2e8f0; }
.stTextInput > div > div > input { background:#f8fafc; border:1.5px solid #cbd5e1; color:#0f172a; border-radius:6px; }
hr { border-color:#e2e8f0; }
::-webkit-scrollbar { width:6px; height:6px; }
::-webkit-scrollbar-track { background:#f1f5f9; }
::-webkit-scrollbar-thumb { background:#94a3b8; border-radius:3px; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────
if st.session_state.is_logged_in:
    role_label = st.session_state.role or ""
    pill_class = "ev-pill-owner" if role_label == "Owner" else "ev-pill"

    st.markdown(f"""
<div class="ev-header">
  <div>
    <div class="ev-header-title">EVEE Dynamic Pricing Agent</div>
    <div class="ev-header-sub">Reinforcement Learning — EV Charging Network</div>
  </div>
  <div class="ev-spacer"></div>
  <span class="{pill_class}">{role_label.upper()}</span>
</div>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════
# AUTH FLOW
# ═══════════════════════════════════════════════════════
if not st.session_state.is_logged_in:

    st.markdown("""
    <style>.auth-page-wrap { display:none; }</style>
    """, unsafe_allow_html=True)

    left, right = st.columns([1, 1], gap="large")

    with left:
        st.markdown("""
        <div style="
            background: linear-gradient(160deg, #0f172a 0%, #0c2340 60%, #0369a1 100%);
            border-radius: 20px; padding: 60px 48px; min-height: 600px;
            display: flex; flex-direction: column; justify-content: center; gap: 32px; margin-top: 20px;
        ">
            <div>
                <div style="font-family:'Barlow Condensed',sans-serif; font-size:11px; font-weight:700; color:#38bdf8; letter-spacing:4px; text-transform:uppercase; margin-bottom:16px;">RL-POWERED CHARGING</div>
                <div style="font-family:'Barlow Condensed',sans-serif; font-size:52px; font-weight:800; color:#ffffff; line-height:1.05; text-transform:uppercase; letter-spacing:-0.5px;">EVEE<br>Dynamic<br>Pricing</div>
                <div style="font-family:'Barlow Condensed',sans-serif; font-size:52px; font-weight:800; color:#38bdf8; line-height:1.05; text-transform:uppercase;">Agent</div>
            </div>
            <div style="font-family:'DM Sans',sans-serif; font-size:15px; color:#94a3b8; line-height:1.7; max-width:340px;">
                A reinforcement learning system that dynamically prices EV charging based on real-time grid conditions, demand, and driver behaviour.
            </div>
            <div style="display:flex; flex-direction:column; gap:14px; margin-top:8px;">
                <div style="display:flex; align-items:center; gap:12px;"><div style="width:8px; height:8px; border-radius:50%; background:#22c55e;"></div><span style="font-family:'DM Mono',monospace; font-size:13px; color:#cbd5e1;">PPO / SAC / TD3 Policies</span></div>
                <div style="display:flex; align-items:center; gap:12px;"><div style="width:8px; height:8px; border-radius:50%; background:#22c55e;"></div><span style="font-family:'DM Mono',monospace; font-size:13px; color:#cbd5e1;">GPS-based station routing</span></div>
                <div style="display:flex; align-items:center; gap:12px;"><div style="width:8px; height:8px; border-radius:50%; background:#22c55e;"></div><span style="font-family:'DM Mono',monospace; font-size:13px; color:#cbd5e1;">Driver skill-aware recommendations</span></div>
                <div style="display:flex; align-items:center; gap:12px;"><div style="width:8px; height:8px; border-radius:50%; background:#22c55e;"></div><span style="font-family:'DM Mono',monospace; font-size:13px; color:#cbd5e1;">RBAC — Owner and Driver roles</span></div>
            </div>
        </div>
        """, unsafe_allow_html=True)

    with right:
        st.markdown("<div style='margin-top:20px;'>", unsafe_allow_html=True)

        # ── ROLE SELECT ──
        if st.session_state.auth_stage == "role_select":
            st.markdown("""
            <div style="padding:8px 0 24px 0;">
                <div style="font-family:'Barlow Condensed',sans-serif; font-size:32px; font-weight:800; color:#0f172a; text-transform:uppercase; letter-spacing:0.5px;">Select Portal</div>
                <div style="font-family:'DM Sans',sans-serif; font-size:14px; color:#64748b; margin-top:6px;">Choose your access level to continue.</div>
            </div>
            """, unsafe_allow_html=True)

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("""
                <div style="background:#fff7ed; border:2px solid #fb923c; border-radius:12px; padding:24px 18px; text-align:center; margin-bottom:8px;">
                    <div style="font-family:'DM Mono',monospace; font-size:28px; color:#ea580c; margin-bottom:8px;">&#9881;</div>
                    <div style="font-family:'Barlow Condensed',sans-serif; font-size:18px; font-weight:700; color:#9a3412; text-transform:uppercase;">Owner</div>
                    <div style="font-family:'DM Sans',sans-serif; font-size:12px; color:#c2410c; margin-top:4px;">Admin Control</div>
                </div>
                """, unsafe_allow_html=True)
                if st.button("Login as Owner", use_container_width=True, key="btn_owner"):
                    st.session_state.auth_stage = "owner_login"; st.rerun()
            with c2:
                st.markdown("""
                <div style="background:#eff6ff; border:2px solid #60a5fa; border-radius:12px; padding:24px 18px; text-align:center; margin-bottom:8px;">
                    <div style="font-family:'DM Mono',monospace; font-size:28px; color:#2563eb; margin-bottom:8px;">&#9889;</div>
                    <div style="font-family:'Barlow Condensed',sans-serif; font-size:18px; font-weight:700; color:#1e40af; text-transform:uppercase;">Driver</div>
                    <div style="font-family:'DM Sans',sans-serif; font-size:12px; color:#1d4ed8; margin-top:4px;">User Portal</div>
                </div>
                """, unsafe_allow_html=True)
                if st.button("Login as Driver", use_container_width=True, key="btn_driver"):
                    st.session_state.auth_stage = "user_auth"; st.rerun()

            st.markdown("---")

        # ── OWNER LOGIN ──
        elif st.session_state.auth_stage == "owner_login":
            st.markdown("""
            <div style="padding:8px 0 24px 0;">
                <div style="font-family:'DM Mono',monospace; font-size:11px; color:#ea580c; letter-spacing:3px; text-transform:uppercase; margin-bottom:6px;">Admin Access</div>
                <div style="font-family:'Barlow Condensed',sans-serif; font-size:32px; font-weight:800; color:#0f172a; text-transform:uppercase;">Owner Login</div>
            </div>
            """, unsafe_allow_html=True)
            with st.form("owner_form", clear_on_submit=False):
                u = st.text_input("Username", placeholder="Enter admin username")
                p = st.text_input("Password", type="password", placeholder="Enter password")
                if st.form_submit_button("Login to Admin Panel", use_container_width=True):
                    role = check_login(u, p)
                    if role == "Owner":
                        st.session_state.update(is_logged_in=True, username=u, role=role, auth_stage="done")
                        st.rerun()
                    else:
                        st.error("Invalid credentials or not an Owner account.")
            if st.button("Back to Portal Select", use_container_width=True):
                st.session_state.auth_stage = "role_select"; st.rerun()

        # ── USER LOGIN / SIGNUP ──
        elif st.session_state.auth_stage == "user_auth":
            st.markdown("""
            <div style="padding:8px 0 24px 0;">
                <div style="font-family:'DM Mono',monospace; font-size:11px; color:#1d4ed8; letter-spacing:3px; text-transform:uppercase; margin-bottom:6px;">Driver Access</div>
                <div style="font-family:'Barlow Condensed',sans-serif; font-size:32px; font-weight:800; color:#0f172a; text-transform:uppercase;">Driver Portal</div>
            </div>
            """, unsafe_allow_html=True)

            t_login, t_signup = st.tabs(["Login", "Create Account"])

            with t_login:
                with st.form("user_login_form", clear_on_submit=False):
                    u = st.text_input("Username", placeholder="Enter your username")
                    p = st.text_input("Password", type="password", placeholder="Enter your password")
                    if st.form_submit_button("Login to Driver Portal", use_container_width=True):
                        role = check_login(u, p)
                        if role == "User":
                            record = _get_user_record(u)
                            # Sync record into in-memory store for rest of session
                            if record and st.session_state.get("pg_available"):
                                st.session_state.user_db[u] = dict(record)
                            elif record:
                                pass  
                            # Block ICE/HYBRID vehicles
                            vtype = (record or {}).get("vehicle_type", "UNKNOWN")
                            if vtype in ("ICE", "HYBRID"):
                                vdisplay = (record or {}).get("car_model", "your vehicle")
                                st.error(
                                    f"Access Denied — '{vdisplay}' is not an electric vehicle. "
                                    "Please contact support or register with a valid EV."
                                )
                            else:
                                st.session_state.update(
                                    is_logged_in=True, username=u, role=role,
                                    driver_skill=(record or {}).get("skill", "Intermediate"),
                                    auth_stage="done",
                                    stations_prefetched=False,
                                )
                                st.rerun()
                        else:
                            st.error("Invalid credentials or not a Driver account.")

            with t_signup:
                # Build sorted dropdown options
                ev_options    = []
                other_options = []
                for db_key, specs_item in sorted(VEHICLE_DB.items(), key=lambda x: x[1]["display"]):
                    label = specs_item["display"]
                    vt    = specs_item["type"]
                    if vt in ("BEV", "PHEV"):
                        ev_options.append(label)
                    elif vt == "UNKNOWN_EV":
                        ev_options.append(label)
                    else:
                        other_options.append(f"{label}")

                dropdown_options = (
                    ["— Select your vehicle —"]
                    + ["── Electric Vehicles (Allowed) ──────────────"]
                    + ev_options
                    + ["── Non-EV Vehicles (Blocked) ─────────────────"]
                    + other_options
                )

                st.markdown("**Vehicle Details**")
                selected_vehicle = st.selectbox(
                    "Select Car Model *",
                    options=dropdown_options,
                    key="signup_car_dropdown",
                    help="Select your vehicle. Only Battery Electric Vehicles (BEV) and Plug-in Hybrids (PHEV) can register.",
                )

                # Determine the car_model_preview from dropdown selection
                is_separator = (
                    selected_vehicle.startswith("—") or
                    selected_vehicle.startswith("──")
                )
                car_model_preview = "" if is_separator else selected_vehicle

                # Live specs card shown immediately when a real vehicle is selected
                if car_model_preview:
                    ev_ok_prev, reason_prev, specs_prev = is_ev_vehicle(car_model_preview)
                    vtype = specs_prev["type"]
                    vdisp = specs_prev["display"]

                    if vtype == "ICE":
                        st.markdown(
                            f'''<div style="background:#fef2f2; border:1.5px solid #fca5a5;
                                border-left:5px solid #dc2626; border-radius:8px;
                                padding:12px 16px; margin:4px 0 12px 0;">
                              <div style="font-family:Barlow Condensed,sans-serif; font-size:15px;
                                          font-weight:700; color:#dc2626; text-transform:uppercase;">
                                  ⛽ Petrol/Diesel Vehicle — Not Allowed
                              </div>
                              <div style="font-size:13px; color:#7f1d1d; margin-top:4px;">
                                  <b>{vdisp}</b> runs on petrol or diesel and cannot use EV charging stations.
                                  Please select a Battery Electric Vehicle (BEV) or Plug-in Hybrid (PHEV).
                              </div>
                            </div>''',
                            unsafe_allow_html=True
                        )
                    elif vtype == "HYBRID":
                        st.markdown(
                            f'''<div style="background:#fef3c7; border:1.5px solid #fcd34d;
                                border-left:5px solid #d97706; border-radius:8px;
                                padding:12px 16px; margin:4px 0 12px 0;">
                              <div style="font-family:Barlow Condensed,sans-serif; font-size:15px;
                                          font-weight:700; color:#d97706; text-transform:uppercase;">
                                  ⚠ Non-Plug Hybrid — Not Allowed
                              </div>
                              <div style="font-size:13px; color:#78350f; margin-top:4px;">
                                  <b>{vdisp}</b> has no charging port. Only plug-in vehicles are permitted.
                              </div>
                            </div>''',
                            unsafe_allow_html=True
                        )
                    elif vtype in ("BEV", "PHEV") and specs_prev["found"]:
                        bkwh = specs_prev["battery_kwh"]
                        vv   = specs_prev["voltage_v"]
                        vt_  = get_voltage_tier(vv)
                        ac_  = specs_prev["max_ac_kw"]
                        dc_  = specs_prev["max_dc_kw"]
                        st.markdown(
                            f'''<div style="background:#f0fdf4; border:1.5px solid #86efac;
                                border-left:5px solid #16a34a; border-radius:8px;
                                padding:14px 18px; margin:4px 0 12px 0;">
                              <div style="font-family:Barlow Condensed,sans-serif; font-size:15px;
                                          font-weight:700; color:#15803d; text-transform:uppercase;
                                          margin-bottom:10px;">
                                  ⚡ EV Confirmed - Specs Auto-Filled
                              </div>
                              <div style="display:flex; gap:20px; flex-wrap:wrap;">
                                <div>
                                  <span style="font-family:DM Mono,monospace; font-size:20px;
                                               font-weight:600; color:#0f172a;">{bkwh} kWh</span><br>
                                  <span style="font-size:11px; color:#64748b; text-transform:uppercase;
                                               letter-spacing:1px;">Battery</span>
                                </div>
                                <div>
                                  <span style="font-family:DM Mono,monospace; font-size:20px;
                                               font-weight:600; color:#0f172a;">{vv}V</span><br>
                                  <span style="font-size:11px; color:#64748b; text-transform:uppercase;
                                               letter-spacing:1px;">{vt_}</span>
                                </div>
                                <div>
                                  <span style="font-family:DM Mono,monospace; font-size:20px;
                                               font-weight:600; color:#0369a1;">{ac_} kW AC</span><br>
                                  <span style="font-size:11px; color:#64748b; text-transform:uppercase;
                                               letter-spacing:1px;">Max AC Charge</span>
                                </div>
                                <div>
                                  <span style="font-family:DM Mono,monospace; font-size:20px;
                                               font-weight:600; color:#16a34a;">{dc_} kW DC</span><br>
                                  <span style="font-size:11px; color:#64748b; text-transform:uppercase;
                                               letter-spacing:1px;">Max DC Fast</span>
                                </div>
                              </div>
                            </div>''',
                            unsafe_allow_html=True
                        )
                    elif vtype == "UNKNOWN_EV":
                        st.markdown(
                            '''<div style="background:#eff6ff; border:1.5px solid #93c5fd;
                                border-left:5px solid #3b82f6; border-radius:8px;
                                padding:12px 16px; margin:4px 0 12px 0;">
                              <div style="font-family:Barlow Condensed,sans-serif; font-size:15px;
                                          font-weight:700; color:#1d4ed8; text-transform:uppercase;">
                                  ⚡ EV Detected — Specs Not in Database
                              </div>
                              <div style="font-size:13px; color:#1e3a5f; margin-top:4px;">
                                  Registration will proceed — specs will need manual verification.
                              </div>
                            </div>''',
                            unsafe_allow_html=True
                        )

                with st.form("signup_form", clear_on_submit=False):
                    st.markdown("**Account Details**")
                    col_a, col_b = st.columns(2)
                    nu    = col_a.text_input("Username *", placeholder="Choose a username")
                    np_   = col_b.text_input("Password * (min 6 chars)", type="password", placeholder="Min 6 characters")
                    phone = col_a.text_input("Phone Number *", placeholder="e.g. 9876543210")
                    email = col_b.text_input("Email Address *", placeholder="you@example.com")
                    # Number plate — Indian standard format with live format hint
                    car_plate_raw = st.text_input(
                        "Number Plate *",
                        placeholder="e.g. TN01AB1234 or MH02CD5678",
                        help="Indian vehicle plate: 2-letter state code + 2-digit district + 1-3 letter series + 4-digit number. E.g. TN01AB1234"
                    )
                    # Live plate format feedback
                    if car_plate_raw.strip():
                        _pok, _pnorm, _perr = validate_indian_plate(car_plate_raw)
                        if _pok:
                            st.markdown(
                                f'<div style="font-family:DM Mono,monospace; font-size:12px; color:#15803d; margin-top:-8px; margin-bottom:4px;">✓ Valid plate: <b>{_pnorm}</b></div>',
                                unsafe_allow_html=True
                            )
                        else:
                            st.markdown(
                                f'<div style="font-size:12px; color:#dc2626; margin-top:-8px; margin-bottom:4px;">✗ {_perr}</div>',
                                unsafe_allow_html=True
                            )
                    car_plate = car_plate_raw
                    st.markdown("**Driver Profile**")
                    skill = st.selectbox("Skill Level *", list(DRIVER_PROFILES.keys()))
                    st.caption(DRIVER_PROFILES[skill]["desc"])
                    submitted = st.form_submit_button("Create Account", use_container_width=True)
                    if submitted:
                        if not all([nu, np_, phone, email, car_model_preview, car_plate]):
                            st.error("All fields marked * are required. Make sure you selected a vehicle above.")
                        elif is_separator or not car_model_preview:
                            st.error("Please select a valid vehicle from the dropdown.")
                        else:
                            ok, msg, reg_specs = signup_user(nu, np_, skill, car_model_preview, car_plate, phone, email)
                            if ok:
                                rtype = reg_specs.get("type", "UNKNOWN") if reg_specs else "UNKNOWN"
                                if rtype in ("BEV", "PHEV") and reg_specs.get("found"):
                                    rdisplay  = reg_specs.get("display", car_model_preview)
                                    rvoltage  = reg_specs.get("voltage_v")
                                    rbattery  = reg_specs.get("battery_kwh")
                                    st.success(
                                        f"Account created! {rdisplay} — "
                                        f"{rvoltage}V / {rbattery} kWh saved. Please login."
                                    )
                                else:
                                    st.success("Account created. Please login.")
                            else:
                                st.error(msg)

            st.markdown("")
            if st.button("Back to Portal Select", use_container_width=True):
                st.session_state.auth_stage = "role_select"; st.rerun()

        st.markdown("</div>", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════
# DASHBOARD (Logged In)
# ═══════════════════════════════════════════════════════
else:
    dynamic_price, multiplier, state = get_dynamic_price(st.session_state.active_policy)

    # ── SIDEBAR ──
    with st.sidebar:
        st.markdown(f"**User:** {st.session_state.username}")
        record = st.session_state.user_db.get(st.session_state.username, {})
        if st.session_state.role == "Owner":
            st.markdown('<span class="badge-owner">OWNER</span>', unsafe_allow_html=True)
        else:
            st.markdown(f'<span class="badge-skill">{st.session_state.driver_skill}</span>', unsafe_allow_html=True)
            if record.get("car_model"):
                st.markdown(f"Vehicle: **{record['car_model']}**")
            if record.get("car_plate"):
                st.markdown(f"Plate: **{record['car_plate']}**")
            if st.session_state.gps_lat:
                st.markdown(f"GPS: `{st.session_state.gps_lat:.4f}, {st.session_state.gps_lon:.4f}`")
            # EV specs panel in sidebar
            vtype = record.get("vehicle_type", "UNKNOWN")
            batt  = record.get("battery_kwh")
            volt  = record.get("voltage_v")
            ac_kw = record.get("max_ac_kw")
            dc_kw = record.get("max_dc_kw")
            vtier = record.get("voltage_tier", "")
            if batt and volt:
                confirmed = record.get("specs_confirmed", False)
                badge_txt = "✓ Verified" if confirmed else "~ Estimated"
                badge_col = "#15803d" if confirmed else "#b45309"
                st.markdown(f"""
                <div style="background:#f0fdf4; border:1px solid #86efac; border-radius:8px;
                            padding:10px 12px; margin-top:8px;">
                    <div style="font-family:'DM Mono',monospace; font-size:10px; color:{badge_col};
                                text-transform:uppercase; letter-spacing:1px; margin-bottom:6px;">
                        ⚡ EV Specs {badge_txt}
                    </div>
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px;">
                        <div><span style="font-size:16px; font-weight:700; font-family:'DM Mono',monospace;">{batt} kWh</span><br><span style="font-size:10px; color:#64748b;">Battery</span></div>
                        <div><span style="font-size:16px; font-weight:700; font-family:'DM Mono',monospace;">{volt}V</span><br><span style="font-size:10px; color:#64748b;">Voltage</span></div>
                        <div><span style="font-size:16px; font-weight:700; font-family:'DM Mono',monospace;">{ac_kw} kW</span><br><span style="font-size:10px; color:#64748b;">AC Max</span></div>
                        <div><span style="font-size:16px; font-weight:700; font-family:'DM Mono',monospace;">{dc_kw} kW</span><br><span style="font-size:10px; color:#64748b;">DC Fast</span></div>
                    </div>
                    <div style="margin-top:6px; font-size:10px; color:#475569; font-family:'DM Mono',monospace;">{vtier}</div>
                </div>""", unsafe_allow_html=True)
        st.markdown("---")
        if st.button("Logout", use_container_width=True):
            for k in ["is_logged_in", "username", "role", "auth_stage", "driver_skill",
                      "gps_lat", "gps_lon", "gps_source", "stations_prefetched"]:
                if k == "is_logged_in":       st.session_state[k] = False
                elif k == "auth_stage":       st.session_state[k] = "role_select"
                elif k == "stations_prefetched": st.session_state[k] = False
                elif k in ["gps_lat", "gps_lon", "gps_source"]: st.session_state[k] = None
                else:                         st.session_state[k] = None
            st.rerun()

    # ═══════════════════════════════════
    #  OWNER DASHBOARD
    # ═══════════════════════════════════
    if st.session_state.role == "Owner":
        st.markdown('<div class="section-label">Admin Control Panel</div>', unsafe_allow_html=True)

        tab_policy, tab_compare, tab_live, tab_stations, tab_users = st.tabs([
            "Policy Management", "Price Comparison", "Live Agent", "Station Map", "Users"
        ])

        with tab_policy:
            st.markdown('<div class="section-label">Deploy and Configure RL Policies</div>', unsafe_allow_html=True)

            for policy in MODEL_PATHS:
                with st.expander(f"{policy} Policy", expanded=(policy == st.session_state.active_policy)):
                    c1, c2, c3 = st.columns([2, 1.5, 1])
                    c1.markdown(f"**Model file:** `{MODEL_PATHS[policy]}`")
                    m_loaded = _model_cache.get(policy) is not None
                    if policy not in _model_cache:
                        c2.markdown("**Status:** ⟳ Loading...")
                    elif m_loaded:
                        c2.markdown("**Status:** ● Loaded ✓")
                    else:
                        c2.markdown("**Status:** ○ Not found (ToU fallback)")
                    enabled = c3.toggle("Enabled", value=st.session_state.policy_enabled[policy], key=f"toggle_{policy}")
                    st.session_state.policy_enabled[policy] = enabled

                    note = st.text_area("Admin Notes", value=st.session_state.policy_notes[policy], key=f"note_{policy}", height=60)
                    st.session_state.policy_notes[policy] = note

                    if enabled:
                        if st.button(f"Deploy {policy} as Active Policy", key=f"deploy_{policy}", use_container_width=True):
                            st.session_state.active_policy = policy
                            st.success(f"{policy} is now the active pricing policy.")
                            time.sleep(0.4); st.rerun()
                    else:
                        st.warning(f"{policy} is disabled and cannot be deployed.")

            st.markdown("---")
            col_a, col_b = st.columns(2)
            col_a.metric("Base Price", f"Rs {BASE_PRICE_PER_KWH:.2f}/kWh")
            col_b.metric("Multiplier Bounds", "0.80x to 1.50x")
            col_a.metric("State Dimensions", "7")
            col_b.metric("Action Space", "Continuous 1D")
            col_a.metric("Pricing Fallback", "Time-of-Use (ToU)")

        with tab_compare:
            st.markdown(f'<div class="section-label">24-Hour Price Simulation — {st.session_state.active_policy}</div>', unsafe_allow_html=True)
            df_prices = simulate_daily_prices(st.session_state.active_policy)
            plot_pricing_comparison(df_prices)
            c1, c2, c3 = st.columns(3)
            c1.metric("Static Pricing", f"Rs {BASE_PRICE_PER_KWH:.2f}/kWh")
            c2.metric("ToU Average",    f"Rs {df_prices['Time-of-Use (ToU) Pricing'].mean():.2f}/kWh")
            c3.metric("RL Average",     f"Rs {df_prices['Dynamic (RL) Pricing'].mean():.2f}/kWh",
                      delta=f"{df_prices['Dynamic (RL) Pricing'].mean() - BASE_PRICE_PER_KWH:+.2f}")
            st.dataframe(df_prices.reset_index().style.format("{:.2f}", subset=df_prices.columns.tolist()), use_container_width=True)

        with tab_live:
            st.markdown('<div class="section-label">Real-Time Agent Decision</div>', unsafe_allow_html=True)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Time",               pd.Timestamp.now().strftime("%I:%M %p"))
            c2.metric("Base Price",         f"Rs {BASE_PRICE_PER_KWH}/kWh")
            c3.metric(f"{st.session_state.active_policy} Multiplier", f"x{multiplier:.3f}")
            c4.metric("Live Dynamic Price", f"Rs {dynamic_price}/kWh")

            m = get_model(st.session_state.active_policy)
            if m is None:
                st.info("RL model not loaded — pricing uses Time-of-Use rules. Station data and routing work normally.")

            st.markdown("#### Observation Vector")
            obs_df = pd.DataFrame({
                "Feature": ["Utilisation", "Traffic", "Weekend", "Rainfall", "Temperature", "Queue", "Energy"],
                "Value":   [round(float(v), 4) for v in state],
            })
            st.dataframe(obs_df.set_index("Feature"), use_container_width=True)
            st.info(f"The {st.session_state.active_policy} agent produced multiplier {multiplier:.3f} → Rs {dynamic_price}/kWh.")

        with tab_stations:
            st.markdown('<div class="section-label">Global Station Overview</div>', unsafe_allow_html=True)
            ref_lat, ref_lon = 20.5937, 78.9629
            all_st = []
            random.seed(42)
            for s in STATION_TEMPLATES:
                for offset_lat, offset_lon, city_tag in [(8.0, -1.5, "S"), (-8.0, -1.5, "N")]:
                    all_st.append({
                        "Station Name":   f"{s['name']} ({city_tag})",
                        "lat":            ref_lat + offset_lat + s["lat_offset"],
                        "lon":            ref_lon + offset_lon + s["lon_offset"],
                        "Price (Rs/kWh)": round(max(10, dynamic_price + s["price_offset"]), 2),
                        "Status":         random.choice(["Available", "Busy", "Available", "Offline"]),
                        "charger_kw":     s["charger_kw"],
                        "distance_km":    0,
                    })
            fig_all = build_station_map(pd.DataFrame(all_st), ref_lat, ref_lon, zoom=4)
            st.plotly_chart(fig_all, use_container_width=True, config={"scrollZoom": True})
            st.dataframe(pd.DataFrame(all_st).drop(columns=["lat", "lon", "distance_km"]), use_container_width=True)

        with tab_users:
            st.markdown('<div class="section-label">Registered Drivers</div>', unsafe_allow_html=True)

            # Storage badge
            db_badge_bg  = "#f0fdf4" if st.session_state.get("pg_available") else "#fef3c7"
            db_badge_bdr = "#86efac" if st.session_state.get("pg_available") else "#fcd34d"
            db_badge_col = "#15803d" if st.session_state.get("pg_available") else "#b45309"
            db_badge_txt = "● PostgreSQL — Live Data" if st.session_state.get("pg_available") else "○ In-Memory (add PostgreSQL in secrets.toml)"
            st.markdown(
                f'''<div style="display:inline-flex;align-items:center;gap:8px;
                    background:{db_badge_bg};border:1px solid {db_badge_bdr};border-radius:6px;
                    padding:5px 12px;margin-bottom:14px;font-family:DM Mono,monospace;
                    font-size:12px;color:{db_badge_col};">{db_badge_txt}</div>''',
                unsafe_allow_html=True
            )

            all_users_src = (
                _pg_get_all_users()
                if st.session_state.get("pg_available")
                else list(st.session_state.user_db.values())
            )
            # Only include actual User accounts (not Owners)
            drivers = [d for d in all_users_src if d.get("role","") == "User"]

            rows = []
            ev_count = blocked_count = 0
            for data in drivers:
                vtype = data.get("vehicle_type", "UNKNOWN")
                is_blocked = vtype in ("ICE", "HYBRID")
                if is_blocked:
                    blocked_count += 1
                if vtype in ("BEV", "PHEV", "UNKNOWN_EV"):
                    ev_count += 1

                bkwh = data.get("battery_kwh")
                vv   = data.get("voltage_v")
                rows.append({
                    "Username":      data.get("username", "?"),
                    "Skill":         data.get("skill", "-"),
                    "Car Model":     data.get("car_model", "-"),
                    "Vehicle Type":  vtype,
                    "Battery (kWh)": bkwh if bkwh else "-",
                    "Voltage":       f"{vv}V" if vv else "-",
                    "Max AC (kW)":   data.get("max_ac_kw", "-"),
                    "Max DC (kW)":   data.get("max_dc_kw", "-"),
                    "Voltage Tier":  data.get("voltage_tier", "-"),
                    "Access":        "🚫 Blocked" if is_blocked else "✅ Active",
                })

            c1, c2, c3 = st.columns(3)
            c1.metric("Total Drivers", len(drivers))
            c2.metric("EV Users",      ev_count)
            c3.metric("Blocked",       blocked_count)

            if rows:
                st.dataframe(
                    pd.DataFrame(rows),
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Access":        st.column_config.TextColumn(width="small"),
                        "Vehicle Type":  st.column_config.TextColumn(width="small"),
                        "Battery (kWh)": st.column_config.NumberColumn(format="%.1f kWh"),
                        "Max AC (kW)":   st.column_config.NumberColumn(format="%.1f kW"),
                        "Max DC (kW)":   st.column_config.NumberColumn(format="%.0f kW"),
                    }
                )
            else:
                st.info("No drivers registered yet.")

            if blocked_count > 0:
                st.warning(f"{blocked_count} driver(s) blocked — non-EV vehicles registered.")



    # ═══════════════════════════════════
    #  USER DASHBOARD
    # ═══════════════════════════════════
    elif st.session_state.role == "User":
        record       = st.session_state.user_db.get(st.session_state.username, {})
        driver_skill = st.session_state.driver_skill
        profile      = DRIVER_PROFILES[driver_skill]

        st.markdown(f'<div class="section-label">Driver Dashboard — {st.session_state.username}</div>', unsafe_allow_html=True)
        st.markdown(
            f'<span class="badge-skill">{driver_skill}</span>&nbsp;&nbsp;'
            f'<span style="color:#475569; font-size:14px;">{profile["desc"]}</span>',
            unsafe_allow_html=True
        )
        st.markdown("")

        # GPS LOCATION SECTION
        st.markdown('<div class="section-label">Your Location</div>', unsafe_allow_html=True)

        # Prefetch OCM stations as soon as coords are known 
        if st.session_state.gps_lat is not None and not st.session_state.stations_prefetched:
            prefetch_stations_async(st.session_state.gps_lat, st.session_state.gps_lon, dynamic_price)
            st.session_state.stations_prefetched = True


        gps_widget_html = """
        <style>
        .gw{background:linear-gradient(135deg,#eff6ff,#dbeafe);
            border:1.5px solid #93c5fd;border-left:5px solid #1d4ed8;
            border-radius:10px;padding:16px 20px;}
        .gw-title{font-weight:700;color:#1d4ed8;font-size:12px;
                  text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;}
        .gw-row{display:flex;align-items:center;gap:10px;font-size:13px;color:#475569;}
        .gw-coords{margin-top:10px;padding:10px 14px;background:#fff;
                   border:1px solid #bfdbfe;border-radius:6px;
                   font-family:DM Mono,monospace;font-size:12px;
                   color:#1e293b;display:none;}
        .gw-ok{color:#15803d;font-weight:700;font-size:12px;margin-top:6px;}
        .gw-err{color:#dc2626;font-weight:600;font-size:12px;}
        @keyframes spin{to{transform:rotate(360deg)}}
        .spin{display:inline-block;width:13px;height:13px;
              border:2px solid #bfdbfe;border-top-color:#1d4ed8;
              border-radius:50%;animation:spin .7s linear infinite;}
        .rbtn{margin-top:10px;padding:8px 18px;background:#0f172a;
              color:#fff;border:none;border-radius:5px;font-size:12px;
              font-weight:700;cursor:pointer;text-transform:uppercase;
              letter-spacing:.5px;display:none;}
        .rbtn:hover{background:#0ea5e9;}
        </style>

        <div class="gw">
          <div class="gw-title">Automatic Location Detection</div>
          <div class="gw-row">
            <span class="spin" id="spin"></span>
            <span id="msg">Detecting your location…</span>
          </div>
          <div class="gw-coords" id="coords"></div>
          <button class="rbtn" id="rbtn" onclick="detect()">↺ Retry</button>
        </div>

        <script>
        (function(){
          var msg    = document.getElementById('msg');
          var spin   = document.getElementById('spin');
          var coords = document.getElementById('coords');
          var rbtn   = document.getElementById('rbtn');

          function sendToStreamlit(lat, lon) {
            // Write coords into the parent window's URL query params
            // using history.replaceState (no navigation = session preserved)
            // then post a message that our polling fragment will detect
            // via the Streamlit query_params mechanism.
            try {
              var url = new URL(window.parent.location.href);
              url.searchParams.set('gps_lat', lat);
              url.searchParams.set('gps_lon', lon);
              window.parent.history.replaceState(null, '', url.toString());
            } catch(e) {}

            // Also store in sessionStorage as fallback
            try {
              window.parent.sessionStorage.setItem('evee_gps', lat + ',' + lon);
            } catch(e) {}
          }

          function detect() {
            if (!navigator.geolocation) {
              spin.style.display = 'none';
              msg.innerHTML = '<span class="gw-err">Geolocation not supported.</span>';
              return;
            }
            spin.style.display = 'inline-block';
            msg.textContent    = 'Detecting your location…';
            coords.style.display = 'none';
            rbtn.style.display   = 'none';

            navigator.geolocation.getCurrentPosition(
              function(pos) {
                var lat = pos.coords.latitude.toFixed(6);
                var lon = pos.coords.longitude.toFixed(6);
                var acc = Math.round(pos.coords.accuracy);
                spin.style.display   = 'none';
                msg.textContent      = '';
                coords.style.display = 'block';
                coords.innerHTML     =
                  '<b>Lat:</b> '+lat+'   <b>Lon:</b> '+lon+
                  '   <b>Accuracy:</b> '+acc+' m'+
                  '<div class="gw-ok">✓ Location captured — map loading…</div>';
                sendToStreamlit(lat, lon);
              },
              function(err) {
                var m = {1:'Permission denied — enable location in browser settings then click Retry.',
                         2:'Location signal unavailable.',
                         3:'Request timed out.'};
                spin.style.display = 'none';
                msg.innerHTML = '<span class="gw-err">'+(m[err.code]||err.message)+'</span>';
                rbtn.style.display = 'inline-block';
              },
              {enableHighAccuracy:true, timeout:60000, maximumAge:60000}
            );
          }

          detect();
        })();
        </script>
        """
        st.components.v1.html(gps_widget_html, height=175, scrolling=False)


        @st.fragment(run_every=1)
        def _gps_poller():
            """Runs every 1 s. Reads URL query params set by JS replaceState."""
            if st.session_state.gps_lat is not None:
                return  
            qp = st.query_params
            if "gps_lat" in qp and "gps_lon" in qp:
                try:
                    new_lat = float(qp["gps_lat"])
                    new_lon = float(qp["gps_lon"])
                    st.session_state.gps_lat             = new_lat
                    st.session_state.gps_lon             = new_lon
                    st.session_state.gps_source          = "browser"
                    st.session_state.stations_prefetched = False
                    st.query_params.clear()
                    st.rerun()  # full rerun now that we have coords
                except Exception:
                    pass

        _gps_poller()

        if st.session_state.gps_lat is None:
            st.info("⏳ Detecting location — the map will load automatically once GPS is ready.")
            st.stop()

        user_lat = st.session_state.gps_lat
        user_lon = st.session_state.gps_lon

        st.markdown(f"""
        <div style="background:#f0fdf4;border:1.5px solid #86efac;border-left:5px solid #16a34a;
                    border-radius:8px;padding:10px 18px;margin-bottom:12px;
                    display:flex;align-items:center;gap:16px;">
            <span style="font-family:'DM Mono',monospace;font-size:12px;color:#15803d;
                         font-weight:700;text-transform:uppercase;">✓ GPS</span>
            <span style="font-family:'DM Mono',monospace;font-size:13px;color:#1e293b;">
                {user_lat:.6f}, {user_lon:.6f}
            </span>
            <span style="font-size:12px;color:#64748b;margin-left:auto;">
                Location confirmed — map loaded
            </span>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("---")

        col_sk, col_ref = st.columns([3, 1])
        new_skill = col_sk.selectbox("Driver Skill Level", list(DRIVER_PROFILES.keys()),
                                     index=list(DRIVER_PROFILES.keys()).index(driver_skill))
        if col_ref.button("Refresh", use_container_width=True):
            st.rerun()
        if new_skill != driver_skill:
            st.session_state.driver_skill = new_skill
            st.session_state.user_db[st.session_state.username]["skill"] = new_skill
            if st.session_state.get("pg_available"):
                _pg_update_skill(st.session_state.username, new_skill)
            st.rerun()

        st.markdown("---")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Live Price",   f"Rs {dynamic_price}/kWh")
        m2.metric("Active Agent", st.session_state.active_policy)
        m3.metric("Skill Level",  driver_skill)
        m4.metric("Time",         pd.Timestamp.now().strftime("%I:%M %p"))

        # ── EV VEHICLE SPECS CARD ──────────────────────────────
        rec_batt   = record.get("battery_kwh")
        rec_volt   = record.get("voltage_v")
        rec_ac     = record.get("max_ac_kw")
        rec_dc     = record.get("max_dc_kw")
        rec_vtier  = record.get("voltage_tier", "")
        rec_vtype  = record.get("vehicle_type", "UNKNOWN")
        rec_conf   = record.get("specs_confirmed", False)

        if rec_batt and rec_volt:
            # Charger compatibility indicator based on voltage tier
            if rec_volt >= 700:
                compat_msg = "✓ Compatible with ultra-fast 800V DC chargers (350 kW)"
                compat_col = "#15803d"
                compat_bg  = "#f0fdf4"
            elif rec_volt >= 350:
                compat_msg = "✓ Compatible with standard 400V DC fast chargers (up to 150 kW)"
                compat_col = "#1d4ed8"
                compat_bg  = "#eff6ff"
            elif rec_volt >= 60:
                compat_msg = "✓ Compatible with AC slow chargers (2-wheeler / low-voltage)"
                compat_col = "#7c3aed"
                compat_bg  = "#f5f3ff"
            else:
                compat_msg = "Voltage tier unknown — check charger compatibility manually"
                compat_col = "#64748b"
                compat_bg  = "#f8fafc"

            est_full_ac = round(rec_batt / rec_ac, 1) if rec_ac else "N/A"
            est_full_dc = round(rec_batt / rec_dc, 1) if rec_dc else "N/A"
            badge_label = "Verified Specs" if rec_conf else "Estimated Specs"
            badge_color_sp = "#15803d" if rec_conf else "#b45309"

            st.markdown(f"""
            <div style="background:#ffffff; border:1.5px solid #e2e8f0; border-left:6px solid #0ea5e9;
                        border-radius:10px; padding:18px 24px; margin:16px 0; display:flex; gap:32px;
                        flex-wrap:wrap; align-items:center;">
                <div>
                    <div style="font-family:'Barlow Condensed',sans-serif; font-size:16px; font-weight:700;
                                color:#0f172a; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:4px;">
                        ⚡ {record.get('car_model', 'Your EV')}
                        <span style="margin-left:10px; font-size:11px; font-weight:700;
                                     color:{badge_color_sp}; font-family:'DM Mono',monospace;
                                     text-transform:uppercase; letter-spacing:1px;">{badge_label}</span>
                    </div>
                    <div style="font-size:12px; color:{compat_col}; background:{compat_bg};
                                border-radius:4px; padding:4px 10px; display:inline-block;
                                font-family:'DM Mono',monospace; margin-top:2px;">{compat_msg}</div>
                </div>
                <div style="display:flex; gap:28px; flex-wrap:wrap; margin-left:auto;">
                    <div style="text-align:center;">
                        <div style="font-family:'DM Mono',monospace; font-size:22px; font-weight:600; color:#0f172a;">{rec_batt} kWh</div>
                        <div style="font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:1px;">Battery</div>
                    </div>
                    <div style="text-align:center;">
                        <div style="font-family:'DM Mono',monospace; font-size:22px; font-weight:600; color:#0f172a;">{rec_volt}V</div>
                        <div style="font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:1px;">{rec_vtier}</div>
                    </div>
                    <div style="text-align:center;">
                        <div style="font-family:'DM Mono',monospace; font-size:22px; font-weight:600; color:#0369a1;">{rec_ac} kW</div>
                        <div style="font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:1px;">Max AC · ~{est_full_ac}h full</div>
                    </div>
                    <div style="text-align:center;">
                        <div style="font-family:'DM Mono',monospace; font-size:22px; font-weight:600; color:#16a34a;">{rec_dc} kW</div>
                        <div style="font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:1px;">Max DC · ~{est_full_dc}h full</div>
                    </div>
                </div>
            </div>""", unsafe_allow_html=True)

        st.markdown("---")

        # Fetch stations
        with st.spinner("Loading nearby stations..."):
            station_df_raw, data_source = fetch_ocm_stations(user_lat, user_lon, dynamic_price)

        station_df = rank_stations(station_df_raw, driver_skill, dynamic_price, user_lat, user_lon)

        # Voltage compatibility filter
        user_voltage = record.get("voltage_v") or 0
        user_max_dc  = record.get("max_dc_kw") or 0
        if user_voltage > 0:

            def _compat_tag(row_kw):
                if user_voltage >= 700:
                    return "✓ Compatible" if row_kw >= 50 else "⚠ Slow (AC only)"
                elif user_voltage >= 200:
                    return "✓ Compatible" if row_kw <= 150 else "⚠ Over-spec (still works)"
                else: 
                    return "✓ Compatible" if row_kw <= 7.2 else "⚠ High-power (may not fit)"
            station_df["Compatibility"] = station_df["charger_kw"].apply(_compat_tag)
        else:
            station_df["Compatibility"] = "Unknown"

        top = station_df.iloc[0]

        is_live      = "Open Charge Map" in data_source
        badge_color  = "#15803d" if is_live else "#b45309"
        badge_bg     = "#dcfce7" if is_live else "#fef3c7"
        badge_border = "#86efac" if is_live else "#fcd34d"
        st.markdown(f"""
        <div style="display:inline-flex; align-items:center; gap:10px; background:{badge_bg};
                    border:1px solid {badge_border}; border-radius:6px; padding:6px 14px; margin-bottom:12px;">
            <span style="width:8px; height:8px; border-radius:50%; background:{badge_color}; display:inline-block;"></span>
            <span style="font-family:'DM Mono',monospace; font-size:12px; color:{badge_color}; font-weight:700;">
                {data_source} — {len(station_df)} stations found within 10 km
            </span>
        </div>
        """, unsafe_allow_html=True)

        top_operator  = top.get("Operator",  "")
        top_address   = top.get("Address",   "")
        top_connector = top.get("Connector", "")
        top_ocm_id    = top.get("OCM_ID",    "")
        ocm_link      = f"https://openchargemap.org/site/poi/details/{top_ocm_id}" if top_ocm_id else ""
        addr_line     = f'<div style="font-size:13px; color:#64748b; margin-top:2px;">{top_address}</div>' if top_address else ""

        st.markdown(f"""
        <div class="rec-card">
          <div class="rec-card-title">Best Station For You — {driver_skill} Profile</div>
          {addr_line}
          <div class="rec-card-body" style="margin-top:10px;">{route_narrative(driver_skill, top)}</div>
          <div style="display:flex; gap:28px; flex-wrap:wrap; margin-bottom:14px;">
            <div><span class="rec-stat-value">Rs {top["Price (Rs/kWh)"]}/kWh</span><span class="rec-stat-label">RL Dynamic Price</span></div>
            <div><span class="rec-stat-value">{top["charger_kw"]} kW</span><span class="rec-stat-label">Charger Speed</span></div>
            <div><span class="rec-stat-value">{top["distance_km"]} km</span><span class="rec-stat-label">Distance</span></div>
            <div><span class="rec-stat-value">{top["est_time_min"]} min</span><span class="rec-stat-label">Est. Travel Time</span></div>
            <div><span class="rec-stat-value">{top["Status"]}</span><span class="rec-stat-label">Status</span></div>
            <div><span class="rec-stat-value">{top["slots"]}</span><span class="rec-stat-label">Charge Points</span></div>
          </div>
          <div style="display:flex; gap:24px; flex-wrap:wrap; border-top:1px solid #bbf7d0; padding-top:10px; margin-top:4px;">
            <div style="font-size:12px; color:#475569;"><b>Operator:</b> {top_operator or "—"}</div>
            <div style="font-size:12px; color:#475569;"><b>Connector:</b> {top_connector or "—"}</div>
          </div>
        </div>
        """, unsafe_allow_html=True)

        nav_url = google_maps_url(user_lat, user_lon, top["lat"], top["lon"])
        btn_col1, btn_col2 = st.columns([3, 1])
        btn_col1.link_button(f"Navigate to {top['Station Name']} via Google Maps", url=nav_url, use_container_width=True)
        if ocm_link:
            btn_col2.link_button("View on OCM", url=ocm_link, use_container_width=True)

        st.markdown("---")

        t_map, t_all, t_price = st.tabs(["Station Map", "All Stations", "Price Trends"])

        with t_map:
            fig = build_station_map(station_df, user_lat, user_lon, zoom=13, top_station_name=top["Station Name"])
            st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})
            st.caption("GREEN = Recommended   |   RED = Other stations   |   BLUE = Your location")

        with t_all:
            st.markdown('<div class="section-label">All Nearby Stations — Ranked for Your Profile</div>', unsafe_allow_html=True)
            base_cols  = ["rank", "Station Name", "Price (Rs/kWh)", "charger_kw", "distance_km", "est_time_min", "Status", "slots", "Compatibility", "score"]
            extra_cols = [c for c in ["Operator", "Address", "Connector"] if c in station_df.columns]
            show_cols  = base_cols + extra_cols

            st.dataframe(
                station_df[show_cols].rename(columns={
                    "charger_kw": "Charger (kW)", "distance_km": "Dist (km)",
                    "est_time_min": "ETA (min)", "score": "Rank Score",
                }),
                use_container_width=True, hide_index=True,
                column_config={
                    "Station Name":    st.column_config.TextColumn(width="medium"),
                    "Operator":        st.column_config.TextColumn(width="medium"),
                    "Address":         st.column_config.TextColumn(width="large"),
                    "Connector":       st.column_config.TextColumn(width="medium"),
                    "Rank Score":      st.column_config.NumberColumn(format="%.3f"),
                    "Dist (km)":       st.column_config.NumberColumn(format="%.2f km"),
                    "Price (Rs/kWh)":  st.column_config.NumberColumn(format="Rs %.2f"),
                },
            )

            sel     = st.selectbox("Get directions to a specific station:", station_df["Station Name"].tolist())
            sel_row = station_df[station_df["Station Name"] == sel].iloc[0]
            d_col1, d_col2 = st.columns([3, 1])
            d_col1.link_button(f"Navigate to {sel} via Google Maps",
                               url=google_maps_url(user_lat, user_lon, sel_row["lat"], sel_row["lon"]),
                               use_container_width=True)
            sel_ocm = sel_row.get("OCM_ID", "")
            if sel_ocm:
                d_col2.link_button("OCM Details",
                                   url=f"https://openchargemap.org/site/poi/details/{sel_ocm}",
                                   use_container_width=True)

            with st.expander("How are stations ranked for your skill level?"):
                st.markdown(f"""
**Profile: {driver_skill}** — {profile['desc']}

| Factor | Weight |
|--------|--------|
| Price | {profile['price_weight']*100:.0f}% |
| Distance | {profile['distance_weight']*100:.0f}% |
| Charger speed | {(1-profile['price_weight']-profile['distance_weight'])*100:.0f}% |
| Min charger required | {profile['charger_min_kw']} kW |
                """)

        with t_price:
            st.markdown('<div class="section-label">Dynamic Pricing — 24 Hour View</div>', unsafe_allow_html=True)
            df_p = simulate_daily_prices(st.session_state.active_policy)
            plot_pricing_comparison(df_p)
            st.caption("RL agent adjusts prices continuously. Off-peak hours (midnight to 5am) offer the lowest rates.")
            current_hour = pd.Timestamp.now().hour
            savings = df_p.loc[current_hour, "Time-of-Use (ToU) Pricing"] - df_p.loc[current_hour, "Dynamic (RL) Pricing"]
            if savings > 0:
                st.success(f"At {current_hour}:00, RL pricing saves you Rs {savings:.2f}/kWh vs Time-of-Use.")
            else:
                st.info(f"Tip: Off-peak hours offer the lowest prices — as low as Rs {df_p['Dynamic (RL) Pricing'].min():.2f}/kWh.")

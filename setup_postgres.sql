-- ═══════════════════════════════════════════════════════════════
-- EVEE Dynamic Pricing Agent — PostgreSQL Setup
-- Run this ONCE to create the database and user.
-- Then add credentials to .streamlit/secrets.toml
-- ═══════════════════════════════════════════════════════════════

-- 1. Create database (run as postgres superuser)
CREATE DATABASE evee_db;

-- 2. Create app user (optional but recommended)
CREATE USER evee_user WITH PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE evee_db TO evee_user;

-- 3. Connect to evee_db then run the rest:
\c evee_db

-- 4. Users table
CREATE TABLE IF NOT EXISTS users (
    username        TEXT PRIMARY KEY,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'User',
    skill           TEXT DEFAULT 'Intermediate',
    car_model       TEXT,
    car_plate       TEXT UNIQUE,          -- UNIQUE enforces no duplicate plates
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

-- Index on car_plate for fast duplicate checks
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_plate
    ON users (UPPER(car_plate));

-- 5. Policy settings table
CREATE TABLE IF NOT EXISTS policy_settings (
    policy_name TEXT PRIMARY KEY,
    enabled     BOOLEAN DEFAULT TRUE,
    notes       TEXT DEFAULT ''
);

-- 6. Seed default admin account (password: adminpass)
INSERT INTO users (username, password_hash, role, phone, email)
VALUES (
    'owner',
    encode(sha256('adminpass'::bytea), 'hex'),
    'Owner', '', ''
) ON CONFLICT DO NOTHING;

-- 7. Seed demo driver account (password: userpass)
INSERT INTO users (
    username, password_hash, role, skill,
    car_model, car_plate, phone, email,
    vehicle_type, battery_kwh, voltage_v,
    max_ac_kw, max_dc_kw, voltage_tier, specs_confirmed
) VALUES (
    'rluser1',
    encode(sha256('userpass'::bytea), 'hex'),
    'User', 'Intermediate',
    'Tata Nexon EV', 'TN01AB1234', '9876543210', 'user@example.com',
    'BEV', 30.2, 320, 7.2, 50.0, '400V Fast', TRUE
) ON CONFLICT DO NOTHING;

-- ═══════════════════════════════════════════════════════════════
-- .streamlit/secrets.toml  (create this file in your project)
-- ═══════════════════════════════════════════════════════════════
-- [postgres]
-- host     = "localhost"
-- port     = 5432
-- dbname   = "evee_db"
-- user     = "evee_user"
-- password = "your_secure_password"
--
-- [OCM_API_KEY]
-- OCM_API_KEY = "your_ocm_api_key"
-- ═══════════════════════════════════════════════════════════════

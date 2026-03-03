#!/usr/bin/env python3
"""OpenWebRX Schedule Widget v2 - Con modi, bandwidth e decoder"""
from datetime import datetime, timedelta, timezone
import json, sys, os, shutil, html as html_lib

OPENWEBRX_URL = "http://192.168.1.132:8073"
LOG_FILE = "/var/www/html/sdr-transmissions-log.json"
EIBI_EVENTS_JSON = "/var/www/html/sdr-eibi-events.json"
SIGNAL_RATINGS_JSON = "/var/lib/openwebrx/signal_ratings.json"

def load_signal_ratings():
    """Carica le valutazioni automatiche dal database dell'orchestratore"""
    if os.path.exists(SIGNAL_RATINGS_JSON):
        try:
            with open(SIGNAL_RATINGS_JSON) as f:
                return json.load(f)
        except:
            pass
    return {}

def get_auto_rating(tx, ratings_db):
    """Recupera rating automatico per una trasmissione.
    Cerca prima per chiave esatta (freq|desc), poi aggrega per descrizione
    su tutte le frequenze della stessa stazione.
    Returns dict with: avg_score, consecutive_nr, enabled, total_ratings
    """
    # L'orchestratore usa la chiave: freq_mhz|description
    freq = str(tx.get('freq', tx.get('frequency_mhz', '0')))
    desc = tx.get('description', '?')
    key = "{}|{}".format(freq, desc)
    
    # 1. Exact match
    entry = ratings_db.get(key)
    if entry:
        ratings_list = entry.get('ratings', [])
        nr_count = sum(1 for r in ratings_list if r.get('score') == 'nr')
        scored_count = sum(1 for r in ratings_list if r.get('score') != 'nr')
        return {
            'avg_score': entry.get('avg_score'),
            'consecutive_nr': entry.get('consecutive_nr', 0),
            'enabled': entry.get('enabled', True),
            'total_ratings': len(ratings_list),
            'nr_count': nr_count,
            'scored_count': scored_count,
            'last_auto': None,
        }
    
    # 2. Fallback: aggregate all entries with the same description (any freq)
    agg_scores = []
    agg_total = 0
    agg_nr_counts = []
    agg_enabled = True
    agg_nr_total = 0
    agg_scored_total = 0
    found_any = False
    for db_key, db_entry in ratings_db.items():
        # db_key format: "freq|description"
        pipe_idx = db_key.find('|')
        if pipe_idx < 0:
            continue
        db_desc = db_key[pipe_idx + 1:]
        if db_desc == desc:
            found_any = True
            ratings_list = db_entry.get('ratings', [])
            agg_total += len(ratings_list)
            agg_nr_total += sum(1 for r in ratings_list if r.get('score') == 'nr')
            agg_scored_total += sum(1 for r in ratings_list if r.get('score') != 'nr')
            agg_nr_counts.append(db_entry.get('consecutive_nr', 0))
            if db_entry.get('avg_score') is not None:
                agg_scores.append(db_entry['avg_score'])
            if not db_entry.get('enabled', True):
                agg_enabled = False
    
    if found_any:
        avg = round(sum(agg_scores) / len(agg_scores), 2) if agg_scores else None
        return {
            'avg_score': avg,
            'consecutive_nr': min(agg_nr_counts) if agg_nr_counts else 0,
            'enabled': agg_enabled,
            'total_ratings': agg_total,
            'nr_count': agg_nr_total,
            'scored_count': agg_scored_total,
            'last_auto': None,
        }
    
    return None

def get_transmission_key(tx):
    """Genera chiave univoca per trasmissione"""
    desc = tx['description'].replace('"', '').replace("'", '')
    return f"{tx['freq']}_{tx['time']}_{desc}"

def load_log():
    """Carica il log delle trasmissioni"""
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_log(log_data):
    """Salva il log delle trasmissioni"""
    with open(LOG_FILE, 'w') as f:
        json.dump(log_data, f, indent=2)

def get_tx_log(tx, log_data):
    """Recupera dati log per una trasmissione"""
    key = get_transmission_key(tx)
    return log_data.get(key, {
        'heard': False,
        'rating': 0,
        'notes': '',
        'last_heard': None,
        'hear_count': 0
    })

_settings_cache = {'data': None, 'mtime': 0}
def _load_settings_cached():
    """Carica settings.json con cache basata su mtime"""
    path = '/var/lib/openwebrx/settings.json'
    try:
        mt = os.path.getmtime(path)
        if _settings_cache['data'] is not None and mt == _settings_cache['mtime']:
            return _settings_cache['data']
        with open(path, 'r') as f:
            data = json.load(f)
        _settings_cache['data'] = data
        _settings_cache['mtime'] = mt
        return data
    except Exception:
        return _settings_cache['data'] or {}

def check_profile_coverage(freq_mhz):
    """Verifica se frequenza è coperta da profilo OpenWebRX esistente"""
    try:
        settings = _load_settings_cached()
        
        freq_hz = float(freq_mhz) * 1e6
        
        # sdrs è un dizionario, non una lista
        sdrs_dict = settings.get('sdrs', {})
        for sdr_id, sdr in sdrs_dict.items():
            profiles = sdr.get('profiles', {})
            for profile_name, profile in profiles.items():
                center = profile.get('center_freq', 0)
                samp_rate = profile.get('samp_rate', 0)
                
                if center and samp_rate:
                    profile_min = center - (samp_rate / 2)
                    profile_max = center + (samp_rate / 2)
                    
                    if profile_min <= freq_hz <= profile_max:
                        # Return full profile ID: "sdr_id|profile_name"
                        return True, f"{sdr_id}|{profile_name}"
        
        return False, None
    except Exception as e:
        print(f"⚠️  Errore verifica profilo per {freq_mhz} MHz: {e}", file=sys.stderr)
        return True, None  # Assume coperto in caso di errore

def create_profile_for_frequency(freq_mhz, description="Auto-generated"):
    """Crea automaticamente profilo OpenWebRX per frequenza non coperta"""
    try:
        freq_hz = float(freq_mhz) * 1e6
        
        # Determina sample rate ottimale
        if freq_hz < 30e6:  # HF
            samp_rate = 2400000
        elif freq_hz < 150e6:  # VHF
            samp_rate = 1600000 if 'LRPT' in description else 1200000
        else:  # UHF
            samp_rate = 3200000
        
        # Nome profilo
        profile_name = f"auto_{int(freq_mhz)}mhz"
        
        # Carica settings
        settings_path = '/var/lib/openwebrx/settings.json'
        with open(settings_path, 'r') as f:
            settings = json.load(f)
        
        # Crea profilo
        new_profile = {
            "name": f"Auto: {freq_mhz} MHz ({description})",
            "center_freq": int(freq_hz),
            "samp_rate": samp_rate,
            "start_freq": int(freq_hz),
            "start_mod": "am",
            "rf_gain": 42,
            "direct_sampling": 0
        }
        
        # Aggiungi decoder LRPT se Meteor-M
        if 'LRPT' in description or 'Meteor' in description:
            new_profile["digital_modes"] = ["lrpt"]
        
        # Aggiungi al primo SDR
        sdr_id = list(settings['sdrs'].keys())[0]
        settings['sdrs'][sdr_id]['profiles'][profile_name] = new_profile
        
        # Backup
        backup_path = f"{settings_path}.bak_auto_{int(datetime.now(timezone.utc).timestamp())}"
        shutil.copy2(settings_path, backup_path)
        
        # Salva
        with open(settings_path, 'w') as f:
            json.dump(settings, f, indent=2)
        
        print(f"✅ Creato profilo '{profile_name}' per {freq_mhz} MHz", file=sys.stderr)
        print(f"   Sample rate: {samp_rate/1e6:.1f} MSPS", file=sys.stderr)
        print(f"   Riavvia OpenWebRX: systemctl restart openwebrx", file=sys.stderr)
        
        return True, f"{sdr_id}|{profile_name}"
        
    except Exception as e:
        print(f"❌ Errore creazione profilo per {freq_mhz} MHz: {e}", file=sys.stderr)
        return False, None

def build_openwebrx_url(freq_mhz, mode, bandwidth_khz=None, decoder=None, tx_type=None):
    """Costruisce URL OpenWebRX con HASH (formato corretto dal codice sorgente)"""
    freq_hz = int(float(freq_mhz) * 1000000)
    mode_lower = mode.lower()
    secondary_mod = None
    
    # TRUCCO FONDAMENTALE per FAX: offset di -1.9 kHz per centrare il tono
    # Le frequenze pubblicate sono quelle della portante, ma in FAX mode serve sintonizzarsi più in basso
    if tx_type == 'WEFAX' and mode.upper() in ['USB', 'FAX']:
        freq_hz = freq_hz - 1900  # Sottrai 1900 Hz (1.9 kHz)
        mode_lower = 'fax'  # Usa FAX come modo primario per attivare il decoder
        secondary_mod = None  # Non serve secondary_mod se usiamo modo primario FAX
    
    # Trova il profilo corretto per la frequenza (usa freq originale per coverage check)
    _, profile_name = check_profile_coverage(float(freq_mhz))
    
    # Formato con PATCH: #freq=XXX,mod=YYY,secondary_mod=ZZZ,profile=WWW,sql=-150
    url = f"{OPENWEBRX_URL}/#freq={freq_hz},mod={mode_lower}"
    if secondary_mod:
        url += f",secondary_mod={secondary_mod}"
    url += f",profile={profile_name},sql=-150"
    
    return url

def get_transmissions():
    """Trasmissioni VERIFICATE e RICEVIBILI dall'Italia (2026)"""
    raw_txs = [
        # METEO FAX (OTTIMO - Mappe meteorologiche Europa/Atlantico)
        {'time': '00:00,06:00,12:00,18:00', 'freq': '4.610', 'type': 'WEFAX', 'days': 'Tutti',
         'description': 'Northwood UK (Mappe Atlantico/Mediterraneo)', 'mode': 'USB', 'bandwidth': '3', 'decoder': 'OpenWebRX+ FAX'},
        {'time': '03:00,09:00,15:00,21:00', 'freq': '7.880', 'type': 'WEFAX', 'days': 'Tutti',
         'description': 'DWD Pinneberg Germany (Mappe Europa)', 'mode': 'USB', 'bandwidth': '3', 'decoder': 'OpenWebRX+ FAX'},
        
        # THE BUZZER - Russia (OTTIMO - continuo, S9)
        {'time': '00:00', 'freq': '4.625', 'type': 'UTILITY', 'days': 'Tutti',
         'description': 'UVB-76 The Buzzer (Russia)', 'mode': 'AM', 'bandwidth': '6', 'decoder': 'Audio'},
        {'time': '12:00', 'freq': '4.625', 'type': 'UTILITY', 'days': 'Tutti',
         'description': 'UVB-76 The Buzzer (Russia)', 'mode': 'AM', 'bandwidth': '6', 'decoder': 'Audio'},
        
        # TIME SIGNALS (solo stazioni ricevibili dall'Italia)
        {'time': '09:00', 'freq': '9.996', 'type': 'TIME', 'days': 'Tutti',
         'description': 'RWM Time Signal (Russia)', 'mode': 'AM', 'bandwidth': '3', 'decoder': 'Audio'},
        
        # VOLMET (Meteo aviazione)
        {'time': '08:00', 'freq': '8.957', 'type': 'VOLMET', 'days': 'Tutti',
         'description': 'Shannon VOLMET (Ireland)', 'mode': 'USB', 'bandwidth': '3', 'decoder': 'Audio'},
        {'time': '20:00', 'freq': '13.264', 'type': 'VOLMET', 'days': 'Tutti',
         'description': 'Shannon VOLMET HF (Ireland)', 'mode': 'USB', 'bandwidth': '3', 'decoder': 'Audio'},
        
        # BROADCAST (verificati attivi)
        {'time': '07:00', 'freq': '7.385', 'type': 'BROADCAST', 'days': 'Tutti',
         'description': 'Radio Romania International [ITA]', 'mode': 'AM', 'bandwidth': '10', 'decoder': 'Audio'},
        {'time': '19:00', 'freq': '9.790', 'type': 'BROADCAST', 'days': 'Tutti',
         'description': 'Radio Romania International', 'mode': 'AM', 'bandwidth': '10', 'decoder': 'Audio'},
        {'time': '08:30', 'freq': '7.250', 'type': 'BROADCAST', 'days': 'Domenica',
         'description': 'Vatican Radio [ITA]', 'mode': 'AM', 'bandwidth': '10', 'decoder': 'Audio'},
        {'time': '17:00', 'freq': '9.660', 'type': 'BROADCAST', 'days': 'Tutti',
         'description': 'Vatican Radio', 'mode': 'AM', 'bandwidth': '10', 'decoder': 'Audio'},
        
        # SATELLITI METEO
        {'time': '09:30', 'freq': '137.620', 'type': 'SATELLITE', 'days': 'Variabile',
         'description': 'NOAA 18 APT', 'mode': 'WFM', 'bandwidth': '40', 'decoder': 'WXtoImg/SatDump'},
        {'time': '11:15', 'freq': '137.912', 'type': 'SATELLITE', 'days': 'Variabile',
         'description': 'NOAA 15 APT', 'mode': 'WFM', 'bandwidth': '40', 'decoder': 'WXtoImg/SatDump'},
        {'time': '14:20', 'freq': '137.100', 'type': 'SATELLITE', 'days': 'Variabile',
         'description': 'NOAA 19 APT', 'mode': 'WFM', 'bandwidth': '40', 'decoder': 'WXtoImg/SatDump'},
        {'time': '18:45', 'freq': '137.900', 'type': 'SATELLITE', 'days': 'Variabile',
         'description': 'Meteor-M N2-3 LRPT', 'mode': 'WFM', 'bandwidth': '120', 'decoder': 'SatDump LRPT'},
        
        # THE PIP (sporadico ma ricevibile)
        {'time': '03:00', 'freq': '3.756', 'type': 'UTILITY', 'days': 'Irregolare',
         'description': 'The Pip (Russia)', 'mode': 'AM', 'bandwidth': '6', 'decoder': 'Audio'},
        {'time': '21:00', 'freq': '4.770', 'type': 'UTILITY', 'days': 'Irregolare',
         'description': 'The Pip (Russia)', 'mode': 'AM', 'bandwidth': '6', 'decoder': 'Audio'},
        
        # NOTA: CHU Canada (7.850/14.670 MHz) e WWV USA (5/10/15 MHz) rimossi:
        #       distanza 7000-8500 km, NON ricevibili da Bolzano.
        
        # BBC WORLD SERVICE su onde corte (ricevibile in Europa)
        {'time': '06:00', 'freq': '6.195', 'type': 'BROADCAST', 'days': 'Tutti',
         'description': 'BBC World Service English 49m', 'mode': 'AM', 'bandwidth': '10', 'decoder': 'Audio'},
        {'time': '18:00', 'freq': '9.410', 'type': 'BROADCAST', 'days': 'Tutti',
         'description': 'BBC World Service English 31m', 'mode': 'AM', 'bandwidth': '10', 'decoder': 'Audio'},
        
        # NOTA: HM01 Cuba (6.855 MHz) rimosso: distanza ~8500 km, NON ricevibile da Bolzano.
        
        # DWD Pinneberg - terza frequenza (13 MHz, propagazione diurna)
        {'time': '06:00,12:00', 'freq': '13.8825', 'type': 'WEFAX', 'days': 'Tutti',
         'description': 'DWD Pinneberg Germany 13 MHz (Mappe)', 'mode': 'USB', 'bandwidth': '3', 'decoder': 'OpenWebRX+ FAX'},
        
        # DWD Pinneberg - frequenza notturna 3.855 MHz
        {'time': '00:00,06:00', 'freq': '3.855', 'type': 'WEFAX', 'days': 'Tutti',
         'description': 'DWD Pinneberg Germany 3 MHz (Mappe)', 'mode': 'USB', 'bandwidth': '3', 'decoder': 'OpenWebRX+ FAX'},
        
        # RAF VOLMET - Meteo aviazione militare UK
        {'time': '05:00,17:00', 'freq': '11.253', 'type': 'VOLMET', 'days': 'Tutti',
         'description': 'RAF VOLMET (UK)', 'mode': 'USB', 'bandwidth': '3', 'decoder': 'Audio'},
        {'time': '09:00,21:00', 'freq': '5.450', 'type': 'VOLMET', 'days': 'Tutti',
         'description': 'RAF VOLMET (UK)', 'mode': 'USB', 'bandwidth': '3', 'decoder': 'Audio'},
        
        # CRI - China Radio International (in italiano!)
        {'time': '18:00', 'freq': '7.265', 'type': 'BROADCAST', 'days': 'Tutti',
         'description': 'CRI China Radio International [ITA]', 'mode': 'AM', 'bandwidth': '10', 'decoder': 'Audio'},
        
        # Radio Kuwait
        {'time': '18:00', 'freq': '11.970', 'type': 'BROADCAST', 'days': 'Tutti',
         'description': 'Radio Kuwait English', 'mode': 'AM', 'bandwidth': '10', 'decoder': 'Audio'},
        
        # Voice of Turkey (TRT)
        {'time': '17:30', 'freq': '9.830', 'type': 'BROADCAST', 'days': 'Tutti',
         'description': 'Voice of Turkey (TRT) English', 'mode': 'AM', 'bandwidth': '10', 'decoder': 'Audio'},
        
        # All India Radio (AIR)
        {'time': '20:00', 'freq': '9.445', 'type': 'BROADCAST', 'days': 'Tutti',
         'description': 'All India Radio English', 'mode': 'AM', 'bandwidth': '10', 'decoder': 'Audio'},
        
        # STANAG 4285 - NATO Naval Broadcasts (Sempre attivo, modem digitale)
        {'time': '10:00,16:00', 'freq': '8.495', 'type': 'UTILITY', 'days': 'Tutti',
         'description': 'STANAG 4285 NATO Naval', 'mode': 'USB', 'bandwidth': '3', 'decoder': 'Audio/Digital'},
        
        # ISS APRS Packet
        {'time': '12:00,20:00', 'freq': '145.825', 'type': 'SATELLITE', 'days': 'Variabile',
         'description': 'ISS APRS Packet Radio', 'mode': 'NFM', 'bandwidth': '12.5', 'decoder': 'Packet/APRS'},
        
        # Radio Exterior de Espana
        {'time': '00:00', 'freq': '9.690', 'type': 'BROADCAST', 'days': 'Tutti',
         'description': 'Radio Exterior de España', 'mode': 'AM', 'bandwidth': '10', 'decoder': 'Audio'},
        
        # NOTA: BPM Cina (5.154 MHz) rimosso: distanza ~9000 km, NON ricevibile da Bolzano.
    ]
    
    # Espandi trasmissioni con orari multipli (es. "00:00,06:00,12:00")
    expanded = []
    for tx in raw_txs:
        if ',' in tx['time']:
            # Crea una voce separata per ogni orario
            for time_slot in tx['time'].split(','):
                tx_copy = tx.copy()
                tx_copy['time'] = time_slot.strip()
                expanded.append(tx_copy)
        else:
            expanded.append(tx)
    
    return expanded

# EIBI target regions receivable from Bolzano, Italy (46.5°N 11.3°E)
# with a Discone antenna + LNA on HF (3-30 MHz, RTL-SDR direct sampling)
# Includes all Europe, Near East, North Africa, CIS, Global
BOLZANO_RECEIVABLE_TARGETS = {
    'Eu',    # Europe (generic)
    'CEu',   # Central Europe
    'WEu',   # Western Europe
    'SEu',   # Southern Europe
    'EEu',   # Eastern Europe
    'NEu',   # Northern Europe
    'SEE',   # South-Eastern Europe
    'ME',    # Middle East
    'NAf',   # North Africa
    'CIS',   # CIS / ex-USSR
    'Cau',   # Caucasus
    'UKR',   # Ukraine
    'NAO',   # North Atlantic
    'Global',# Global broadcasts
    'ITN',   # Italy (specific)
    'RUS',   # Russia (European part, sometimes receivable)
}

def get_external_transmissions(now, max_hours=24):
    """Legge eventi da sdr-eibi-events.json (EIBI + priyom.org calendar).
    Filtra solo le trasmissioni ricevibili da Bolzano.
    Deduplicato per (freq, descrizione): una sola entry per stazione.
    On-air preferita (fine più imminente prima), poi prossima occorrenza."""
    if not os.path.exists(EIBI_EVENTS_JSON):
        return []
    try:
        with open(EIBI_EVENTS_JSON) as f:
            data = json.load(f)
    except Exception:
        return []

    now_frac = now.hour * 60 + now.minute + now.second / 60.0  # sub-minute precision
    now_min = int(now_frac)  # integer version for on-air check
    best = {}  # (freq, desc) → best event dict

    for ev in data.get("events", []):
        target = ev.get("target", "")
        # Handle compound targets like "Eu,NAf" or "CEu/SEu"
        if target:
            target_parts = [t.strip() for t in target.replace('/', ',').split(',')]
            if not any(tp in BOLZANO_RECEIVABLE_TARGETS for tp in target_parts):
                continue
        t_str = ev.get("time_utc", "")
        try:
            h, m = map(int, t_str.split(':'))
        except ValueError:
            continue

        start_min = h * 60 + m
        end_str = ev.get("end_utc", "")
        dur_min = ev.get("duration_min")
        if end_str and ":" in end_str:
            try:
                eh, em = map(int, end_str.split(":"))
                end_min = eh * 60 + em
                # Normalize midnight-crossing: if end < start, it wraps past 00:00
                if end_min < start_min:
                    end_min += 1440
            except ValueError:
                end_min = start_min + (dur_min if dur_min else 30)
        elif dur_min:
            end_min = start_min + dur_min
        else:
            end_min = start_min + 30

        # On-air check with midnight-crossing support
        if end_min > 1440:
            on_air = now_min >= start_min or now_min < end_min % 1440
        else:
            on_air = start_min <= now_min < end_min

        end_abs = end_min % 1440
        end_disp = f"{end_abs // 60:02d}:{end_abs % 60:02d}"

        if on_air:
            # minutes_to_end with sub-minute precision
            if end_min > 1440 and now_frac >= start_min:
                minutes_to_end = (1440 - now_frac) + end_abs
            else:
                minutes_to_end = end_abs - now_frac
            sort_key = (0, minutes_to_end)  # on-air: soonest-ending first (most urgent)
            tx_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
            delta_minutes = (tx_time - now).total_seconds() / 60
        else:
            tx_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if tx_time <= now:
                tx_time += timedelta(days=1)
            delta_minutes = (tx_time - now).total_seconds() / 60
            if delta_minutes > max_hours * 60:
                continue
            minutes_to_end = None
            sort_key = (1, delta_minutes)  # upcoming: soonest start first

        freq = ev.get("frequency_mhz", "")
        desc = ev.get("description", "")
        station_key = (freq, desc)

        existing = best.get(station_key)
        if existing is None or sort_key < existing['_sort_key']:
            best[station_key] = {
                'time': t_str,
                'freq': freq,
                'description': desc,
                'mode': ev.get("mode", "USB"),
                'bandwidth': ev.get("bandwidth", "3"),
                'decoder': ev.get("decoder", "Audio"),
                'type': 'EXTERNAL',
                'days': 'Tutti',
                'source': ev.get("source", "EIBI"),
                'target': target,
                'delta_minutes': delta_minutes,
                'datetime': tx_time,
                'target_time': tx_time,
                'on_air': on_air,
                'end_utc': end_disp,
                'minutes_to_end': minutes_to_end,
                '_sort_key': sort_key,
            }

    return list(best.values())


def get_next_transmissions(count=15):
    now = datetime.now(timezone.utc)
    with_time = []
    for tx in get_transmissions():
        h, m = map(int, tx['time'].split(':'))
        tx_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
        delta_minutes = (tx_time - now).total_seconds() / 60

        # Includi eventi iniziati fino a 60 minuti fa (mostra "in corso")
        if -60 <= delta_minutes < 0:
            # On-air: recently started local event
            sk = (0, 60 + delta_minutes)  # urgency: closer to end = lower
            with_time.append({**tx, 'datetime': tx_time, 'target_time': tx_time,
                              'delta_minutes': delta_minutes, 'on_air': True,
                              'end_utc': None, 'minutes_to_end': None,
                              '_sort_key': sk})
        else:
            # Upcoming: push to tomorrow if in the past
            if tx_time < now:
                tx_time += timedelta(days=1)
                delta_minutes = (tx_time - now).total_seconds() / 60
            sk = (1, delta_minutes)
            with_time.append({**tx, 'datetime': tx_time, 'target_time': tx_time,
                              'delta_minutes': delta_minutes, 'on_air': False,
                              'end_utc': None, 'minutes_to_end': None,
                              '_sort_key': sk})

    # Fondi con eventi EIBI/priyom, deduplicando per (freq, descrizione)
    existing_station_keys = {(t['freq'], t['description']) for t in with_time}
    for ev in get_external_transmissions(now):
        sk = (ev['freq'], ev['description'])
        if sk not in existing_station_keys:
            with_time.append(ev)
            existing_station_keys.add(sk)

    # Ordina: on-air per urgenza (fine più imminente), poi upcoming per inizio imminente
    with_time.sort(key=lambda t: t.get('_sort_key', (1, t['delta_minutes'])))
    return with_time[:count]

def generate_html():
    txs = get_next_transmissions(25)
    now = datetime.now(timezone.utc)
    log_data = load_log()
    ratings_db = load_signal_ratings()
    # Aggiungi timestamp per forzare reload del browser
    version = int(now.timestamp())
    update_time = now.strftime('%H:%M:%S UTC')
    html = f'''<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="version" content="v{version}">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate, max-age=0">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>SDR Schedule v{version}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:Arial,sans-serif;background:linear-gradient(135deg,#1e3c72,#2a5298);color:#fff;padding:20px;min-height:100vh}}
.container{{max-width:1000px;margin:0 auto}}
.header{{text-align:center;margin-bottom:30px;padding:25px 20px;background:rgba(255,255,255,0.1);border-radius:10px}}
.header h1{{font-size:1.6em;margin-bottom:15px;opacity:0.9}}
.utc-clock{{font-size:4em;font-weight:bold;font-family:'Courier New',monospace;letter-spacing:3px;text-shadow:0 0 20px rgba(100,181,246,0.5)}}
.header-date{{font-size:1em;opacity:0.7;margin-top:8px}}
.footer{{text-align:center;margin-top:30px;padding:15px;background:rgba(0,0,0,0.3);border-radius:10px;font-size:0.8em;opacity:0.6}}
.nav-bar{{display:flex;justify-content:center;gap:12px;margin-bottom:20px;flex-wrap:wrap}}
.nav-bar a{{display:inline-flex;align-items:center;gap:6px;padding:8px 18px;background:rgba(255,255,255,0.15);color:#fff;text-decoration:none;border-radius:8px;font-size:0.95em;font-weight:bold;transition:all 0.3s;border:1px solid rgba(255,255,255,0.1)}}
.nav-bar a:hover{{background:rgba(255,255,255,0.25);transform:translateY(-2px);box-shadow:0 4px 12px rgba(0,0,0,0.3)}}
.nav-bar a.rec-link{{border-color:#FF5722;background:rgba(255,87,34,0.25)}}
.nav-bar a.rec-link:hover{{background:rgba(255,87,34,0.4);box-shadow:0 4px 12px rgba(255,87,34,0.4)}}
.sort-bar{{display:flex;justify-content:center;gap:8px;margin-bottom:18px;flex-wrap:wrap}}
.sort-btn{{display:inline-flex;align-items:center;gap:4px;padding:6px 14px;background:rgba(255,255,255,0.1);color:rgba(255,255,255,0.7);border:1px solid rgba(255,255,255,0.15);border-radius:20px;font-size:0.82em;font-weight:600;cursor:pointer;transition:all 0.25s;user-select:none}}
.sort-btn:hover{{background:rgba(255,255,255,0.2);color:#fff}}
.sort-btn.active{{background:rgba(100,181,246,0.3);color:#90CAF9;border-color:rgba(100,181,246,0.5)}}
.transmission{{background:rgba(255,255,255,0.15);border-radius:10px;padding:20px;margin-bottom:15px;border-left:4px solid #4CAF50;cursor:pointer;transition:all 0.3s;position:relative}}
.transmission:hover{{transform:translateX(5px);background:rgba(255,255,255,0.2)}}
.transmission.next{{border-left-color:#FF5722;background:rgba(255,87,34,0.2)}}
.transmission.heard{{border-left-color:#FFD700;opacity:0.85}}
.tx-header{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:15px}}
.tx-time{{font-size:1.4em;font-weight:bold;font-family:monospace}}
.tx-countdown{{background:rgba(255,255,255,0.3);padding:5px 15px;border-radius:20px;font-size:0.9em;font-weight:bold;align-self:flex-start}}
.next .tx-countdown{{background:#FF5722;animation:pulse 2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.7}}}}
.tx-freq{{font-size:1.3em;color:#FFC107;font-weight:bold;margin-bottom:10px;cursor:pointer;transition:all 0.2s;padding:5px 10px;border-radius:5px;display:inline-block}}
.tx-freq:hover{{background:rgba(255,193,7,0.3);transform:scale(1.05);box-shadow:0 0 10px rgba(255,193,7,0.5)}}
.tx-freq:active{{transform:scale(0.98);background:rgba(255,193,7,0.5)}}
.tx-freq::after{{content:'';font-size:0.6em;color:#64B5F6;margin-left:10px;opacity:0.8}}
.tx-desc{{opacity:0.9;margin-bottom:10px}}
.tx-stats{{display:flex;gap:15px;margin-bottom:10px;font-size:0.9em;align-items:center;flex-wrap:wrap}}
.auto-quality-container{{display:flex;flex-direction:column;gap:4px;flex:1 1 100%}}
.aq-row{{display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap}}
.auto-quality-stars{{display:inline-flex;align-items:center;color:#FFD700;cursor:pointer;user-select:none;font-size:1.8em}}
.auto-quality-stars .star{{cursor:pointer;transition:all 0.2s;font-size:0.72em}}
.auto-quality-stars .star:hover{{transform:scale(1.4);filter:drop-shadow(0 0 5px #FFD700)}}
.aq-info{{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:10px;font-size:0.8em}}
.aq-info.aq-nr{{background:rgba(255,82,82,0.3);color:#FF8A80}}
.aq-info.aq-low{{background:rgba(255,152,0,0.3);color:#FFB74D}}
.aq-info.aq-mid{{background:rgba(255,193,7,0.3);color:#FFF176}}
.aq-info.aq-high{{background:rgba(76,175,80,0.35);color:#A5D6A7}}
.rec-cards{{margin-top:4px;width:100%}}
.rec-card{{background:#0d1117;border-radius:10px;margin-bottom:8px;overflow:hidden;border:1px solid rgba(255,255,255,0.08);transition:all 0.2s}}
.rec-card:hover{{border-color:rgba(255,87,34,0.4);box-shadow:0 0 12px rgba(255,87,34,0.15)}}
.rec-card-header{{display:flex;align-items:center;gap:6px;padding:7px 12px;background:rgba(255,255,255,0.04);flex-wrap:wrap;font-size:0.8em;border-bottom:1px solid rgba(255,255,255,0.06)}}
.rec-card-header .rc-icon{{font-size:1em}}
.rec-card-header .rc-date{{font-family:monospace;color:#E0E0E0;font-weight:600}}
.rec-card-header .rc-dur{{color:#81C784;font-weight:bold;background:rgba(76,175,80,0.15);padding:1px 7px;border-radius:8px}}
.rec-card-header .rc-size{{color:#90CAF9;opacity:0.65;font-size:0.9em}}
.rec-card-header .rc-score{{color:#FFD700;letter-spacing:1px}}
.rec-card-header .rc-dl{{color:#64B5F6;text-decoration:none;opacity:0.5;transition:opacity 0.2s;margin-left:auto}}
.rec-card-header .rc-dl:hover{{opacity:1}}
.rec-card-header .rc-gemini{{color:#a78bfa;border:1px solid #7c3aed;background:none;font-size:0.85em;padding:1px 6px;border-radius:4px;cursor:pointer;opacity:0.7;transition:all 0.2s}}
.rec-card-header .rc-gemini:hover{{opacity:1;background:#7c3aed;color:#fff}}
.rec-viz{{position:relative;cursor:pointer;background:#060a14;overflow:hidden;border-radius:4px;margin:4px 8px}}
.rec-viz canvas.rv-spec{{display:block;width:100%;height:36px}}
.rec-viz canvas.rv-wave{{display:block;width:100%;height:28px}}
.rec-viz .rv-overlay{{position:absolute;top:0;left:0;width:0%;height:100%;background:rgba(0,212,255,0.12);pointer-events:none;transition:width 0.06s linear}}
.rec-viz .rv-loading{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:0.7em;color:#556;font-family:monospace}}
.rec-card-bottom{{display:flex;align-items:center;gap:6px;padding:4px 10px 6px}}
.rec-card-bottom audio{{flex:1;height:28px;border-radius:4px;min-width:0}}
.rec-no-audio{{font-size:0.78em;color:#999;padding:4px 0;font-style:italic}}
.rc-aq{{font-size:0.75em;padding:1px 6px;border-radius:8px;font-weight:600;white-space:nowrap}}
.rc-aq small{{font-weight:400;opacity:0.7}}
.rc-aq-good{{background:rgba(76,175,80,0.25);color:#A5D6A7}}
.rc-aq-fair{{background:rgba(255,193,7,0.25);color:#FFF176}}
.rc-aq-poor{{background:rgba(255,82,82,0.25);color:#FF8A80}}
.tx-heard{{color:#81C784;cursor:pointer;padding:3px 10px;border-radius:15px;background:rgba(255,255,255,0.1);transition:all 0.2s;user-select:none}}
.tx-heard:hover{{background:rgba(255,255,255,0.2);transform:scale(1.05)}}
.tx-heard.not-heard{{color:#BDBDBD;opacity:0.8}}
.tx-count{{color:#90CAF9;font-size:0.85em}}
.tx-technical{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.2)}}
.tech-item{{background:rgba(0,0,0,0.3);padding:8px;border-radius:5px;text-align:center}}
.tech-label{{font-size:0.75em;opacity:0.7;margin-bottom:3px}}
.tech-value{{font-size:0.95em;font-weight:bold;color:#81C784}}
.auto-quality.aq-disabled{{background:rgba(255,82,82,0.4);color:#FF1744;text-decoration:line-through}}
.aq-info.aq-disabled{{background:rgba(255,82,82,0.3);color:#FF8A80;text-decoration:line-through}}
.transmission.disabled{{opacity:0.45;filter:saturate(0.3);border-left-color:#666!important}}
.transmission.disabled:hover{{opacity:0.65;filter:saturate(0.5)}}
.auto-quality .aq-label{{font-size:0.7em;opacity:0.7}}
.version-banner{{background:rgba(0,0,0,0.5);padding:5px 10px;border-radius:5px;font-size:0.8em;display:inline-block;margin-left:10px;cursor:pointer}}
.version-banner:hover{{background:rgba(0,0,0,0.7)}}
.auto-mode-panel{{background:rgba(0,0,0,0.35);border-radius:12px;padding:14px 20px 12px;margin-bottom:20px;text-align:center}}
.auto-panel-top{{display:flex;align-items:center;justify-content:center;gap:10px;margin-bottom:6px;flex-wrap:wrap}}
.auto-station-name{{font-size:1.15em;font-weight:bold;color:#FFD54F;letter-spacing:0.5px;text-shadow:0 2px 8px rgba(0,0,0,0.7)}}
.auto-station-freq{{font-size:0.95em;color:#E3F2FD;font-weight:600}}
.auto-panel-bottom{{display:flex;align-items:center;justify-content:center;gap:12px;flex-wrap:wrap;font-size:0.82em;opacity:0.9}}
.auto-status-badge{{padding:4px 14px;border-radius:16px;font-weight:bold;font-size:0.85em;letter-spacing:0.5px;white-space:nowrap}}
.auto-status-badge.auto{{background:#4CAF50;color:#fff;box-shadow:0 0 10px rgba(76,175,80,0.4)}}
.auto-status-badge.manual{{background:#FF9800;color:#fff}}
.auto-status-badge.off{{background:#666;color:#ccc}}
.auto-status-badge.idle{{background:#2196F3;color:#fff;box-shadow:0 0 10px rgba(33,150,243,0.3)}}
.auto-status-badge.scanning{{background:#9C27B0;color:#fff;box-shadow:0 0 10px rgba(156,39,176,0.4);animation:pulse 1.5s infinite}}
.auto-panel-bottom span{{white-space:nowrap}}
.auto-panel-bottom .sep{{opacity:0.3;font-size:0.8em}}
.auto-quality-live{{display:none}}
.scan-btn{{background:#9C27B0;color:#fff;padding:5px 14px;border:none;border-radius:16px;cursor:pointer;font-weight:bold;transition:all 0.3s;font-size:0.85em}}
.scan-btn:hover{{background:#7B1FA2;box-shadow:0 0 12px rgba(156,39,176,0.5)}}
.scan-modal-overlay{{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.8);z-index:9999;justify-content:center;align-items:center}}
.scan-modal-overlay.active{{display:flex}}
.scan-modal{{background:#1a1a1a;color:#fff;padding:25px;border-radius:15px;width:90%;max-width:600px;max-height:80vh;overflow-y:auto;box-shadow:0 0 30px rgba(156,39,176,0.5)}}
.scan-modal h2{{margin-top:0;color:#9C27B0}}
.close-btn{{position:absolute;top:10px;right:15px;background:none;border:none;color:#fff;font-size:2em;cursor:pointer;line-height:1}}
.close-btn:hover{{color:#9C27B0}}
.scan-toggle{{margin:15px 0;display:flex;align-items:center;gap:10px}}
.scan-toggle input[type="checkbox"]{{width:20px;height:20px;cursor:pointer}}
.scan-silence-row{{display:flex;align-items:center;gap:10px;margin:10px 0}}
.scan-silence-row input{{width:80px;padding:5px;background:#333;border:1px solid #666;color:#fff;border-radius:5px}}
.scan-freq-list{{list-style:none;padding:0;margin:15px 0}}
.scan-freq-item{{background:#2a2a2a;padding:12px;margin-bottom:10px;border-radius:8px;display:flex;justify-content:space-between;align-items:center}}
.freq-info{{flex:1}}
.freq-label{{font-weight:bold;color:#4CAF50;margin-bottom:5px}}
.freq-detail{{font-size:0.85em;opacity:0.7}}
.del-btn{{background:#f44336;color:#fff;border:none;padding:5px 12px;border-radius:5px;cursor:pointer;transition:background 0.2s}}
.del-btn:hover{{background:#d32f2f}}
.scan-add-form{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}}
.scan-add-form input,.scan-add-form select{{background:#333;border:1px solid #666;color:#fff;padding:8px;border-radius:5px}}
.scan-add-form button{{grid-column:1/-1;background:#4CAF50;color:#fff;border:none;padding:10px;border-radius:5px;cursor:pointer;font-weight:bold;transition:background 0.3s}}
.scan-add-form button:hover{{background:#45a049}}
.scan-save-btn{{background:#4CAF50;color:#fff;border:none;padding:10px 20px;border-radius:5px;cursor:pointer;font-weight:bold;margin-top:10px}}
.scan-save-btn:hover{{background:#45a049}}
</style>
<script>
// SOLUZIONE: RigCtl Bridge - invia comandi via nc a rigctld locale
async function openFrequency(event, freq, mode, name, profile) {{
    if (event && event.target.closest('.auto-quality-container, .tx-heard')) {{
        return;
    }}
    
    if (event) {{
        event.stopPropagation();
    }}
    
    console.log('📡 Apertura:', name, freq, 'MHz', mode, '| Profilo:', profile);
    
    const freqHz = Math.round(parseFloat(freq) * 1000000);
    
    // Formato con PATCH profile support
    const url = `{OPENWEBRX_URL}/#freq=${{freqHz}},mod=${{mode.toLowerCase()}},profile=${{profile}},sql=-150`;
    window.open(url, '_blank');
    console.log('🔗', url);
}}
</script>
</head><body><div class="container"><div class="header"><h1>📡 Prossime Trasmissioni</h1>
<div class="utc-clock" id="time">{now.strftime('%H:%M:%S')}</div>
<div id="localtime" style="font-size:0.65em;opacity:0.55;margin-top:2px;font-family:'Courier New',monospace;letter-spacing:1px;">locale --:--</div>
<div class="header-date">📅 <span id="date">{['Lunedì','Martedì','Mercoledì','Giovedì','Venerdì','Sabato','Domenica'][now.weekday()]} {now.strftime('%d')} {['Gennaio','Febbraio','Marzo','Aprile','Maggio','Giugno','Luglio','Agosto','Settembre','Ottobre','Novembre','Dicembre'][now.month-1]} {now.strftime('%Y')}</span> &nbsp;|&nbsp; 🔄 Ultimo aggiornamento: {update_time}</div></div>
<div class="nav-bar">
  <a href="{OPENWEBRX_URL}/recordings" class="rec-link" target="_blank">🔴 Registrazioni</a>
  <a href="{OPENWEBRX_URL}/files" target="_blank">📁 Files Audio</a>
  <a href="{OPENWEBRX_URL}" target="_blank">📻 Radio</a>
  <a href="{OPENWEBRX_URL}/map" target="_blank">🗺️ Mappa</a>
</div>
<div class="sort-bar" id="sort-bar">
  <span style="opacity:0.5;font-size:0.82em;align-self:center">Ordina:</span>
  <button class="sort-btn active" data-sort="time-asc" onclick="sortCards(this)">⏰ Tempo ↑</button>
  <button class="sort-btn" data-sort="time-desc" onclick="sortCards(this)">⏰ Tempo ↓</button>
  <button class="sort-btn" data-sort="stars-desc" onclick="sortCards(this)">⭐ Stelle ↓</button>
  <button class="sort-btn" data-sort="stars-asc" onclick="sortCards(this)">⭐ Stelle ↑</button>
</div>
<div class="auto-mode-panel" id="auto-mode-panel">
  <div class="auto-panel-top">
    <span class="auto-station-name" id="auto-station">⏳ Caricamento...</span>
    <span class="auto-station-freq" id="auto-freq"></span>
  </div>
  <div class="auto-panel-bottom">
    <span class="auto-status-badge off" id="auto-badge"></span>
    <span class="sep">|</span>
    <span id="auto-progress"></span>
    <span class="sep">|</span>
    <span id="auto-clients"></span>
    <span class="sep">|</span>
    <span id="auto-rec"></span>
    <button class="scan-btn" onclick="openScanModal()">📻 Scansione</button>
  </div>
</div>
<div class="scan-modal-overlay" id="scan-modal-overlay">
  <div class="scan-modal">
    <button class="close-btn" onclick="closeScanModal()">&times;</button>
    <h2>📻 Frequenze Scansione</h2>
    <p style="font-size:0.85em;opacity:0.7;margin-bottom:10px">Frequenze da scansionare in attesa di eventi programmati. Lo scanner resta sulla frequenza finché c'è segnale, poi passa alla successiva dopo il timeout di silenzio.</p>
    <div class="scan-toggle">
      <input type="checkbox" id="scan-enabled" checked>
      <label for="scan-enabled">Scansione attiva</label>
    </div>
    <div class="scan-silence-row">
      <span>Timeout silenzio:</span>
      <input type="number" id="scan-silence" value="15" min="5" max="120">
      <span>secondi</span>
    </div>
    <ul class="scan-freq-list" id="scan-freq-list"></ul>
    <h3 style="margin-top:15px;font-size:1em">➕ Aggiungi Frequenza</h3>
    <div class="scan-add-form">
      <input type="text" id="scan-new-freq" placeholder="Freq MHz (es. 145.500)">
      <select id="scan-new-mode">
        <option value="NFM">NFM</option>
        <option value="FM">FM</option>
        <option value="WFM">WFM</option>
        <option value="AM">AM</option>
        <option value="USB">USB</option>
        <option value="LSB">LSB</option>
      </select>      <select id="scan-new-decoder">
        <option value="">Nessun decoder</option>
        <option value="packet">APRS/Packet</option>
        <option value="fax">Fax</option>
        <option value="ft8">FT8</option>
        <option value="wspr">WSPR</option>
        <option value="pocsag">POCSAG</option>
        <option value="ism">📡 ISM</option>
      </select>      <input type="text" id="scan-new-label" placeholder="Etichetta (es. Chiamata 2m)">
      <input type="text" id="scan-new-bw" placeholder="BW kHz (es. 12.5)">
      <input type="text" id="scan-new-squelch" placeholder="Squelch RMS (es. 0.15)" value="0.15">
      <button class="scan-save-btn" onclick="addScanFreq()">➕ Aggiungi</button>
    </div>
  </div>
</div>
<script>
(function(){{
  const host = window.location.hostname;
  const badge = document.getElementById('auto-badge');
  const stationEl = document.getElementById('auto-station');
  const freqEl = document.getElementById('auto-freq');
  const progressEl = document.getElementById('auto-progress');
  const clientsEl = document.getElementById('auto-clients');
  const recEl = document.getElementById('auto-rec');
  let recDuration = 0;
  let recActive = false;
  let recDisabled = false;
  let recSynced = false;
  let recTimer = null;
  let recLastSync = 0;
  function fmtDur(s){{
    s = Math.max(0, Math.floor(s));
    const h = Math.floor(s/3600);
    const m = Math.floor((s%3600)/60);
    const sec = s%60;
    if(h>0) return h+'h '+String(m).padStart(2,'0')+'m '+String(sec).padStart(2,'0')+'s';
    if(m>0) return m+'m '+String(sec).padStart(2,'0')+'s';
    return sec+'s';
  }}
  function updateRecDisplay(){{
    if(recDisabled){{
      recEl.textContent = '🔇 REC off (digitale)';
    }}else if(!recSynced){{
      recEl.textContent = '⏳ REC...';
    }}else if(recActive){{
      recEl.textContent = '🔴 REC '+fmtDur(recDuration);
    }}else{{
      recEl.textContent = '⚪ REC standby';
    }}
  }}
  function startRecTimer(){{
    if(recTimer) return;
    recTimer = setInterval(()=>{{
      if(recActive && recSynced){{
        recDuration++;
        updateRecDisplay();
      }}
    }}, 1000);
  }}
  async function fetchStatus(){{
    let data = null;
    let fetchError = false;
    try{{
      const ctrl = new AbortController();
      setTimeout(()=>ctrl.abort(), 3000);
      const r = await fetch('http://'+host+':8073/api/auto-mode/status', {{signal:ctrl.signal}});
      if(r.ok) data = await r.json();
      else fetchError = true;
    }}catch(e){{fetchError = true;}}
    if(!data){{
      try{{
        const r2 = await fetch('http://'+host+'/auto-mode-status.json?t='+Date.now());
        if(r2.ok) data = await r2.json();
        else fetchError = true;
      }}catch(e2){{fetchError = true;}}
    }}
    // Show offline indicator if both API calls failed
    if(fetchError && !data){{
      badge.className = 'auto-status-badge off';
      badge.textContent = '❌ OPENWEBRX OFFLINE';
      freqEl.innerHTML = '<span style="color:#f44336">Servizio non disponibile</span>';
      // Stop recording timer on offline
      recActive = false;
      recDuration = 0;
      if(recTimer){{
        clearInterval(recTimer);
        recTimer = null;
      }}
      updateRecDisplay();
      return;
    }}
    if(data && data.orchestrator){{
      const orch = data.orchestrator;
      const cm = data.client_monitor;
      const dm = data.decoder_manager;
      const sr = data.squelch_recorder;
      const st = (orch.state||'').toUpperCase();
      if(st==='AUTO'){{
        badge.className = 'auto-status-badge auto';
        badge.textContent = '🤖 SCANSIONE AUTO';
      }}else if(st==='SCANNING'){{
        badge.className = 'auto-status-badge scanning';
        badge.textContent = '🔍 SCANSIONE';
      }}else if(st==='IDLE'){{
        badge.className = 'auto-status-badge idle';
        badge.textContent = '💤 IN ATTESA';
      }}else if(st==='MANUAL'){{
        badge.className = 'auto-status-badge manual';
        badge.textContent = '🎛️ MANUALE';
      }}else{{
        badge.className = 'auto-status-badge off';
        badge.textContent = '⏸️ NON ATTIVO';
      }}
      // --- Populate panel ---
      if(orch.current_frequency){{
        const cf = orch.current_frequency;
        const freqMHz = (cf.frequency/1e6).toFixed(3);
        const label = cf.label || (freqMHz+' MHz');
        // Top row: station name + frequency
        stationEl.textContent = label;
        if(orch.scanning_events && cf.event_index !== undefined){{
          const src = cf.source ? ' ['+cf.source+']' : '';
          freqEl.textContent = '📡 '+freqMHz+' MHz'+src;
          progressEl.textContent = '📻 '+(cf.event_index+1)+'/'+cf.event_total;
        }}else if(cf.scan_index !== undefined){{
          freqEl.textContent = '🔍 '+freqMHz+' MHz';
          progressEl.textContent = '📻 '+(cf.scan_index+1)+'/'+cf.scan_total;
        }}else{{
          freqEl.textContent = '📡 '+freqMHz+' MHz';
          progressEl.textContent = '';
        }}
      }}else if(st==='IDLE'){{
        stationEl.textContent = '💤 In attesa...';
        freqEl.textContent = '';
        progressEl.textContent = '';
      }}else{{
        stationEl.textContent = '';
        freqEl.textContent = '';
        progressEl.textContent = '';
      }}
      if(cm && cm.clients){{
        const c = cm.clients;
        clientsEl.textContent = '👥 '+c.total+' client'+(c.total!==1?'i':'');
      }}
      recDisabled = !!(sr && sr.recording_disabled);
      if(sr && sr.recording){{
        recActive = true;
        recDuration = Math.floor(sr.duration||0);
        recLastSync = Date.now();
        recSynced = true;
        startRecTimer();
      }}else if(dm && dm.is_recording){{
        recActive = true;
        recSynced = true;
        startRecTimer();
      }}else{{
        // No recording active - stop timer
        recActive = false;
        recDuration = 0;
        recSynced = true;
        if(recTimer){{
          clearInterval(recTimer);
          recTimer = null;
        }}
      }}
      updateRecDisplay();
    }}else if(data && data.initialized===false){{
      badge.className='auto-status-badge off';
      badge.textContent='⏸️ NON ATTIVO';
    }}else{{
      badge.className='auto-status-badge off';
      badge.textContent='⚠️ Stato non disponibile';
    }}
  }}
  fetchStatus();
  setInterval(fetchStatus, 10000);
}})();

// ═══ Scan Modal Functions ═══
const scanApiUrl = 'http://'+window.location.hostname+':8073/api/scan/frequencies';

function openScanModal(){{
  document.getElementById('scan-modal-overlay').classList.add('active');
  loadScanFreqs();
}}
function closeScanModal(){{
  document.getElementById('scan-modal-overlay').classList.remove('active');
}}
document.getElementById('scan-modal-overlay').addEventListener('click', function(e){{
  if(e.target === this) closeScanModal();
}});

async function loadScanFreqs(){{
  try{{
    const r = await fetch(scanApiUrl);
    if(!r.ok) return;
    const data = await r.json();
    document.getElementById('scan-enabled').checked = data.scan_enabled !== false;
    document.getElementById('scan-silence').value = data.silence_timeout_seconds || 15;
    const list = document.getElementById('scan-freq-list');
    list.innerHTML = '';
    (data.frequencies||[]).forEach((f,i)=>{{
      const li = document.createElement('li');
      li.className = 'scan-freq-item';
      const decoderInfo = f.decoder ? ' | Decoder: '+f.decoder : '';
      li.innerHTML = '<div class="freq-info"><div class="freq-label">'+f.frequency_mhz+' MHz — '+
        (f.label||'')+'</div><div class="freq-detail">'+f.mode+' | BW: '+(f.bandwidth||'auto')+
        ' kHz | Squelch: '+(f.squelch||0.10)+decoderInfo+'</div></div>'+
        '<button class="del-btn" onclick="deleteScanFreq('+i+')">🗑️</button>';
      list.appendChild(li);
    }});
    if(!data.frequencies || data.frequencies.length===0){{
      list.innerHTML = '<li style="text-align:center;opacity:0.5;padding:15px">Nessuna frequenza configurata</li>';
    }}
  }}catch(e){{console.error('Errore caricamento scan:',e);}}
}}

async function addScanFreq(){{
  const freq = document.getElementById('scan-new-freq').value.trim();
  const mode = document.getElementById('scan-new-mode').value;
  const decoder = document.getElementById('scan-new-decoder').value;
  const label = document.getElementById('scan-new-label').value.trim();
  const bw = document.getElementById('scan-new-bw').value.trim();
  const sq = document.getElementById('scan-new-squelch').value.trim();
  if(!freq){{alert('Inserisci la frequenza in MHz');return;}}
  if(isNaN(parseFloat(freq))){{alert('Frequenza non valida');return;}}
  try{{
    const r = await fetch(scanApiUrl);
    const data = await r.json();
    const freqs = data.frequencies || [];
    freqs.push({{
      frequency_mhz: freq,
      mode: mode,
      decoder: decoder || null,
      label: label || (freq+' MHz'),
      bandwidth: bw || '12.5',
      squelch: parseFloat(sq) || 0.10
    }});
    const payload = {{
      scan_enabled: document.getElementById('scan-enabled').checked,
      silence_timeout_seconds: parseInt(document.getElementById('scan-silence').value) || 15,
      frequencies: freqs
    }};
    const r2 = await fetch(scanApiUrl, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(payload)}});
    if(r2.ok){{
      document.getElementById('scan-new-freq').value='';
      document.getElementById('scan-new-label').value='';
      document.getElementById('scan-new-bw').value='';
      loadScanFreqs();
    }}else{{
      const err = await r2.json();
      alert('Errore: '+(err.error||'sconosciuto'));
    }}
  }}catch(e){{alert('Errore di rete: '+e);}}
}}

async function deleteScanFreq(index){{
  if(!confirm('Rimuovere questa frequenza dalla scansione?')) return;
  try{{
    const r = await fetch(scanApiUrl, {{method:'DELETE', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{index:index}})}});
    if(r.ok) loadScanFreqs();
    else alert('Errore rimozione');
  }}catch(e){{alert('Errore di rete: '+e);}}
}}

document.getElementById('scan-enabled').addEventListener('change', saveScanSettings);
document.getElementById('scan-silence').addEventListener('change', saveScanSettings);
async function saveScanSettings(){{
  try{{
    const r = await fetch(scanApiUrl);
    const data = await r.json();
    data.scan_enabled = document.getElementById('scan-enabled').checked;
    data.silence_timeout_seconds = parseInt(document.getElementById('scan-silence').value) || 15;
    await fetch(scanApiUrl, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(data)}});
  }}catch(e){{console.error('Errore salvataggio settings:',e);}}
}}

// ═══ Real-time Quality Ratings on Event Cards ═══
(function(){{
  const ratingsApiUrl = 'http://'+window.location.hostname+':8073/api/auto-mode/ratings';
  
  const rateApiUrl = 'http://'+window.location.hostname+':8073/api/auto-mode/rate';
  
  function renderBigStars(key, score){{
    const safeKey = key.replace(/&/g,'&amp;').replace(/'/g,'&#39;').replace(/"/g,'&quot;');
    let html = '';
    for(let i=1; i<=5; i++){{
      const icon = i <= score ? '⭐' : '☆';
      html += '<span class="star" onclick="rateAutoQuality(event, \\x27'+safeKey+'\\x27, '+i+')">'+icon+'</span>';
    }}
    return '<span class="auto-quality-stars">'+html+'</span>';
  }}
  
  const owrxHost = 'http://'+window.location.hostname+':8073';

  function fmtDurRec(s){{
    s=Math.max(0,Math.round(s));
    if(s>=3600){{ const h=Math.floor(s/3600); const m=Math.floor((s%3600)/60); return h+'h'+String(m).padStart(2,'0')+'m'; }}
    if(s>=60){{ const m=Math.floor(s/60); const sec=s%60; return m+'m'+String(sec).padStart(2,'0')+'s'; }}
    return s+'s';
  }}
  function fmtSize(bytes){{
    if(bytes>=1048576) return (bytes/1048576).toFixed(1)+' MB';
    if(bytes>=1024) return Math.round(bytes/1024)+' KB';
    return bytes+' B';
  }}

  function renderRecordingCards(recordings, lastPositive){{
    const allRecs = [];
    const usedFiles = new Set();
    if(recordings && recordings.length > 0){{
      recordings.forEach(rec => {{
        const dateStr = rec.date ? rec.date.substring(6,8)+'/'+rec.date.substring(4,6) : '';
        const timeStr = rec.time ? rec.time.substring(0,2)+':'+rec.time.substring(2,4) : '';
        let score = 0, src = '', aq = null;
        if(lastPositive){{
          const match = lastPositive.find(p => p.recording === rec.filename);
          if(match){{ score = match.score || 0; src = match.type === 'manuale' ? '👤' : '🤖'; aq = match.audio_quality || null; usedFiles.add(rec.filename); }}
        }}
        allRecs.push({{ filename: rec.filename, date: dateStr, time: timeStr, dur: rec.duration_s||0, size: rec.size||0, score: score, src: src, aq: aq }});
      }});
    }}
    if(lastPositive){{
      lastPositive.forEach(p => {{
        if(p.recording && !usedFiles.has(p.recording)){{
          try{{
            const d = new Date(p.ts);
            allRecs.push({{ filename: p.recording, date: String(d.getUTCDate()).padStart(2,'0')+'/'+String(d.getUTCMonth()+1).padStart(2,'0'), time: String(d.getUTCHours()).padStart(2,'0')+':'+String(d.getUTCMinutes()).padStart(2,'0'), dur: 0, size: 0, score: p.score||0, src: p.type==='manuale'?'👤':'🤖', aq: p.audio_quality||null }});
          }}catch(e){{}}
          usedFiles.add(p.recording);
        }}
      }});
    }}
    if(allRecs.length === 0) return '';
    const show = allRecs.slice(-1);
    const cardId = 'rc'+Math.random().toString(36).substring(2,8);
    let html = '<div class="rec-cards" id="'+cardId+'" onclick="event.stopPropagation()">';
    show.forEach((rec, idx) => {{
      const safeFile = rec.filename.replace(/&/g,'&amp;').replace(/'/g,'&#39;').replace(/"/g,'&quot;');
      const starsHtml = rec.score > 0 ? ('★'.repeat(rec.score)+'☆'.repeat(5-rec.score)) : '';
      const audioUrl = owrxHost+'/files/'+encodeURIComponent(rec.filename);
      html += '<div class="rec-card" data-idx="'+idx+'">';
      /* header row */
      html += '<div class="rec-card-header">';
      html += '<span class="rc-icon">🎵</span>';
      html += '<span class="rc-date">'+rec.date+' '+rec.time+'</span>';
      if(rec.dur > 0) html += '<span class="rc-dur">'+fmtDurRec(rec.dur)+'</span>';
      if(rec.size > 0) html += '<span class="rc-size">'+fmtSize(rec.size)+'</span>';
      if(starsHtml) html += '<span class="rc-score" title="'+(rec.src||'')+'">'+starsHtml+'</span>';
      /* Audio quality badge from offline DSP analysis */
      if(rec.aq && rec.aq.audio_score){{
        const aqs = rec.aq.audio_score;
        const nl = rec.aq.noise_level || 0;
        const nPct = Math.round(nl*100);
        const aqCls = aqs >= 4 ? 'rc-aq-good' : (aqs >= 3 ? 'rc-aq-fair' : 'rc-aq-poor');
        html += '<span class="rc-aq '+aqCls+'" title="Qualit\u00e0 audio: '+aqs+'/5 &#10;Rumore: '+nPct+'%">';
        html += '🔊'+aqs+'/5 ';
        html += '<small>(📣'+nPct+'%)</small></span>';
      }}
      html += '<a class="rc-dl" href="'+audioUrl+'" download title="Download">⬇</a>';
      html += '<button class="rc-gemini" data-file="'+safeFile+'" title="Chiedi a Gemini AI" onclick="event.stopPropagation();openRecGemini(this.getAttribute(&apos;data-file&apos;))">🤖</button>';
      html += '</div>';
      /* spectrogram + waveform viz — full width stacked rows */
      html += '<div class="rec-viz">';
      html += '<canvas class="rv-spec"></canvas>';
      html += '<canvas class="rv-wave"></canvas>';
      html += '<div class="rv-overlay"></div>';
      html += '<span class="rv-loading">caricamento...</span>';
      html += '</div>';
      /* audio player */
      html += '<div class="rec-card-bottom"><audio controls preload="metadata" crossorigin="anonymous" src="'+audioUrl+'"></audio></div>';
      html += '</div>';
    }});
    html += '</div>';
    /* Schedule viz rendering after DOM insertion */
    setTimeout(function(){{ initRecViz(cardId); }}, 80);
    return html;
  }}

  /* ---- FFT engine (Cooley-Tukey radix-2 in-place) ---- */
  function _fft(re, im){{
    const n = re.length;
    for(let i=1,j=0; i<n; i++){{
      let bit = n>>1;
      for(; j&bit; bit>>=1) j ^= bit;
      j ^= bit;
      if(i<j){{ [re[i],re[j]]=[re[j],re[i]]; [im[i],im[j]]=[im[j],im[i]]; }}
    }}
    for(let len=2; len<=n; len<<=1){{
      const ang = 2*Math.PI/len;
      const wR = Math.cos(ang), wI = Math.sin(ang);
      for(let i=0; i<n; i+=len){{
        let curR=1, curI=0;
        for(let j=0; j<len/2; j++){{
          const tR = curR*re[i+j+len/2] - curI*im[i+j+len/2];
          const tI = curR*im[i+j+len/2] + curI*re[i+j+len/2];
          re[i+j+len/2] = re[i+j]-tR; im[i+j+len/2] = im[i+j]-tI;
          re[i+j] += tR; im[i+j] += tI;
          const nR = curR*wR - curI*wI; curI = curR*wI + curI*wR; curR = nR;
        }}
      }}
    }}
  }}

  function _specColor(v){{
    /* 0→black, 0.25→blue, 0.5→cyan, 0.75→yellow, 1→white */
    let r=0,g=0,b=0;
    if(v<0.25){{ const t=v/0.25; b=t; }}
    else if(v<0.5){{ const t=(v-0.25)/0.25; g=t; b=1; }}
    else if(v<0.75){{ const t=(v-0.5)/0.25; r=t; g=1; b=1-t; }}
    else{{ const t=(v-0.75)/0.25; r=1; g=1; b=t; }}
    return 'rgb('+Math.round(r*255)+','+Math.round(g*255)+','+Math.round(b*255)+')';
  }}

  function drawRecSpectrogram(canvas, data, sampleRate){{
    const W = canvas.clientWidth, H = canvas.clientHeight;
    if(!W||!H) return;
    canvas.width = W*2; canvas.height = H*2;
    const ctx = canvas.getContext('2d');
    ctx.scale(2,2);
    const fftSize = 256;
    const numCols = W;
    const hop = Math.max(1, Math.floor(data.length/numCols));
    const hann = new Float32Array(fftSize);
    for(let i=0;i<fftSize;i++) hann[i]=0.5*(1-Math.cos(2*Math.PI*i/(fftSize-1)));
    const numBins = fftSize/2;
    for(let col=0; col<numCols; col++){{
      const start = col*hop;
      const re = new Float32Array(fftSize);
      const im = new Float32Array(fftSize);
      for(let i=0;i<fftSize;i++) re[i] = (start+i<data.length) ? data[start+i]*hann[i] : 0;
      _fft(re, im);
      for(let row=0; row<numBins; row++){{
        const mag = Math.sqrt(re[row]*re[row]+im[row]*im[row]);
        let dB = (20*Math.log10(mag+1e-10)+60)/60;
        dB = Math.max(0, Math.min(1, dB));
        ctx.fillStyle = _specColor(dB);
        ctx.fillRect(col, H-1-(row/(numBins-1))*(H-1), 1, 1);
      }}
    }}
  }}

  function drawRecWaveform(canvas, data){{
    const W = canvas.clientWidth, H = canvas.clientHeight;
    if(!W||!H) return;
    canvas.width = W*2; canvas.height = H*2;
    const ctx = canvas.getContext('2d');
    ctx.scale(2,2);
    ctx.fillStyle = '#0a0e1a';
    ctx.fillRect(0,0,W,H);
    const binsPerPx = data.length/W;
    for(let x=0; x<W; x++){{
      const s = Math.floor(x*binsPerPx), e = Math.floor((x+1)*binsPerPx);
      let peak = 0;
      for(let i=s; i<e&&i<data.length; i++) peak = Math.max(peak, Math.abs(data[i]));
      const barH = peak*H;
      /* color ramp: low→teal, high→orange-red */
      const t = peak;
      const r = Math.round(20 + t*235);
      const g = Math.round(180 - t*100);
      const b = Math.round(160 - t*140);
      ctx.fillStyle = 'rgb('+r+','+g+','+b+')';
      ctx.fillRect(x, H-barH, 1, barH);
    }}
  }}

  let _recVizAudioCtx = null;
  function getRecAudioCtx(){{
    if(!_recVizAudioCtx){{
      _recVizAudioCtx = new (window.AudioContext || window.webkitAudioContext)({{sampleRate:22050}});
    }}
    if(_recVizAudioCtx.state === 'suspended') _recVizAudioCtx.resume();
    return _recVizAudioCtx;
  }}

  /* --- Viz cache (localStorage) to avoid re-fetching MP3s on refresh --- */
  const VIZ_CACHE_PREFIX = 'rviz_';
  const VIZ_CACHE_MAX = 50;
  function _vizCacheKey(url){{ return VIZ_CACHE_PREFIX + url.split('/').pop(); }}
  function _vizCacheGet(url){{
    try{{ const d = localStorage.getItem(_vizCacheKey(url)); return d ? JSON.parse(d) : null; }}catch(e){{ return null; }}
  }}
  function _vizCachePut(url, specData, waveData){{
    try{{
      const keys = []; for(let i=0;i<localStorage.length;i++){{ const k=localStorage.key(i); if(k&&k.startsWith(VIZ_CACHE_PREFIX)) keys.push(k); }}
      while(keys.length >= VIZ_CACHE_MAX){{ localStorage.removeItem(keys.shift()); }}
      localStorage.setItem(_vizCacheKey(url), JSON.stringify({{s:specData,w:waveData,t:Date.now()}}));
    }}catch(e){{/* quota exceeded, ignore */}}
  }}

  function initRecViz(containerId){{
    const wrap = document.getElementById(containerId);
    if(!wrap) return;
    const cards = wrap.querySelectorAll('.rec-card');
    cards.forEach(function(card){{
      const audio = card.querySelector('audio');
      const viz = card.querySelector('.rec-viz');
      const specC = card.querySelector('.rv-spec');
      const waveC = card.querySelector('.rv-wave');
      const overlay = card.querySelector('.rv-overlay');
      const loading = card.querySelector('.rv-loading');
      if(!audio||!viz||!specC||!waveC) return;
      let vizDone = false;
      function loadViz(){{
        if(vizDone) return;
        vizDone = true;
        const src = audio.src || audio.querySelector('source')?.src;
        if(!src){{ if(loading) loading.textContent='⚠ no src'; return; }}
        /* Try cache first */
        const cached = _vizCacheGet(src);
        if(cached && cached.s && cached.w){{
          const specImg = new Image(); specImg.onload = function(){{
            const W=specC.clientWidth, H=specC.clientHeight; specC.width=W*2; specC.height=H*2;
            const sCtx=specC.getContext('2d'); sCtx.scale(2,2); sCtx.drawImage(specImg,0,0,W,H);
          }}; specImg.src = cached.s;
          const waveImg = new Image(); waveImg.onload = function(){{
            const W=waveC.clientWidth, H=waveC.clientHeight; waveC.width=W*2; waveC.height=H*2;
            const wCtx=waveC.getContext('2d'); wCtx.scale(2,2); wCtx.drawImage(waveImg,0,0,W,H);
          }}; waveImg.src = cached.w;
          if(loading) loading.style.display='none';
          return;
        }}
        if(loading) loading.textContent='decodifica audio...';
        const ctx = getRecAudioCtx();
        fetch(src).then(function(r){{
          if(!r.ok) throw new Error('HTTP '+r.status);
          return r.arrayBuffer();
        }}).then(function(buf){{
          return ctx.decodeAudioData(buf);
        }}).then(function(decoded){{
          const pcm = decoded.getChannelData(0);
          drawRecSpectrogram(specC, pcm, decoded.sampleRate);
          drawRecWaveform(waveC, pcm);
          if(loading) loading.style.display='none';
          /* Cache rendered canvas images */
          try{{ _vizCachePut(src, specC.toDataURL('image/png',0.6), waveC.toDataURL('image/png',0.6)); }}catch(ce){{}}
        }}).catch(function(e){{
          console.error('rec-viz error:', e);
          if(loading) loading.textContent='⚠ '+e.message;
        }});
      }}
      /* Auto-load immediately — small MP3s, parallel fetch is fine */
      loadViz();
      /* Playback progress overlay */
      audio.addEventListener('timeupdate', function(){{
        if(audio.duration > 0){{
          overlay.style.width = (audio.currentTime/audio.duration*100)+'%';
        }}
      }});
      /* Click-to-seek on viz */
      viz.addEventListener('click', function(e){{
        const ctx2 = getRecAudioCtx();
        if(audio.duration){{
          const rect = viz.getBoundingClientRect();
          audio.currentTime = ((e.clientX - rect.left)/rect.width) * audio.duration;
          if(audio.paused) audio.play();
        }} else {{
          audio.play();
        }}
      }});
    }});
  }}

  function renderQualityBadgeOnly(r, key){{
    /* Same as renderQualityBadge but WITHOUT recording cards — for incremental updates */
    if(!r){{
      return '<div class="aq-row">'+renderBigStars(key, 0) + '<span class="aq-info" title="Clicca le stelle per votare">⏳ In attesa</span></div>';
    }}
    const total = r.total_ratings || 0;
    const nr = r.consecutive_nr || 0;
    const avg = r.avg_score;
    const enabled = r.enabled !== false;
    const scored = r.scored_count || 0;
    const nrTotal = r.nr_count || 0;
    const detailStr = total > 0 ? ' ('+scored+'/'+total+' con segnale)' : '';
    if(!enabled){{
      return '<div class="aq-row">'+renderBigStars(key, 0) + '<span class="aq-info aq-disabled" title="Disabilitato dopo '+nr+' NR consecutivi ('+nrTotal+' NR su '+total+')">⛔ Disabilitato'+detailStr+'</span></div>';
    }}
    const displayScore = avg !== null && avg !== undefined ? Math.round(avg) : 0;
    const starsHtml = renderBigStars(key, displayScore);
    if(total === 0){{
      return '<div class="aq-row">'+starsHtml + '<span class="aq-info" title="Clicca le stelle per votare">⏳ In attesa</span></div>';
    }}
    if(avg === null || avg === undefined || nr >= 3){{
      return '<div class="aq-row">'+starsHtml + '<span class="aq-info aq-nr" title="Segnale assente x'+nr+' consecutivi ('+nrTotal+' NR su '+total+' scansioni)">📡 ×'+nr+detailStr+'</span></div>';
    }}
    const cls = avg <= 1.5 ? 'aq-low' : (avg <= 3.0 ? 'aq-mid' : 'aq-high');
    return '<div class="aq-row">'+starsHtml + '<span class="aq-info '+cls+'" title="Media auto: '+avg.toFixed(1)+'/5 — '+scored+' con segnale, '+nrTotal+' NR su '+total+' scansioni">'+avg.toFixed(1)+'/5'+detailStr+'</span></div>';
  }}

  function _recFingerprint(entry){{
    /* Build a fingerprint of recording filenames for change detection */
    const names = [];
    if(entry && entry.recordings) entry.recordings.forEach(r => names.push(r.filename||''));
    if(entry && entry.last_positive) entry.last_positive.forEach(p => {{ if(p.recording) names.push(p.recording); }});
    return names.sort().join('|');
  }}

  function renderQualityBadge(r, key){{
    if(!r){{
      return '<div class="aq-row">'+renderBigStars(key, 0) + '<span class="aq-info" title="Clicca le stelle per votare">⏳ In attesa</span></div>';
    }}
    const total = r.total_ratings || 0;
    const nr = r.consecutive_nr || 0;
    const avg = r.avg_score;
    const enabled = r.enabled !== false;
    const scored = r.scored_count || 0;
    const nrTotal = r.nr_count || 0;
    const detailStr = total > 0 ? ' ('+scored+'/'+total+' con segnale)' : '';
    
    if(!enabled){{
      return '<div class="aq-row">'+renderBigStars(key, 0) + '<span class="aq-info aq-disabled" title="Disabilitato dopo '+nr+' NR consecutivi ('+nrTotal+' NR su '+total+')">⛔ Disabilitato'+detailStr+'</span></div>' + renderRecordingCards(r.recordings, r.last_positive);
    }}
    const displayScore = avg !== null && avg !== undefined ? Math.round(avg) : 0;
    const starsHtml = renderBigStars(key, displayScore);
    
    if(total === 0){{
      return '<div class="aq-row">'+starsHtml + '<span class="aq-info" title="Clicca le stelle per votare">⏳ In attesa</span></div>';
    }}
    if(avg === null || avg === undefined || nr >= 3){{
      return '<div class="aq-row">'+starsHtml + '<span class="aq-info aq-nr" title="Segnale assente x'+nr+' consecutivi ('+nrTotal+' NR su '+total+' scansioni)">📡 ×'+nr+detailStr+'</span></div>' + renderRecordingCards(r.recordings, r.last_positive);
    }}
    const cls = avg <= 1.5 ? 'aq-low' : (avg <= 3.0 ? 'aq-mid' : 'aq-high');
    return '<div class="aq-row">'+starsHtml + '<span class="aq-info '+cls+'" title="Media auto: '+avg.toFixed(1)+'/5 — '+scored+' con segnale, '+nrTotal+' NR su '+total+' scansioni">'+avg.toFixed(1)+'/5'+detailStr+'</span></div>' + renderRecordingCards(r.recordings, r.last_positive);
  }}
  
  // Aggregate ratings by description across ALL frequencies
  function buildDescLookup(ratings){{
    const byDesc = {{}};
    for(const [key, entry] of Object.entries(ratings)){{
      const pipeIdx = key.indexOf('|');
      if(pipeIdx < 0) continue;
      const desc = key.substring(pipeIdx + 1);
      if(!byDesc[desc]){{
        byDesc[desc] = {{avg_score:null, consecutive_nr:0, enabled:true, total_ratings:0, nr_count:0, scored_count:0, last_positive:[], recordings:[], _scores:[], _nr_counts:[]}};
      }}
      const agg = byDesc[desc];
      agg.total_ratings += (entry.total_ratings || 0);
      agg.nr_count += (entry.nr_count || 0);
      agg.scored_count += (entry.scored_count || 0);
      if(entry.last_positive) agg.last_positive = agg.last_positive.concat(entry.last_positive);
      if(entry.recordings) agg.recordings = agg.recordings.concat(entry.recordings);
      agg._nr_counts.push(entry.consecutive_nr || 0);
      if(entry.enabled === false) agg.enabled = false;
      if(entry.avg_score !== null && entry.avg_score !== undefined){{
        agg._scores.push(entry.avg_score);
      }}
    }}
    // Finalize aggregates
    for(const agg of Object.values(byDesc)){{
      if(agg._scores.length > 0){{
        agg.avg_score = agg._scores.reduce((a,b)=>a+b, 0) / agg._scores.length;
      }}
      agg.consecutive_nr = Math.min(...agg._nr_counts.length ? agg._nr_counts : [0]);
      if(agg.last_positive && agg.last_positive.length > 10){{
        agg.last_positive.sort((a,b) => (a.ts > b.ts ? 1 : -1));
        agg.last_positive = agg.last_positive.slice(-5);
      }}
      delete agg._scores;
      delete agg._nr_counts;
    }}
    return byDesc;
  }}
  
  async function refreshCardRatings(){{
    try{{
      const ctrl = new AbortController();
      setTimeout(()=>ctrl.abort(), 4000);
      const r = await fetch(ratingsApiUrl, {{signal:ctrl.signal}});
      if(!r.ok) return;
      const ratings = await r.json();
      const byDesc = buildDescLookup(ratings);
      
      document.querySelectorAll('.transmission[data-rating-key]').forEach(card=>{{
        const key = card.getAttribute('data-rating-key');
        const container = card.querySelector('.auto-quality-container');
        if(!container) return;
        // 1. Try exact key match (freq|desc)
        let entry = ratings[key];
        // 2. Fallback: match by description only (aggregated across all freqs)
        if(!entry){{
          const pipeIdx = key.indexOf('|');
          if(pipeIdx >= 0){{
            const desc = key.substring(pipeIdx + 1);
            entry = byDesc[desc];
          }}
        }}
        /* --- Incremental update: avoid destroying initialized spectrograms --- */
        const existingCards = container.querySelector('.rec-cards');
        const hasViz = existingCards && existingCards.querySelector('canvas[width]');
        if(hasViz){{
          /* Only update the badge row, preserve recording cards + viz */
          const oldRow = container.querySelector('.aq-row');
          const newBadgeHtml = renderQualityBadgeOnly(entry, key);
          if(oldRow){{
            const tmp = document.createElement('div');
            tmp.innerHTML = newBadgeHtml;
            const newRow = tmp.firstChild;
            if(newRow) oldRow.replaceWith(newRow);
          }}
          /* Check if recordings changed — if so, rebuild cards only */
          const oldFp = container.getAttribute('data-rec-fp') || '';
          const newFp = _recFingerprint(entry);
          if(oldFp !== newFp){{
            container.innerHTML = renderQualityBadge(entry, key);
            container.setAttribute('data-rec-fp', newFp);
          }}
        }} else {{
          /* First time or no viz yet — full render */
          container.innerHTML = renderQualityBadge(entry, key);
          container.setAttribute('data-rec-fp', _recFingerprint(entry));
        }}
        // Toggle disabled class on card
        if(entry && entry.enabled === false){{ card.classList.add('disabled'); }}
        else{{ card.classList.remove('disabled'); }}
      }});
    }}catch(e){{/* ignore fetch errors */}}
  }}
  
  // Initial fetch + periodic refresh every 15 seconds
  refreshCardRatings();
  setInterval(refreshCardRatings, 15000);
}})();
</script>'''
    
    # Crea array JavaScript con i dati delle trasmissioni
    tx_data_js = []
    uncovered_freqs = []
    auto_create_profiles = False  # Imposta True per creare profili automaticamente
    
    for i, tx in enumerate(txs):
        # Verifica copertura profilo
        is_covered, profile_name = check_profile_coverage(tx['freq'])
        if not is_covered:
            uncovered_freqs.append((tx['freq'], tx['description']))
            if auto_create_profiles:
                success, new_profile = create_profile_for_frequency(tx['freq'], tx['description'])
                if success:
                    is_covered = True
        
        url = build_openwebrx_url(tx['freq'], tx['mode'], tx['bandwidth'], tx['decoder'], tx.get('type'))
        # Escape per JSON/JS (backslash prima delle virgolette)
        desc_escaped = tx['description'].replace('\\', '\\\\').replace('"', '\\"')
        tx_key = get_transmission_key(tx)
        tx_key_escaped = tx_key.replace('\\', '\\\\').replace('"', '\\"')
        on_air_flag = 'true' if tx.get('on_air') else 'false'
        if tx.get('on_air') and tx.get('minutes_to_end') is not None:
            end_ms = int((now + timedelta(minutes=tx['minutes_to_end'])).timestamp() * 1000)
        else:
            end_ms = int(tx['target_time'].timestamp() * 1000)
        tx_data_js.append(f'{{"time":"{tx["time"]}","targetMs":{int(tx["target_time"].timestamp()*1000)},"endMs":{end_ms},"onAir":{on_air_flag},"freq":"{tx["freq"]}","desc":"{desc_escaped}","mode":"{tx["mode"]}","bw":"{tx["bandwidth"]}","decoder":"{tx["decoder"]}","url":"{url}","key":"{tx_key_escaped}"}}')
    
    # Mostra warning se ci sono frequenze non coperte
    if uncovered_freqs:
        print(f"\n⚠️  ATTENZIONE: {len(uncovered_freqs)} frequenze NON coperte da profili OpenWebRX:", file=sys.stderr)
        for freq, desc in uncovered_freqs:
            print(f"   - {freq} MHz ({desc})", file=sys.stderr)
        if auto_create_profiles:
            print(f"\n   Riavvia OpenWebRX: systemctl restart openwebrx\n", file=sys.stderr)
        else:
            print(f"\n   Per creare automaticamente, imposta auto_create_profiles=True nel codice", file=sys.stderr)
            print(f"   OPPURE crea manualmente in OpenWebRX Settings\n", file=sys.stderr)
    
    for i, tx in enumerate(txs):
        m = tx['delta_minutes']
        if tx.get('on_air') and tx.get('minutes_to_end') is not None:
            mte = tx['minutes_to_end']
            if mte >= 60:
                countdown = f"IN CORSO \u2014 fine tra {int(mte//60)}h {int(mte%60)}m"
            else:
                countdown = f"IN CORSO \u2014 fine tra {int(mte)}min"
        elif m < 0:
            countdown = f"IN CORSO, iniziato alle {tx['time']}"
        elif m >= 60:
            countdown = f"TRA {int(m//60)}h {int(m%60)}m"
        else:
            countdown = f"TRA {int(m)}min"
        if tx.get('on_air') and tx.get('end_utc'):
            time_display = f"{tx['time']} \u2192 {tx['end_utc']} UTC"
        else:
            time_display = f"{tx['time']} UTC"
        
        # Chiave trasmissione (prima di tutto)
        tx_key = get_transmission_key(tx)
        tx_key_html = tx_key.replace('&', '&amp;').replace('"', '&quot;').replace("'", '&#39;')
        tx_key_js = tx_key.replace("'", "\\'").replace('"', '\\"')
        
        # Recupera log per questa trasmissione
        tx_log = get_tx_log(tx, log_data)
        cls = "next" if i == 0 else ""
        if tx_log['heard']:
            cls += " heard"
        
        # Escape descrizione per HTML (previene XSS)
        desc_html = html_lib.escape(tx['description'])
        
        # Rating key for quality system (same format as orchestrator: freq|desc)
        rating_key = "{}|{}".format(tx['freq'], tx['description'])
        rating_key_html = rating_key.replace('&', '&amp;').replace('"', '&quot;')
        
        # Auto-quality badge dall'orchestratore (unified big stars)
        auto_rating = get_auto_rating(tx, ratings_db)
        auto_quality_html = ""
        rating_key_js = rating_key.replace("'", "\\'").replace('"', '\\"')
        if auto_rating:
            avg = auto_rating['avg_score']
            nr = auto_rating['consecutive_nr']
            enabled = auto_rating['enabled']
            total = auto_rating['total_ratings']
            scored = auto_rating.get('scored_count', 0)
            nr_total = auto_rating.get('nr_count', 0)
            detail_str = f"({scored}/{total} con segnale)" if total > 0 else ""
            
            if not enabled:
                # Disabled station: show stars (zeroed) + badge
                stars_big_dis = ""
                for sn in range(1, 6):
                    stars_big_dis += f'<span class="star" onclick="rateAutoQuality(event, \'{rating_key_js}\', {sn})">☆</span>'
                auto_quality_html = f'<div class="aq-row"><span class="auto-quality-stars">{stars_big_dis}</span><span class="aq-info aq-disabled" title="Disabilitato dopo {nr} segnali assenti consecutivi ({nr_total} NR su {total})">⛔ Disabilitato {detail_str}</span></div>'
                cls += " disabled"
            else:
                # Render big clickable stars based on avg_score
                display_score = int(round(avg)) if avg is not None else 0
                stars_big = ""
                for sn in range(1, 6):
                    icon = "⭐" if sn <= display_score else "☆"
                    stars_big += f'<span class="star" onclick="rateAutoQuality(event, \'{rating_key_js}\', {sn})">{icon}</span>'
                
                if avg is None and nr >= 3:
                    info = f'<span class="aq-info aq-nr" title="Segnale assente x{nr} consecutivi ({nr_total} NR su {total} scansioni)">📡 Segnale assente ×{nr} {detail_str}</span>'
                elif avg is not None:
                    quality_cls = "aq-low" if avg <= 1.5 else ("aq-mid" if avg <= 3.0 else "aq-high")
                    info = f'<span class="aq-info {quality_cls}" title="Media auto: {avg:.1f}/5 — {scored} con segnale, {nr_total} NR su {total} scansioni">{avg:.1f}/5 {detail_str}</span>'
                elif total > 0:
                    info = f'<span class="aq-info aq-nr" title="Segnale assente x{nr}">📡 ×{nr} {detail_str}</span>'
                else:
                    info = '<span class="aq-info" title="Nessuna valutazione ancora">⏳ In attesa</span>'
                auto_quality_html = f'<div class="aq-row"><span class="auto-quality-stars">{stars_big}</span>{info}</div>'
        else:
            # No rating at all - show empty clickable stars
            stars_big = ""
            for sn in range(1, 6):
                stars_big += f'<span class="star" onclick="rateAutoQuality(event, \'{rating_key_js}\', {sn})">☆</span>'
            auto_quality_html = f'<div class="aq-row"><span class="auto-quality-stars">{stars_big}</span><span class="aq-info" title="Clicca le stelle per votare">⏳ In attesa</span></div>'
        
        heard_class = "" if tx_log['heard'] else "not-heard"
        heard_text = "✅ Ascoltato" if tx_log['heard'] else "⚪ Mai ricevuto"
        
        # Counter con data ultimo ascolto
        if tx_log['hear_count'] > 0 and tx_log['last_heard']:
            last = datetime.fromisoformat(tx_log['last_heard'].replace('Z', '+00:00'))
            last_str = last.strftime('%d/%m %H:%M')
            count_info = f"({tx_log['hear_count']}x) - Ultimo: {last_str} UTC"
        elif tx_log['hear_count'] > 0:
            count_info = f"({tx_log['hear_count']}x)"
        else:
            count_info = ""
        
        # Riusa URL e profile già calcolati nel primo loop
        url = build_openwebrx_url(tx['freq'], tx['mode'], tx['bandwidth'], tx['decoder'], tx.get('type'))
        is_covered, profile_name = check_profile_coverage(tx['freq'])
        if not is_covered:
            profile_name = "NESSUNO"
        
        # Escape delle virgolette per JavaScript
        desc_js_escaped = tx['description'].replace("'", "\\'").replace('"', '\\"')
        rating_desc_html = tx['description'].replace('&', '&amp;').replace('"', '&quot;')
        
        # Usa l'URL corretto con offset e secondary_mod già calcolati
        html += f'''<div class="transmission {cls}" data-key="{tx_key_html}" data-rating-key="{rating_key_html}" data-profile="{profile_name}" data-freq="{tx['freq']}" onclick="window.open('{url}', '_blank'); event.stopPropagation();">
<div class="tx-header">
<div class="tx-time">{time_display}</div>
<div class="tx-countdown" data-idx="{i}">{countdown}</div>
</div>
<div class="tx-freq">📡 {tx['freq']} MHz</div>
<div class="tx-desc">{desc_html}</div>
<div class="tx-stats">
<span class="auto-quality-container">{auto_quality_html}</span>
<span class="tx-heard {heard_class}" onclick="markHeard(event, '{tx_key_js}')">{heard_text}</span>
<span class="tx-count">{count_info}</span>
</div>
<div class="tx-technical">
<div class="tech-item"><div class="tech-label">MODO</div><div class="tech-value">{tx['mode']}</div></div>
<div class="tech-item"><div class="tech-label">BANDWIDTH</div><div class="tech-value">{tx['bandwidth']} kHz</div></div>
<div class="tech-item"><div class="tech-label">DECODER</div><div class="tech-value">{tx['decoder']}</div></div>
</div></div>'''
    
    html += f'''</div><script>
const transmissions = [{','.join(tx_data_js)}];

// Funzioni per logging
async function markHeard(event, key) {{
    event.stopPropagation();
    try {{
        const response = await fetch('/sdr-log-save', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{action: 'heard', key: key}})
        }});
        if (response.ok) {{
            // Toggle stato ascoltato
            const container = event.target.closest('.transmission');
            const badge = event.target;
            const isHeard = !badge.classList.contains('not-heard');
            
            if (isHeard) {{
                // Era già ascoltato, rimuovi
                badge.textContent = '⚪ Mai ricevuto';
                badge.classList.add('not-heard');
                container.classList.remove('heard');
            }} else {{
                // Non era ascoltato, aggiungi
                badge.textContent = '✅ Ascoltato';
                badge.classList.remove('not-heard');
                container.classList.add('heard');
                
                // Aggiorna counter e data
                const counter = container.querySelector('.tx-count');
                const text = counter.textContent;
                const match = text.match(/(\\d+)x/);
                const currentCount = match ? parseInt(match[1]) : 0;
                const now = new Date().toLocaleString('it-IT', {{timeZone: 'UTC'}});
                counter.textContent = `(${{currentCount + 1}}x) - Ultimo: ${{now}} UTC`;
            }}
        }} else {{
            console.error('Errore salvataggio:', await response.text());
        }}
    }} catch(e) {{
        console.error('Errore fetch:', e);
    }}
}}

async function rateAutoQuality(event, ratingKey, score) {{
    event.stopPropagation();
    try {{
        const rateUrl = 'http://'+window.location.hostname+':8073/api/auto-mode/rate';
        const response = await fetch(rateUrl, {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{key: ratingKey, score: score}})
        }});
        if (response.ok) {{
            // Update stars visually immediately
            const container = event.target.closest('.auto-quality-stars');
            if(container){{
                const stars = container.querySelectorAll('.star');
                stars.forEach((star, idx) => {{
                    star.textContent = idx < score ? '⭐' : '☆';
                }});
            }}
            console.log('🌟 Voto manuale:', ratingKey, '=', score);
        }} else {{
            console.error('Errore salvataggio rating auto:', await response.text());
        }}
    }} catch(e) {{
        console.error('Errore fetch rating auto:', e);
    }}
}}

function updateCountdowns() {{
    const now = Date.now();
    let staleCount = 0;
    transmissions.forEach((tx, idx) => {{
        let countdown;
        const card = document.querySelector(`.transmission[data-idx-tx="${{idx}}"]`) ||
                     (document.querySelectorAll('.transmission')[idx]);
        if (tx.onAir) {{
            const endDiff = tx.endMs - now;
            const endMin = Math.floor(endDiff / 60000);
            if (endDiff <= 0) {{
                const goneMin = Math.floor(-endDiff / 60000);
                if (goneMin >= 5) {{
                    countdown = "TERMINATO";
                    staleCount++;
                    if(card) {{ card.style.opacity='0.3'; card.style.filter='saturate(0.2)'; }}
                }} else {{
                    countdown = `TERMINATO da ${{goneMin > 0 ? goneMin+'min' : 'poco'}}`;
                    if(card) {{ card.style.opacity='0.5'; card.style.filter='saturate(0.4)'; }}
                }}
            }} else if (endMin >= 60) {{
                const h = Math.floor(endMin / 60), m = endMin % 60;
                countdown = `IN CORSO \u2014 fine tra ${{h}}h ${{m}}m`;
            }} else if (endMin > 0) {{
                const s = Math.floor((endDiff % 60000) / 1000);
                countdown = `IN CORSO \u2014 fine tra ${{endMin}}m ${{s}}s`;
            }} else {{
                const s = Math.floor(endDiff / 1000);
                countdown = `IN CORSO \u2014 fine tra ${{s}}s`;
            }}
        }} else {{
            const diffMs = tx.targetMs - now;
            const diffMin = Math.floor(diffMs / 60000);
            const diffSec = Math.floor((diffMs % 60000) / 1000);
            if (diffMin < -60) {{
                countdown = "TERMINATO";
                staleCount++;
                if(card) {{ card.style.opacity='0.3'; card.style.filter='saturate(0.2)'; }}
            }} else if (diffMin < 0) {{
                countdown = `IN CORSO, iniziato alle ${{tx.time}}`;
            }} else if (diffMin >= 60) {{
                const h = Math.floor(diffMin / 60);
                const m = diffMin % 60;
                countdown = `TRA ${{h}}h ${{m}}m`;
            }} else if (diffMin > 0) {{
                countdown = `TRA ${{diffMin}}min ${{diffSec}}s`;
            }} else {{
                countdown = `TRA ${{diffSec}}s`;
            }}
        }}
        const el = document.querySelector(`[data-idx="${{idx}}"]`);
        if (el) el.textContent = countdown;
    }});
    // Auto-reload quando >40% delle card sono terminate (dati stale)
    if (staleCount > 0 && staleCount >= Math.ceil(transmissions.length * 0.4)) {{
        console.log('🔄 Troppe card terminate, reload pagina...');
        location.reload();
    }}
}}

// Aggiorna orologio, data, orario locale e countdown ogni secondo
setInterval(() => {{
    const now = new Date();
    document.getElementById("time").textContent = now.toISOString().substr(11,8);
    const days = ['Domenica','Lunedì','Martedì','Mercoledì','Giovedì','Venerdì','Sabato'];
    const months = ['Gennaio','Febbraio','Marzo','Aprile','Maggio','Giugno','Luglio','Agosto','Settembre','Ottobre','Novembre','Dicembre'];
    document.getElementById("date").textContent = days[now.getUTCDay()] + ' ' + now.getUTCDate() + ' ' + months[now.getUTCMonth()] + ' ' + now.getUTCFullYear();
    const lt = document.getElementById("localtime");
    if (lt) {{
        const lh = String(now.getHours()).padStart(2,'0');
        const lm = String(now.getMinutes()).padStart(2,'0');
        const offset = -now.getTimezoneOffset();
        const sign = offset >= 0 ? '+' : '-';
        const oh = String(Math.floor(Math.abs(offset)/60)).padStart(2,'0');
        const om = String(Math.abs(offset)%60).padStart(2,'0');
        lt.textContent = `${{lh}}:${{lm}} locale (UTC${{sign}}${{oh}}:${{om}})`;
    }}
    updateCountdowns();
}}, 1000);

// Esegui subito il primo aggiornamento
updateCountdowns();

// ---- Sort cards ----
function sortCards(btn) {{
  document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const mode = btn.getAttribute('data-sort');
  const container = document.querySelector('.container');
  const cards = Array.from(container.querySelectorAll('.transmission'));
  if(cards.length === 0) return;

  cards.forEach((card, idx) => {{
    card._txIdx = idx;
    // Stelle: conta le stelle piene nel badge auto-quality
    const stars = card.querySelectorAll('.auto-quality-stars .star');
    let starCount = 0;
    stars.forEach(s => {{ if(s.textContent === '⭐') starCount++; }});
    card._stars = starCount;
    // Tempo: usa transmissions array
    if(idx < transmissions.length) {{
      const tx = transmissions[idx];
      if(tx.onAir) {{
        card._timeKey = -(tx.endMs || 0); // on-air: ordina per fine (negativo = prima)
      }} else {{
        card._timeKey = tx.targetMs; // upcoming: ordina per inizio
      }}
    }} else {{
      card._timeKey = Infinity;
    }}
  }});

  if(mode === 'time-asc') {{
    cards.sort((a,b) => a._timeKey - b._timeKey);
  }} else if(mode === 'time-desc') {{
    cards.sort((a,b) => b._timeKey - a._timeKey);
  }} else if(mode === 'stars-desc') {{
    cards.sort((a,b) => b._stars - a._stars || a._timeKey - b._timeKey);
  }} else if(mode === 'stars-asc') {{
    cards.sort((a,b) => a._stars - b._stars || a._timeKey - b._timeKey);
  }}

  // Rimuovi classe 'next' da tutti e rimetti sulla prima card
  cards.forEach(c => c.classList.remove('next'));
  if(cards.length > 0) cards[0].classList.add('next');

  // Re-insert in DOM order
  const frag = document.createDocumentFragment();
  cards.forEach(c => frag.appendChild(c));
  // Insert before footer/script
  const footer = container.querySelector('.footer');
  if(footer) container.insertBefore(frag, footer);
  else container.appendChild(frag);
}}
</script>
<div class="footer">SDR Schedule Widget v2.1 &nbsp;|&nbsp; OpenWebRX+ v1.2.106 &nbsp;|&nbsp; Generato: {update_time} &nbsp;|&nbsp; <a href="{OPENWEBRX_URL}/recordings" target="_blank" style="color:#FF5722;text-decoration:none;font-weight:bold">🔴 Registrazioni</a></div>
<!-- Gemini AI Modal for recordings -->
<div id="recGeminiOverlay" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);z-index:9999;justify-content:center;align-items:center;">
  <div style="background:#1a1a2e;border:1px solid #7c3aed;border-radius:10px;width:92%;max-width:560px;max-height:80vh;overflow-y:auto;padding:18px;color:#e0e0e0;box-shadow:0 0 30px rgba(124,58,237,.3);">
    <h3 style="color:#a78bfa;margin:0 0 10px;font-size:1.1rem;">🤖 Gemini AI Analysis</h3>
    <div id="rgFile" style="color:#00d4ff;font-size:.82rem;margin-bottom:8px;"></div>
    <div id="rgResult" style="background:#0a0e1a;border:1px solid #333;border-radius:6px;padding:12px;margin:10px 0;font-size:.82rem;line-height:1.5;white-space:pre-wrap;word-wrap:break-word;min-height:60px;color:#888;font-style:italic;">Premi "Analizza" per chiedere a Gemini...</div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;">
      <button onclick="document.getElementById('recGeminiOverlay').style.display='none'" style="padding:6px 16px;border-radius:6px;border:none;cursor:pointer;font-size:.82rem;font-weight:600;background:#333;color:#ccc;">Chiudi</button>
      <button id="rgAskBtn" onclick="doRecGeminiAnalyze()" style="padding:6px 16px;border-radius:6px;border:none;cursor:pointer;font-size:.82rem;font-weight:600;background:#7c3aed;color:#fff;">🤖 Analizza</button>
    </div>
  </div>
</div>
<script>
var _rgFile = null;
function openRecGemini(filename) {{
  _rgFile = filename;
  document.getElementById('rgFile').textContent = '📁 ' + filename;
  document.getElementById('rgResult').innerHTML = '<span style="color:#888;font-style:italic">Premi "Analizza" per chiedere a Gemini...</span>';
  document.getElementById('recGeminiOverlay').style.display = 'flex';
}}
function doRecGeminiAnalyze() {{
  if(!_rgFile) return;
  var btn = document.getElementById('rgAskBtn');
  btn.disabled = true; btn.textContent = '⏳ Analisi...';
  document.getElementById('rgResult').innerHTML = '<span style="color:#888;font-style:italic">Invio audio a Gemini AI... potrebbe richiedere alcuni secondi.</span>';
  fetch('{OPENWEBRX_URL}/api/gemini/analyze-recording', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{filename:_rgFile}})
  }}).then(function(r){{ return r.json(); }}).then(function(data){{
    if(data.text) document.getElementById('rgResult').textContent = data.text;
    else if(data.error) document.getElementById('rgResult').innerHTML = '<span style="color:#f44">Errore: '+data.error+'</span>';
  }}).catch(function(e){{
    document.getElementById('rgResult').innerHTML = '<span style="color:#f44">Errore: '+e.message+'</span>';
  }}).finally(function(){{
    btn.disabled = false; btn.textContent = '🤖 Analizza';
  }});
}}
document.getElementById('recGeminiOverlay').addEventListener('click', function(e){{ if(e.target===this) this.style.display='none'; }});
</script>
</div></body></html>'''
    return html

def generate_json():
    txs = get_next_transmissions(25)
    data = {'generated_at': datetime.now(timezone.utc).isoformat(), 'openwebrx_url': OPENWEBRX_URL, 'next_transmissions': []}
    for tx in txs:
        entry = {
            'time_utc': tx['time'], 'frequency_mhz': tx['freq'], 'description': tx['description'],
            'mode': tx['mode'], 'bandwidth': tx['bandwidth'], 'decoder': tx['decoder'],
            'minutes_until': int(tx['delta_minutes']),
            'on_air': bool(tx.get('on_air')),
            'end_utc': tx.get('end_utc'),
            'minutes_to_end': round(tx['minutes_to_end'], 1) if tx.get('minutes_to_end') is not None else None,
            'openwebrx_link': build_openwebrx_url(tx['freq'], tx['mode'], tx['bandwidth'], tx['decoder'], tx.get('type'))
        }
        data['next_transmissions'].append(entry)
    return json.dumps(data, indent=2, ensure_ascii=False)

if __name__ == '__main__':
    with open('/var/www/html/sdr-schedule.html', 'w', encoding='utf-8') as f:
        f.write(generate_html())
    print("✓ HTML: http://192.168.1.132:8080/sdr-schedule.html", file=sys.stderr)
    with open('/var/www/html/sdr-schedule.json', 'w', encoding='utf-8') as f:
        f.write(generate_json())
    print("✓ JSON: http://192.168.1.132:8080/sdr-schedule.json", file=sys.stderr)

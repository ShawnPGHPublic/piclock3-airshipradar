"""Ship and aircraft feeds. No Qt.

Ships: Open Waters (no key), AISHub, or AISStream.
Aircraft: adsb.lol (no key), with OpenSky as the other free web feed.
"""
import json
import math

try:
    from .radarmath import (
        bounding_box,
        parse_aishub,
        parse_aisstream,
        ship_dims,
        ship_kind,
        _angle,
        _num,
        _short,
        _sog,
    )
except ImportError:
    from radarmath import (
        bounding_box,
        parse_aishub,
        parse_aisstream,
        ship_dims,
        ship_kind,
        _angle,
        _num,
        _short,
        _sog,
    )

M_PER_S_TO_KT = 1.943844
M_TO_FT = 3.28084
ADSB = 'https://api.adsb.lol/v2/lat/%s/lon/%s/dist/%s'
OPENSKY = 'https://opensky-network.org/api/states/all'
OPENWATERS = 'https://ais.openwaters.io/v1/vessels'
AIS_HUB = 'https://data.aishub.net/ws.php'
# Open Waters anonymous reads are capped near 100 square degrees.
MAX_BOX_DEG2 = 90.0


def box_too_wide(box):
    area = abs(box['latmax'] - box['latmin']) * abs(box['lonmax'] - box['lonmin'])
    return area > MAX_BOX_DEG2


def openwaters_url(lat, lon, radius_m):
    box = bounding_box(lat, lon, radius_m)
    if box_too_wide(box):
        return None
    return OPENWATERS + '?bbox=%.5f,%.5f,%.5f,%.5f' % (
        box['latmin'], box['lonmin'], box['latmax'], box['lonmax'])


def aishub_url(username, lat, lon, radius_m, minutes):
    box = bounding_box(lat, lon, radius_m)
    from urllib.parse import quote
    return (
        '%s?username=%s&format=1&output=json&compress=0'
        '&latmin=%.5f&latmax=%.5f&lonmin=%.5f&lonmax=%.5f&interval=%d'
        % (AIS_HUB, quote(username), box['latmin'], box['latmax'],
           box['lonmin'], box['lonmax'], minutes)
    )


def adsb_url(lat, lon, radius_m):
    nm = int(round(radius_m / 1852.0))
    nm = max(1, min(250, nm))
    return ADSB % (('%.5f' % lat), ('%.5f' % lon), nm)


def opensky_url(lat, lon, radius_m):
    box = bounding_box(lat, lon, radius_m)
    return (
        '%s?lamin=%.5f&lomin=%.5f&lamax=%.5f&lomax=%.5f'
        % (OPENSKY, box['latmin'], box['lonmin'], box['latmax'], box['lonmax'])
    )


def ships_from_openwaters(body):
    """(vessels, error). A clear box is a success."""
    data = _json(body)
    if not isinstance(data, dict):
        return [], 'bad reply'
    if data.get('error'):
        return [], _short(data.get('error'))
    rows = data.get('features')
    if rows is None:
        return [], 'bad reply'
    if not isinstance(rows, list):
        return [], 'bad reply'
    vessels = []
    for feat in rows:
        vessel = _from_openwaters(feat)
        if vessel is not None:
            vessels.append(vessel)
    return vessels, None


def ships_from_aishub(body):
    vessels, err = parse_aishub(body)
    if err:
        return [], err
    return [_as_ship(item) for item in vessels], None


def ship_from_aisstream(body):
    """one frame, or None. name_only frames stay name_only."""
    vessel = parse_aisstream(body)
    if not vessel:
        return None
    if vessel.get('name_only'):
        vessel['layer'] = 'ship'
        vessel['id'] = 'mmsi:' + str(vessel.get('mmsi') or '')
        return vessel
    return _as_ship(vessel)


def planes_from_adsb(body):
    data = _json(body)
    if not isinstance(data, dict):
        return [], 'bad reply'
    if data.get('error'):
        return [], _short(data.get('error'))
    rows = data.get('ac')
    if rows is None:
        rows = data.get('aircraft')
    if not isinstance(rows, list):
        return [], 'bad reply'
    planes = []
    seen = set()
    for row in rows:
        plane = _from_adsb(row)
        if plane is None or plane['id'] in seen:
            continue
        seen.add(plane['id'])
        planes.append(plane)
    return planes, None


def planes_from_opensky(body):
    data = _json(body)
    if not isinstance(data, dict):
        return [], 'bad reply'
    if data.get('error') or data.get('message'):
        message = data.get('error') or data.get('message')
        return [], _short(message)
    rows = data.get('states')
    if rows is None:
        return [], 'bad reply'
    if not isinstance(rows, list):
        return [], 'bad reply'
    planes = []
    for row in rows:
        plane = _from_opensky(row)
        if plane is not None:
            planes.append(plane)
    return planes, None


def _as_ship(vessel):
    vessel['layer'] = 'ship'
    vessel['id'] = 'mmsi:' + str(vessel.get('mmsi') or '')
    vessel['alt_ft'] = None
    vessel['on_ground'] = False
    vessel.setdefault('detail', vessel.get('kind') or 'other')
    if not vessel.get('id') or vessel['id'] == 'mmsi:':
        vessel['id'] = 'pos:%.5f,%.5f' % (vessel['lat'], vessel['lon'])
    name = str(vessel.get('name') or '').replace('@', ' ').strip()
    vessel['name'] = ' '.join(name.split())
    return vessel


def _from_openwaters(feat):
    if not isinstance(feat, dict):
        return None
    props = feat.get('properties') or {}
    if not isinstance(props, dict):
        return None
    if str(props.get('kind') or 'vessel') not in ('vessel', 'ship', ''):
        return None
    coords = (feat.get('geometry') or {}).get('coordinates') or []
    if len(coords) < 2:
        return None
    lon, lat = _num(coords[0]), _num(coords[1])
    if lat is None or lon is None:
        return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    length = _num(props.get('length'))
    beam = _num(props.get('beam'))
    if length is None or beam is None:
        span_l, span_b = ship_dims(
            props.get('to_bow'), props.get('to_stern'),
            props.get('to_port'), props.get('to_starboard'))
        length = length if length is not None else span_l
        beam = beam if beam is not None else span_b
    mmsi = props.get('mmsi') or feat.get('id') or ''
    name = str(props.get('name') or '').replace('@', ' ').strip()
    return {
        'id': 'mmsi:' + str(mmsi),
        'mmsi': str(mmsi),
        'layer': 'ship',
        'name': ' '.join(name.split()),
        'lat': lat,
        'lon': lon,
        'sog': _sog(props.get('sog')),
        'cog': _angle(props.get('cog')),
        'heading': _angle(props.get('heading')),
        'kind': ship_kind(props.get('type')),
        'length': length,
        'beam': beam,
        'alt_ft': None,
        'on_ground': False,
        'detail': ship_kind(props.get('type')),
        'name_only': False,
    }


def aircraft_mark(type_code, category):
    """Wingspan in meters, and whether the mark is a rotor.
    None span means the feed did not say. The scope scales the glyph from it."""
    code = ''.join(ch for ch in str(type_code or '').upper() if ch.isalnum())
    cat = str(category or '').strip().upper()
    rotor = cat == 'A7' or code in _ROTORS
    span = _SPANS.get(code)
    if span is None and len(code) >= 3:
        for prefix, family in _FAMILIES:
            if code.startswith(prefix):
                span = family
                break
    if span is not None:
        return span, rotor
    fallback = _CATEGORIES.get(cat)
    if fallback is None:
        return None, rotor
    return fallback[0], rotor or fallback[1]


_SPANS = {
    'C150': 10.2, 'C152': 10.2, 'C170': 11, 'C172': 11, 'C175': 11, 'C177': 10.8,
    'C180': 10.9, 'C182': 11, 'C185': 11, 'C206': 10.9, 'C210': 11.2,
    'P28A': 10.7, 'P28R': 10.8, 'PA18': 10.7, 'PA32': 11, 'PA34': 11.9, 'PA46': 13.1,
    'SR20': 11.7, 'SR22': 11.7, 'DA40': 11.9, 'DA42': 13.4, 'BE36': 10.2,
    'C208': 15.9, 'PC12': 16.3, 'TBM7': 12.7, 'TBM8': 12.7, 'TBM9': 12.7,
    'BE20': 16.6, 'BE9L': 15.3, 'B350': 17.7, 'C25A': 15.2, 'C25B': 16, 'C25C': 16.3,
    'C510': 11.1, 'C525': 14.3, 'C56X': 16.6, 'C560': 15.9, 'C680': 19.5, 'C68A': 21,
    'C750': 19.5, 'CL30': 19.5, 'CL35': 21, 'CL60': 19.6, 'GLF4': 23.7, 'GLF5': 28.5,
    'GLF6': 31.4, 'GLEX': 28.7, 'FA7X': 26.2, 'FA50': 18.9, 'E50P': 15.8,
    'E170': 26, 'E75S': 26, 'E75L': 28.7, 'E190': 28.7, 'E195': 28.7, 'E295': 35.1,
    'CRJ2': 21.2, 'CRJ7': 23.2, 'CRJ9': 24.9, 'CRJX': 26.2,
    'DH8A': 25.9, 'DH8D': 28.4, 'AT72': 27.1, 'AT76': 27.1, 'SF34': 21.4,
    'A306': 44.8, 'A310': 43.9, 'A318': 34.1, 'A319': 34.1, 'A320': 34.1, 'A321': 34.1,
    'A19N': 35.8, 'A20N': 35.8, 'A21N': 35.8, 'A332': 60.3, 'A333': 60.3,
    'A338': 64, 'A339': 64, 'A359': 64.8, 'A35K': 64.8, 'A388': 79.8,
    'B712': 28.4, 'B738': 35.8, 'B739': 35.8, 'B38M': 35.9, 'B39M': 35.9, 'B37M': 35.9,
    'B752': 38.1, 'B753': 38.1, 'B763': 47.6, 'B764': 51.9, 'B772': 60.9, 'B77W': 64.8,
    'B788': 60.1, 'B789': 60.1, 'B744': 64.4, 'B748': 68.4, 'MD11': 51.7,
    'BCS1': 35.1, 'BCS3': 35.1,
    'R22': 7.7, 'R44': 10.1, 'R66': 10.1, 'B06': 10.2, 'B407': 10.7, 'EC35': 10.7,
    'A139': 13.8, 'S92': 17.2, 'H60': 16.4,
}
_ROTORS = {
    'R22', 'R44', 'R66', 'B06', 'B407', 'B412', 'B429', 'EC20', 'EC30', 'EC35',
    'EC45', 'EC55', 'EC75', 'A109', 'A139', 'AS50', 'AS65', 'S76', 'S92', 'H60',
    'H47', 'GAZL',
}
_CATEGORIES = {
    'A1': (11, False), 'A2': (20, False), 'A3': (36, False), 'A4': (38, False),
    'A5': (64, False), 'A6': (12, False), 'A7': (12, True),
    'B1': (15, False), 'B2': (20, False), 'B4': (8, False),
}
_FAMILIES = (
    ('A388', 79.8), ('A38', 79.8), ('A35', 64.8), ('A34', 60.3), ('A33', 60.3),
    ('A318', 34.1), ('A319', 34.1), ('A320', 34.1), ('A321', 34.1),
    ('A19', 35.8), ('A20', 35.8), ('A21', 35.8),
    ('B748', 68.4), ('B74', 64), ('B78', 60.1), ('B77', 61), ('B76', 47.6),
    ('B75', 38.1), ('B73', 35.8), ('B37', 35.9), ('B38', 35.9), ('B39', 35.9),
    ('B3X', 35.9), ('E29', 35.1), ('E19', 28.7), ('E17', 26), ('E75', 26),
    ('CRJ', 23), ('DH8', 27), ('AT7', 27.1), ('AT4', 24.6),
    ('C17', 11), ('C15', 10.2), ('C18', 11), ('P28', 10.8),
)


def _from_adsb(row):
    if not isinstance(row, dict):
        return None
    lat, lon = _num(row.get('lat')), _num(row.get('lon'))
    if lat is None or lon is None:
        last = row.get('lastPosition') if isinstance(row.get('lastPosition'), dict) else None
        if last:
            lat, lon = _num(last.get('lat')), _num(last.get('lon'))
    if lat is None or lon is None:
        return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    alt = row.get('alt_baro')
    on_ground = alt == 'ground' or row.get('alt_baro') == 'ground'
    alt_ft = None
    if isinstance(alt, (int, float)) and not isinstance(alt, bool):
        if not math.isnan(float(alt)):
            alt_ft = float(alt)
    elif alt is None:
        geom = _num(row.get('alt_geom'))
        if geom is not None:
            alt_ft = geom
    if on_ground:
        alt_ft = 0.0
    flight = str(row.get('flight') or row.get('r') or '').strip()
    hexid = str(row.get('hex') or '').strip().lower()
    track = _angle(row.get('track'))
    if track is None:
        track = _angle(row.get('true_heading'))
    heading = _angle(row.get('true_heading') if row.get('true_heading') is not None else row.get('track'))
    detail = str(row.get('t') or row.get('category') or '').strip()
    span, rotor = aircraft_mark(row.get('t'), row.get('category'))
    return {
        'id': 'hex:' + (hexid or flight or '%.5f,%.5f' % (lat, lon)),
        'layer': 'plane',
        'name': flight,
        'lat': lat,
        'lon': lon,
        'sog': _speed_kt(row.get('gs')),
        'cog': track,
        'heading': heading,
        'kind': 'aircraft',
        'length': None,
        'beam': None,
        'span': span,
        'rotor': rotor,
        'alt_ft': alt_ft,
        'on_ground': bool(on_ground),
        'detail': detail,
        'name_only': False,
    }


def _from_opensky(row):
    if not isinstance(row, (list, tuple)) or len(row) < 11:
        return None
    lon, lat = _num(row[5]), _num(row[6])
    if lat is None or lon is None:
        return None
    on_ground = bool(row[8])
    alt_m = _num(row[7])
    alt_ft = None if alt_m is None else alt_m * M_TO_FT
    if on_ground:
        alt_ft = 0.0
    velocity = _num(row[9])
    sog = None if velocity is None else velocity * M_PER_S_TO_KT
    track = _angle(row[10])
    callsign = str(row[1] or '').strip()
    icao = str(row[0] or '').strip().lower()
    return {
        'id': 'hex:' + (icao or callsign or '%.5f,%.5f' % (lat, lon)),
        'layer': 'plane',
        'name': callsign,
        'lat': lat,
        'lon': lon,
        'sog': sog,
        'cog': track,
        'heading': track,
        'kind': 'aircraft',
        'length': None,
        'beam': None,
        'span': None,
        'rotor': False,
        'alt_ft': alt_ft,
        'on_ground': on_ground,
        'detail': str(row[2] or '').strip(),
        'name_only': False,
    }


def _speed_kt(value):
    number = _num(value)
    if number is None or number < 0 or number > 800:
        return None
    return number


def _json(body):
    if isinstance(body, (bytes, bytearray)):
        body = body.decode('utf-8', 'replace')
    return json.loads(body) if isinstance(body, str) else body

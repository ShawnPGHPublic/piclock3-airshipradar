"""Radar geometry and AIS parsing. No Qt, so it tests alone.

Derived from piclock3-ais-radar (MIT). The scope is the full disk.
"""
import json
import math
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

M_PER_DEG_LAT = 111320.0
MI_TO_M = 1609.344
KM_TO_M = 1000.0
NM_TO_M = 1852.0

POSITION_TYPES = (
    'PositionReport',
    'StandardClassBPositionReport',
    'ExtendedClassBPositionReport',
)

# AIS "not available" sentinels that survive into the human-readable feed.
SOG_NOT_AVAILABLE = 102.0
COG_NOT_AVAILABLE = 360.0

_BEAM_RATIO = {
    'tanker': 0.16,
    'cargo': 0.16,
    'passenger': 0.18,
    'fishing': 0.28,
    'tug': 0.36,
    'pleasure': 0.32,
    'fast': 0.18,
}


def ship_kind(type_code):
    """ITU-R M.1371 type code, grouped so a scope can still be read."""
    try:
        code = int(float(type_code))
    except (TypeError, ValueError):
        return 'other'
    if code <= 0 or code >= 100:
        return 'other'
    if code == 30:
        return 'fishing'
    if code in (31, 32, 52):
        return 'tug'
    if code in (36, 37):
        return 'pleasure'
    if 40 <= code <= 49:
        return 'fast'
    if 60 <= code <= 69:
        return 'passenger'
    if 70 <= code <= 79:
        return 'cargo'
    if 80 <= code <= 89:
        return 'tanker'
    return 'other'


def ship_dims(a, b, c, d):
    """(length, beam) from AIS A/B and C/D. Missing or zero is None."""
    return _span(a, b, 2, 800), _span(c, d, 1, 150)


def mark_pixels(length, beam, kind, radius_px, radius_m):
    """(len_px, beam_px). No length means a type symbol, not a measurement."""
    ratio = _BEAM_RATIO.get(kind or 'other', 0.16)
    if length is None or length <= 0 or radius_m <= 0:
        len_px = 4.0 if kind in (None, '', 'other') else 11.0
        return len_px, max(2.2, len_px * (1.0 if kind in (None, '', 'other') else ratio))
    if beam and beam > 0:
        ratio = float(beam) / float(length)
    true_len = (float(length) / float(radius_m)) * float(radius_px)
    class_len = 7.0 + math.log(max(float(length), 8.0) / 8.0, 2) * 6.2
    len_px = min(max(true_len, class_len), float(radius_px) * 0.22)
    beam_px = min(max(len_px * ratio, 2.2), len_px * 0.55)
    return len_px, beam_px


def radius_meters(radius, unit):
    """miles, kilometers, or nautical miles, as a radius in meters."""
    unit = normalize_unit(unit)
    if unit == 'km':
        scale = KM_TO_M
    elif unit == 'nm':
        scale = NM_TO_M
    else:
        scale = MI_TO_M
    meters = float(radius) * scale
    if meters <= 0:
        raise ValueError('radius must be greater than zero')
    return meters


def normalize_unit(unit):
    name = str(unit or 'mi').strip().lower()
    if name in ('km', 'kilometer', 'kilometers', 'kilometre', 'kilometres'):
        return 'km'
    if name in ('nm', 'nmi', 'nautical', 'nauticalmile', 'nauticalmiles'):
        return 'nm'
    return 'mi'


def format_radius(radius, unit):
    """`18 mi`, `12.5 km`, or `10 nm`, the way the caption prints it."""
    unit = normalize_unit(unit)
    n = float(radius)
    text = '%d' % round(n) if abs(n - round(n)) < 0.05 else '%.1f' % n
    return '%s %s' % (text, unit)


def bounding_box(lat, lon, radius_m):
    """the full circle around the station, as a lat/lon box."""
    dlat = radius_m / M_PER_DEG_LAT
    cos = math.cos(math.radians(lat))
    dlon = radius_m / (M_PER_DEG_LAT * max(abs(cos), 0.01))
    return {
        'latmin': max(-90.0, lat - dlat),
        'latmax': min(90.0, lat + dlat),
        'lonmin': lon - dlon,
        'lonmax': lon + dlon,
    }


def project(lat, lon, center_lat, center_lon, bearing_deg, radius_m):
    """(right, forward) in radii.

    forward > 0 is toward the top of the scope. right > 0 is to the right.
    bearing_deg is the true bearing that sits at the top. 0 is north-up.
    None when the radius is unusable.
    """
    if radius_m <= 0:
        return None
    north = (lat - center_lat) * M_PER_DEG_LAT
    east = ((lon - center_lon) * M_PER_DEG_LAT
            * math.cos(math.radians(center_lat)))
    bearing = math.radians(bearing_deg)
    forward = north * math.cos(bearing) + east * math.sin(bearing)
    right = -north * math.sin(bearing) + east * math.cos(bearing)
    return (right / radius_m, forward / radius_m)


def in_scope(point):
    """inside the full disk, including the rim. Every bearing is kept."""
    if point is None:
        return False
    right, forward = point
    return right * right + forward * forward <= 1.000001


def parse_aishub(body):
    """(vessels, error). error is a short caption, or None on a real reply.

    An empty traffic list is a success: the scope is clear, not broken.
    """
    if isinstance(body, (bytes, bytearray)):
        body = body.decode('utf-8', 'replace')
    data = json.loads(body) if isinstance(body, str) else body
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return [], 'bad reply'
    head = data[0]
    if _is_error(head.get('ERROR')):
        message = head.get('ERROR_MESSAGE') or head.get('REASON') or 'rejected'
        return [], _short(message)
    rows = data[1] if len(data) > 1 and isinstance(data[1], list) else []
    vessels = []
    for row in rows:
        vessel = _from_aishub_row(row)
        if vessel is not None:
            vessels.append(vessel)
    return vessels, None


def stream_note(body):
    """'live' for a subscription ack, 'error:…' for a refusal, else None.

    AISStream puts traffic and these control texts on the same socket.
    """
    if isinstance(body, (bytes, bytearray)):
        body = body.decode('utf-8', 'replace')
    try:
        msg = json.loads(body) if isinstance(body, str) else body
    except (TypeError, ValueError):
        return None
    if not isinstance(msg, dict):
        return None
    kind = str(msg.get('MessageType') or msg.get('messageType') or '')
    if kind == 'SubscriptionConfirmation':
        return 'live'
    err = msg.get('error') or msg.get('Error') or msg.get('ERROR')
    if err:
        return 'error:' + _short(err)
    if kind.lower() == 'error':
        message = msg.get('Message') or msg.get('Reason') or 'rejected'
        if isinstance(message, dict):
            message = message.get('error') or message.get('Error') or 'rejected'
        return 'error:' + _short(message)
    return None


def parse_aisstream(body):
    """one websocket JSON frame, or None when it has nothing to plot.

    A static-data frame with a name and no position comes back with
    name_only set, so a position already on the scope can pick the name up.
    """
    if isinstance(body, (bytes, bytearray)):
        body = body.decode('utf-8', 'replace')
    msg = json.loads(body) if isinstance(body, str) else body
    if not isinstance(msg, dict):
        return None
    meta = msg.get('MetaData') or msg.get('Metadata') or {}
    if not isinstance(meta, dict):
        meta = {}
    inner = msg.get('Message') or {}
    if not isinstance(inner, dict):
        inner = {}
    report = None
    for key in POSITION_TYPES:
        if isinstance(inner.get(key), dict):
            report = inner[key]
            break
    lat = _first_num(meta, 'Latitude', 'latitude')
    lon = _first_num(meta, 'Longitude', 'longitude')
    if report is not None:
        if lat is None:
            lat = _first_num(report, 'Latitude', 'latitude')
        if lon is None:
            lon = _first_num(report, 'Longitude', 'longitude')
    mmsi = _first(meta, 'MMSI', 'mmsi', 'UserID')
    if mmsi is None and report is not None:
        mmsi = report.get('UserID')
    name = _first(meta, 'ShipName', 'shipname', 'shipName') or ''
    name = str(name).strip()
    kind, length, beam, static_name = _identity(inner, report)
    if not name:
        name = static_name
    if lat is None or lon is None:
        if mmsi and (name or kind != 'other' or length is not None):
            return {
                'mmsi': str(mmsi),
                'name': name,
                'kind': kind,
                'length': length,
                'beam': beam,
                'name_only': True,
            }
        return None
    sog = cog = heading = None
    if report is not None:
        sog = _sog(report.get('Sog'))
        cog = _angle(report.get('Cog'))
        heading = _angle(report.get('TrueHeading'))
    return {
        'mmsi': str(mmsi or ''),
        'name': name,
        'lat': lat,
        'lon': lon,
        'sog': sog,
        'cog': cog,
        'heading': heading,
        'kind': kind,
        'length': length,
        'beam': beam,
        'name_only': False,
    }


def remember_identity(cache, vessel):
    """Keep type and size by MMSI, and copy them onto a later position.

    A position report usually has neither. The static report often arrives
    first, or on its own, and has to still be there when the position shows up.
    """
    mmsi = str(vessel.get('mmsi') or '')
    if not mmsi:
        return vessel
    slot = cache.setdefault(mmsi, {})
    if vessel.get('kind') not in (None, '', 'other'):
        slot['kind'] = vessel['kind']
    if vessel.get('length') is not None:
        slot['length'] = vessel['length']
    if vessel.get('beam') is not None:
        slot['beam'] = vessel['beam']
    if vessel.get('name'):
        slot['name'] = vessel['name']
    if vessel.get('kind') in (None, '', 'other') and slot.get('kind'):
        vessel['kind'] = slot['kind']
    if vessel.get('length') is None and slot.get('length') is not None:
        vessel['length'] = slot['length']
    if vessel.get('beam') is None and slot.get('beam') is not None:
        vessel['beam'] = slot['beam']
    if not vessel.get('name') and slot.get('name'):
        vessel['name'] = slot['name']
    return vessel


def _from_aishub_row(row):
    if not isinstance(row, dict):
        return None
    lat = _num(row.get('LATITUDE'))
    lon = _num(row.get('LONGITUDE'))
    if lat is None or lon is None:
        return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    length, beam = ship_dims(row.get('A'), row.get('B'), row.get('C'), row.get('D'))
    return {
        'mmsi': str(row.get('MMSI') or ''),
        'name': str(row.get('NAME') or '').strip(),
        'lat': lat,
        'lon': lon,
        'sog': _sog(row.get('SOG')),
        'cog': _angle(row.get('COG')),
        'heading': _angle(row.get('HEADING')),
        'kind': ship_kind(row.get('TYPE')),
        'length': length,
        'beam': beam,
        'name_only': False,
    }


def _identity(inner, report):
    static = None
    if isinstance(inner, dict):
        for key in ('ShipStaticData', 'StaticDataReport'):
            if isinstance(inner.get(key), dict):
                static = inner[key]
                break
    report_b = {}
    if isinstance(static, dict) and isinstance(static.get('ReportB'), dict):
        report_b = static['ReportB']
    src = {}
    if isinstance(static, dict):
        src.update(static)
    src.update(report_b)
    if isinstance(report, dict):
        for key in ('Type', 'ShipType', 'Dimension', 'A', 'B', 'C', 'D'):
            if key not in src and key in report:
                src[key] = report[key]
    name = str(_first(src, 'Name', 'ShipName') or '').strip()
    kind = ship_kind(_first(src, 'Type', 'ShipType'))
    dim = src.get('Dimension') if isinstance(src.get('Dimension'), dict) else src
    length, beam = ship_dims(dim.get('A'), dim.get('B'), dim.get('C'), dim.get('D'))
    return kind, length, beam, name


def _span(a, b, low, high):
    na, nb = _num(a), _num(b)
    if na is None and nb is None:
        return None
    total = (na or 0) + (nb or 0)
    if total < low or total > high:
        return None
    return total


def _is_error(flag):
    if flag in (False, None, 0, 'false', 'FALSE', 'False', ''):
        return False
    return True


def _short(message):
    text = ' '.join(str(message).split())
    return text[:42] or 'rejected'


def _num(value):
    try:
        if value is None or value == '':
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _first(src, *keys):
    for key in keys:
        if key in src and src[key] not in (None, ''):
            return src[key]
    return None


def _first_num(src, *keys):
    for key in keys:
        number = _num(src.get(key))
        if number is not None:
            return number
    return None


def _sog(value):
    number = _num(value)
    if number is None or number < 0 or number >= SOG_NOT_AVAILABLE:
        return None
    return number


def _angle(value):
    number = _num(value)
    if number is None or number < 0 or number >= COG_NOT_AVAILABLE:
        return None
    return number % 360.0


# AIS-catcher ShippingClass, the order of the sprites in its web viewer.
# Planes, base stations, and AtoN are tracked too; they are not hulls.
_CLASS_NUM = {
    2: 'cargo',
    3: 'pleasure',
    4: 'passenger',
    6: 'tanker',
    7: 'fast',
    8: 'fishing',
}
_SKIP_CLASS_NUM = (9, 10, 11, 12, 13)
_SKIP_CLASS_NAME = {
    'plane', 'aircraft', 'helicopter', 'heli',
    'station', 'base_station', 'aton', 'aton_virtual',
    'sart', 'sartepirb', 'epirb',
}
_CLASS_NAME = {
    'cargo': 'cargo',
    'tanker': 'tanker',
    'passenger': 'passenger',
    'fishing': 'fishing',
    'high_speed': 'fast',
    'highspeed': 'fast',
    'class_b': 'pleasure',
    'pleasure': 'pleasure',
    'tug': 'tug',
    'fast': 'fast',
}
_LIST_KEYS = ('ships', 'vessels', 'data', 'msgs')


def catcher_request(url, box=None, max_age_s=None):
    """(url, error). error is a short caption, or None.

    A host and port, or a URL with no path, is the web viewer:
    http://that-host/api/ships.json. The /api/v1/vessels list is asked
    only for the box around the station.
    """
    text = str(url or '').strip()
    if not text:
        return '', 'no station'
    if '://' not in text:
        text = 'http://' + text
    parts = urlsplit(text)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        return '', 'bad station'
    path = parts.path or ''
    query = parse_qsl(parts.query, keep_blank_values=True)
    have = set(key for key, _value in query)
    if path in ('', '/'):
        path = '/api/ships.json'
    if path.rstrip('/') == '/api/v1/vessels':
        if 'limit' not in have:
            query.append(('limit', '1000'))
        if box and 'bbox' not in have:
            query.append(('bbox', '%.5f,%.5f,%.5f,%.5f' % (
                box['lonmin'], box['latmin'], box['lonmax'], box['latmax'])))
        if max_age_s and 'max_age_s' not in have:
            try:
                query.append(('max_age_s', '%d' % int(max_age_s)))
            except (TypeError, ValueError):
                pass
    return urlunsplit((parts.scheme, parts.netloc, path,
                       urlencode(query), '')), None


def parse_aiscatcher(body):
    """(vessels, error). error is a short caption, or None on a real reply.

    An empty ship list is a success: the scope is clear, not broken.
    Reads the web viewer's /api/ships.json, GeoJSON from /geojson, and
    the /api/v1/vessels list. Planes, base stations, and aids to navigation
    are left out; this layer is ships.
    """
    if isinstance(body, (bytes, bytearray)):
        body = body.decode('utf-8', 'replace')
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except (TypeError, ValueError):
        return [], 'bad reply'
    rows, err = _catcher_rows(data)
    if err:
        return [], err
    vessels = []
    for row in rows:
        vessel = _from_catcher(row)
        if vessel is not None:
            vessels.append(vessel)
    return vessels, None


def _catcher_rows(data):
    if isinstance(data, list):
        return data, None
    if not isinstance(data, dict):
        return [], 'bad reply'
    err = _catcher_error(data)
    if err:
        return [], err
    kind = str(data.get('type') or '')
    if kind == 'FeatureCollection' and isinstance(data.get('features'), list):
        return data['features'], None
    if kind == 'Feature':
        return [data], None
    for key in _LIST_KEYS:
        rows = data.get(key)
        if isinstance(rows, list):
            return rows, None
    if _first(data, 'mmsi', 'MMSI') is not None or _first(data, 'lat', 'latitude') is not None:
        return [data], None
    if data.get('error') is False or 'count' in data:
        return [], None
    return [], 'bad reply'


def _catcher_error(data):
    flag = data.get('error')
    if flag in (False, None, 0, 'false', 'FALSE', 'False', ''):
        return None
    if flag is True:
        message = data.get('error_message') or data.get('detail') or data.get('title') or 'rejected'
        return _short(message)
    if isinstance(flag, str):
        return _short(flag)
    return None


def _from_catcher(row):
    if not isinstance(row, dict):
        return None
    if str(row.get('type') or '') == 'Feature':
        props = row.get('properties') if isinstance(row.get('properties'), dict) else {}
        merged = dict(props)
        geom = row.get('geometry') if isinstance(row.get('geometry'), dict) else {}
        coords = geom.get('coordinates')
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            merged.setdefault('lon', coords[0])
            merged.setdefault('lat', coords[1])
        row = merged
    return _from_catcher_row(row)


def _from_catcher_row(row):
    lat = _first_num(row, 'lat', 'latitude', 'LATITUDE')
    lon = _first_num(row, 'lon', 'longitude', 'LONGITUDE')
    if lat is None or lon is None:
        return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    if _catcher_nonship(row):
        return None
    length, beam = ship_dims(
        _first(row, 'to_bow', 'to_bow_m', 'A'),
        _first(row, 'to_stern', 'to_stern_m', 'B'),
        _first(row, 'to_port', 'to_port_m', 'C'),
        _first(row, 'to_starboard', 'to_starboard_m', 'D'))
    if length is None:
        length = _span_one(_first(row, 'length', 'length_m'))
    if beam is None:
        beam = _span_one(_first(row, 'beam', 'beam_m', 'width'))
    age = _num(row.get('age_s'))
    if age is None:
        heard = _num(row.get('last_signal'))
        # ships.json sends seconds since the message, not a unix time.
        if heard is not None and 0 <= heard < 1000000000:
            age = heard
    vessel = {
        'mmsi': str(_first(row, 'mmsi', 'MMSI') or ''),
        'name': str(_first(row, 'shipname', 'name', 'NAME') or '').strip(),
        'lat': lat,
        'lon': lon,
        'sog': _sog(_first(row, 'speed', 'speed_kn', 'sog', 'SOG')),
        'cog': _angle(_first(row, 'cog', 'course', 'course_deg', 'COG')),
        'heading': _angle(_first(row, 'heading', 'heading_deg', 'HEADING')),
        'kind': _catcher_kind(row),
        'length': length,
        'beam': beam,
        'name_only': False,
    }
    if age is not None and age >= 0:
        vessel['age'] = age
    return vessel


def _span_one(value):
    number = _num(value)
    if number is None or number < 2 or number > 800:
        return None
    return number


def _catcher_nonship(row):
    """Planes, helicopters, base stations, and AtoN are not ship hulls.

    A real ITU type still counts, even if the class code is odd.
    """
    if ship_kind(_first(row, 'shiptype', 'ship_type', 'TYPE')) != 'other':
        return False
    clas = row.get('class')
    if isinstance(clas, str) and clas.strip().lower().replace('-', '_') in _SKIP_CLASS_NAME:
        return True
    number = _num(row.get('shipclass'))
    return number is not None and int(number) in _SKIP_CLASS_NUM


def _catcher_kind(row):
    kind = ship_kind(_first(row, 'shiptype', 'ship_type', 'TYPE'))
    if kind != 'other':
        return kind
    clas = row.get('class')
    if isinstance(clas, str):
        mapped = _CLASS_NAME.get(clas.strip().lower())
        if mapped:
            return mapped
    number = _num(row.get('shipclass'))
    if number is None:
        return 'other'
    return _CLASS_NUM.get(int(number), 'other')

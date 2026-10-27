"""Geometry and feed parsing. No Qt, no network."""
import json
from urllib.parse import parse_qsl, urlsplit

from feeds import (
    aircraft_mark,
    catcher_url,
    planes_from_adsb,
    planes_from_opensky,
    ships_from_aiscatcher,
    ships_from_openwaters,
)
from radarmath import catcher_request, in_scope, project


def test_full_circle_keeps_the_back_half():
    south = project(40.0, -74.0, 40.2, -74.0, 0, 50000)
    assert south is not None
    assert south[1] < 0
    assert in_scope(south)
    assert in_scope((0.2, -0.2))
    assert not in_scope((0.9, 0.9))
    assert in_scope((0.0, 1.0))
    assert not in_scope((0.0, 1.02))


def test_north_is_up():
    point = project(40.7, -74.0, 40.6, -74.0, 0, 50000)
    assert point[1] > 0
    assert abs(point[0]) < 0.05


def test_openwaters_ship():
    body = json.dumps({
        'features': [{
            'geometry': {'type': 'Point', 'coordinates': [-74.01, 40.66]},
            'properties': {
                'kind': 'vessel', 'mmsi': 367759510, 'name': 'CIRCLE LINE',
                'sog': 6.2, 'cog': 90, 'type': 60, 'length': 47, 'beam': 10,
            },
        }],
    })
    rows, err = ships_from_openwaters(body)
    assert err is None
    assert rows[0]['kind'] == 'passenger'
    assert rows[0]['layer'] == 'ship'
    assert rows[0]['sog'] == 6.2


def test_adsb_plane():
    body = json.dumps({'ac': [{
        'hex': 'a4ef3c', 'flight': 'N417MH  ', 't': 'C25A',
        'lat': 40.75, 'lon': -74.1, 'alt_baro': 10475,
        'gs': 206.6, 'track': 262.7,
    }]})
    rows, err = planes_from_adsb(body)
    assert err is None and len(rows) == 1
    assert rows[0]['name'] == 'N417MH'
    assert rows[0]['alt_ft'] == 10475
    assert rows[0]['on_ground'] is False
    assert 14 < rows[0]['span'] < 17
    assert rows[0]['rotor'] is False


def test_aircraft_sizes():
    light, light_rotor = aircraft_mark('C172', 'A1')
    jet, jet_rotor = aircraft_mark('B738', 'A3')
    heavy, heavy_rotor = aircraft_mark('A388', 'A5')
    unknown, heli = aircraft_mark('', 'A7')
    bare, bare_rotor = aircraft_mark('ZZZZ', '')
    assert light < 14 < jet < 40 < heavy
    assert heavy > jet > light * 2
    assert light_rotor is jet_rotor is heavy_rotor is False
    assert heli is True and unknown == 12
    assert bare is None and bare_rotor is False


def test_opensky_plane():
    body = json.dumps({'states': [[
        'a34215', 'EDV5455 ', 'United States', 1, 1,
        -74.18, 40.85, 4351.02, False, 164.72, 271.97,
    ]]})
    rows, err = planes_from_opensky(body)
    assert err is None and rows[0]['name'] == 'EDV5455'
    assert rows[0]['alt_ft'] > 14000
    assert 300 < rows[0]['sog'] < 340


def test_aiscatcher_ships():
    body = json.dumps({
        'count': 2,
        'error': False,
        'ships': [
            {
                'mmsi': 367000001, 'lat': 40.66, 'lon': -74.04,
                'speed': 12.4, 'cog': 150, 'heading': 148,
                'to_bow': 90, 'to_stern': 30, 'to_port': 8, 'to_starboard': 10,
                'shiptype': 80, 'shipname': 'HARBOR', 'last_signal': 7,
            },
            {
                'mmsi': 999, 'lat': 40.7, 'lon': -74.1,
                'shiptype': 0, 'shipclass': 9, 'shipname': 'N1',
            },
        ],
    })
    rows, err = ships_from_aiscatcher(body)
    assert err is None and len(rows) == 1
    ship = rows[0]
    assert ship['layer'] == 'ship' and ship['id'] == 'mmsi:367000001'
    assert ship['kind'] == 'tanker' and ship['name'] == 'HARBOR'
    assert ship['sog'] == 12.4 and ship['cog'] == 150 and ship['heading'] == 148
    assert ship['length'] == 120 and ship['beam'] == 18 and ship['age'] == 7
    empty, err = ships_from_aiscatcher('{"count": 0, "error": false, "ships": []}')
    assert empty == [] and err is None
    rows, err = ships_from_aiscatcher('{"error": true, "error_message": "nope"}')
    assert rows == [] and err == 'nope'
    rows, err = ships_from_aiscatcher('{')
    assert rows == [] and err == 'bad reply'


def test_aiscatcher_v1_and_geojson():
    body = json.dumps({'data': [{
        'mmsi': 244000001, 'name': 'FAST', 'lat': 40.7, 'lon': -74.0,
        'speed_kn': 18.5, 'course_deg': 90, 'heading_deg': 511,
        'ship_type': 40, 'to_bow_m': 40, 'to_stern_m': 10,
        'to_port_m': 5, 'to_starboard_m': 6, 'age_s': 4,
        'last_signal': 1700000000,
    }]})
    rows, err = ships_from_aiscatcher(body)
    assert err is None and rows[0]['kind'] == 'fast'
    assert rows[0]['heading'] is None and rows[0]['age'] == 4
    assert rows[0]['length'] == 50 and rows[0]['beam'] == 11
    body = json.dumps({
        'type': 'Feature',
        'geometry': {'type': 'Point', 'coordinates': [-74.02, 40.68]},
        'properties': {
            'mmsi': 1, 'shipname': 'DOT', 'speed': 102.3, 'cog': 360,
            'shiptype': 0, 'shipclass': 6,
        },
    })
    rows, err = ships_from_aiscatcher(body)
    assert err is None and rows[0]['kind'] == 'tanker'
    assert rows[0]['lat'] == 40.68 and rows[0]['lon'] == -74.02
    assert rows[0]['sog'] is None and rows[0]['cog'] is None
    assert 'age' not in rows[0]
    body = json.dumps({'ships': [{
        'mmsi': 2, 'lat': 1, 'lon': 2, 'class': 'class_b', 'last_signal': 1700000000,
    }]})
    rows, err = ships_from_aiscatcher(body)
    assert err is None and rows[0]['kind'] == 'pleasure' and 'age' not in rows[0]
    rows, err = ships_from_aiscatcher(json.dumps({
        'ships': [{'mmsi': 3, 'lat': 91, 'lon': 0}],
    }))
    assert rows == [] and err is None


def test_catcher_url():
    url, err = catcher_url('192.168.1.50:8100', 40.66, -74.05, 40000, 10)
    assert err is None
    assert url == 'http://192.168.1.50:8100/api/ships.json'
    url, err = catcher_request('http://192.168.1.50:8100/api/ships.json', {
        'latmin': 40, 'latmax': 41, 'lonmin': -75, 'lonmax': -73,
    }, 600)
    assert err is None and url == 'http://192.168.1.50:8100/api/ships.json'
    url, err = catcher_url('http://192.168.1.50:8111/api/v1/vessels', 40.66, -74.05, 20000, 10)
    assert err is None
    query = dict(parse_qsl(urlsplit(url).query))
    assert query['limit'] == '1000' and query['max_age_s'] == '600'
    assert query['bbox'].count(',') == 3
    url, err = catcher_request(
        'http://h/api/v1/vessels?limit=10&bbox=0,0,1,1&max_age_s=30',
        {'latmin': 1, 'latmax': 2, 'lonmin': 3, 'lonmax': 4}, 600)
    assert err is None
    assert parse_qsl(urlsplit(url).query) == [
        ('limit', '10'), ('bbox', '0,0,1,1'), ('max_age_s', '30')]
    assert catcher_request('', None, None) == ('', 'no station')
    assert catcher_request('ftp://radio/ships', None, None)[1] == 'bad station'


if __name__ == '__main__':
    test_full_circle_keeps_the_back_half()
    test_north_is_up()
    test_openwaters_ship()
    test_adsb_plane()
    test_aircraft_sizes()
    test_opensky_plane()
    test_aiscatcher_ships()
    test_aiscatcher_v1_and_geojson()
    test_catcher_url()
    print('ok')


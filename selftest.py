"""Geometry and feed parsing. No Qt, no network."""
import json

from feeds import (
    aircraft_mark,
    planes_from_adsb,
    planes_from_opensky,
    ships_from_openwaters,
)
from radarmath import in_scope, project


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


if __name__ == '__main__':
    test_full_circle_keeps_the_back_half()
    test_north_is_up()
    test_openwaters_ship()
    test_adsb_plane()
    test_aircraft_sizes()
    test_opensky_plane()
    print('ok')

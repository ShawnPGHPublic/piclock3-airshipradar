# Air Ship Radar

A [PiClock3](https://github.com/n0bel/PiClock3) widget. A simulated radar of
ships and aircraft around a latitude and longitude, plotted on one scope.

<img width="367" height="336" alt="image" src="https://github.com/user-attachments/assets/b5646fca-c1bb-40d5-b218-c779b0415a2d" />

You set the center. The glass is the whole 360 degrees out to the radius
you set, in miles, kilometers, or nautical miles. `bearing` only says which
way is up. `0` is north. Targets inside the circle are a plan-position
display: range rings, a sweep that goes all the way around, the ship, and
the aircraft. Ships are drawn from the AIS type and the hull dimensions,
the same way a shoreline scope draws them. Aircraft are a separate mark,
amber, sized from the type code so a light plane is smaller than a jet
and a heavy is larger still. A helicopter is a rotor, not a wing. A target
on the ground is a hollow dot, still sized from the type. Under the scope,
in small text, is the radius, the two counts, and the time a feed last answered.

```
25 mi  ·  140 ships  ·  86 aircraft  ·  14:02:11
```

The widget fills the layout region it is given. `font-size` is a fraction
of that region's height. To make the scope bigger on the page, change the
region in `layouts/circle.yaml`. Do not look for a pixel width.

This plugin is derived from
[piclock3-ais-radar](https://github.com/ShawnPGHPublic/piclock3-ais-radar).
The half-disk became the full disk, and a flight feed was added beside AIS.
The AISHub parser, the AISStream socket, and the hull marks come from there,
under the same MIT license.

## Feeds

Ships and aircraft are separate settings. Both can be on. Either can be
`off`. The defaults need no key.

| Setting | Value | What it is | Key |
|---|---|---|---|
| `ships` | `openwaters` | Polls the [Open Waters](https://openwaters.io/ais/) AIS snapshot | none |
| `ships` | `aishub` | Polls [AISHub](https://www.aishub.net/api), at most once a minute | `username` |
| `ships` | `aisstream` | Holds [AISStream](https://aisstream.io/documentation) open while the page is showing | `apikey` |
| `ships` | `aiscatcher` | Polls an [AIS-catcher](https://github.com/jvde-github/AIS-catcher) on this LAN | none for the web viewer. `token` only if `aiscatcher-url` is an `/api/v1` service that requires a key |
| `flights` | `adsb` | Polls [adsb.lol](https://api.adsb.lol/v2/lat/0/lon/0/dist/25), a free ADS-B feed | none |
| `flights` | `opensky` | Polls the [OpenSky Network](https://openskynetwork.github.io/opensky-api/rest.html) state vectors | none |

Open Waters anonymous reads stay inside about a hundred square degrees.
A radius that would ask for more is not requested; the caption says
`radius too wide`. adsb.lol is asked for the circle in nautical miles,
capped at 250. OpenSky is asked for the bounding box of the circle.

AISHub and AISStream keys do not belong in `config.yaml`. Put them in
`ApiKeys.yaml` beside `PyQtPiClock3.py`:

```yaml
aishub: YOUR_USERNAME
aisstream: YOUR_KEY
```

And, once, in the clock config:

```yaml
apikeys: !include ApiKeys.yaml
```

The defaults are already `{apikeys.aishub}` and `{apikeys.aisstream}`.
With no key, an AISHub or AISStream scope still draws and the caption
says `no key`. It does not invent ships. `openwaters` and `adsb` do not
look at those keys.

## AIS-catcher on the same network

`ships: aiscatcher` reads a station you run. Aircraft stay on `flights`
(`adsb` or `opensky`) unless you turn them `off`. This is your receiver,
not the aiscatcher.org sharing site.

On the machine with the radio, managed mode (`AIS-catcher -E ... 0.0.0.0:8118`)
serves the control panel on 8118 and the web viewer on the next port, 8119.
The viewer is the page with the map. The control panel is not this list.
A standalone `AIS-catcher -N 8100` is the same list on 8100 instead. In the widget:

```yaml
ships: aiscatcher
aiscatcher-url: http://192.168.1.50:8119
refresh: 10
```

A host and port is enough. The widget requests `/api/ships.json`.
`http://127.0.0.1:8119` is the default, for AIS-catcher on the Pi itself.
`refresh` may be as short as 5 seconds. AISHub's one-minute floor does
not apply. Planes, base stations, and aids to navigation in that list are
not drawn as ships. See `examples/aiscatcher.yaml`.

If `/api/ships.json` answers 404 because only the newer HTTP API is on,
give the full path:

```yaml
aiscatcher-url: http://192.168.1.50:8111/api/v1/vessels
```

The widget then asks for the box around the station, at most 1000 ships.
A v1 API bound to the LAN rather than only `127.0.0.1` requires a key, or
`"public": true` in the AIS-catcher config. The key goes out as
`Authorization: Bearer`, and only to this ship feed:

```yaml
token: '{apikeys.aiscatcher}'
```

```yaml
# ApiKeys.yaml
aiscatcher: YOUR_KEY
```

Do not put the AISStream key in `token`. The open web viewer needs no
token. Ships the station no longer lists leave the scope on the next
reply. A ship it still lists is aged from how long ago the station heard
it, then dropped after `max-age`.

A page that is not showing closes the AISStream socket and does not poll.

## Install

From the top of a PiClock3 checkout:

```
git clone https://github.com/you/piclock3-airshipradar plugins/AirShipRadar
python3 PyQtPiClock3.py plugins/AirShipRadar/examples/example.yaml
```

The folder name is the plugin name:

```yaml
widgets:
  traffic:
    plugin: plugins.AirShipRadar
    region: radar
    latitude: 40.66
    longitude: -74.05
    bearing: 0
    radius: 25
    radius-unit: mi
    ships: openwaters
    flights: adsb
```

The example looks at New York Harbor, where the circle holds the port
and the arrivals into the airports around it. `bearing` is degrees true
at the top of the glass. The `circle` layout ships in `layouts/`. It
adds a `radar` region. It does not replace any region a shipped layout
already has.

`radius-unit` is `mi`, `km`, or `nm`. The caption prints the radius in
that unit.

To check the shape without Qt or a network:

```
python3 selftest.py
```

To check the clock config:

```
python3 PyQtPiClock3.py plugins/AirShipRadar/examples/example.yaml --check
```

## Settings

| | |
|---|---|
| `latitude`, `longitude` | Center of the circle. A number, or `{location.latitude}`. |
| `bearing` | Degrees true at the top. `0` is north. Tracking stays 360 either way. |
| `radius`, `radius-unit` | Reach of the circle. `mi`, `km`, or `nm`. |
| `ships` | `openwaters`, `aishub`, `aisstream`, `aiscatcher`, or `off`. Also `catcher` or `local`. |
| `flights` | `adsb`, `opensky`, or `off`. |
| `aiscatcher-url` | AIS-catcher web viewer. A host and port uses `/api/ships.json`. Default `http://127.0.0.1:8119`. |
| `username`, `apikey` | `{apikeys.aishub}` and `{apikeys.aisstream}`. |
| `token` | `{apikeys.aiscatcher}`, or empty. Bearer token for a locked v1 API. |
| `refresh` | Seconds. AISHub waits at least 60. The free feeds wait at least 15. AIS-catcher may be every 5. |
| `max-age` | Minutes a target stays after its last report. |
| `max-targets` | Nearest targets kept, per layer, when the circle is crowded. |
| `sweep` | Seconds for the beam to go all the way around. |
| `legend` | Color the two counts. On by default. |
| `font-size` | Caption size, a fraction of the region height. |

Color of the ships follows the theme when that color is light enough to
read on the dark glass. A dark theme color falls back to phosphor green.
Aircraft stay amber so the two layers do not become one color. The glass
itself stays dark so the scope still reads on a light page.

There is no map under it. This is the radar, not a chart.

## Credit

Vessel positions and aircraft positions are other people's receivers.

- Open Waters AIS — [openwaters.io](https://openwaters.io/ais/)
- adsb.lol contributors
- OpenSky Network — [opensky-network.org](https://opensky-network.org)
- AISHub — [aishub.net](https://www.aishub.net)
- AISStream — [aisstream.io](https://aisstream.io)
- AIS-catcher — the receiver on your own network, [jvde-github/AIS-catcher](https://github.com/jvde-github/AIS-catcher)
- Hull marks, AIS parsing, and the AISStream socket are derived from
  piclock3-ais-radar by ShawnPGHPublic, MIT.

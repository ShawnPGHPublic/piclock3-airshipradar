"""A full circle of ships and aircraft around a latitude and longitude.

The glass is a plan-position display. Range rings, a sweep that goes all
the way around, and both layers on the same scope. Ships stay the trace
color. Aircraft stay amber, so a harbor and a stack of arrivals can be
read at once. North is at the top unless bearing says otherwise.

Open Waters and adsb.lol need no key. AISHub is polled. AISStream stays
open while the page is the one on screen, and is closed when it is not.
"""
import logging
import math
import threading
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from PyQt5.QtCore import QPointF, QRectF, Qt, QThread, QTimer
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QLabel, QWidget

from PiClock3.Widget import Widget

from .feeds import (
    adsb_url,
    aishub_url,
    openwaters_url,
    opensky_url,
    planes_from_adsb,
    planes_from_opensky,
    ship_from_aisstream,
    ships_from_aishub,
    ships_from_openwaters,
)
from .radarmath import (
    bounding_box,
    format_radius,
    in_scope,
    mark_pixels,
    project,
    radius_meters,
    remember_identity,
    stream_note,
)
from .stream import dumps, open_stream, Reader

logger = logging.getLogger(__name__)

GLASS = QColor(6, 18, 13, 235)
PLANE = QColor(230, 179, 37)
UA = 'PiClock3-TrafficRadar/1.0'


def _plane_px(radius, span):
    # A 36 m jet is the reference. Light planes stay a small mark; a heavy
    # is larger, but nothing covers a range ring.
    meters = 24.0 if span is None else float(span)
    raw = radius * 0.055 * (meters / 36.0)
    return min(radius * 0.16, max(radius * 0.028, raw))


class Scope(QWidget):
    """the full disk.  The caption is a separate label under it."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.targets = []
        self.sweep = 0.0
        self.bearing = 0.0
        self.radius_m = 1.0
        self.ship = QColor('#5EE08A')
        self.rings = ('', '')

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        width, height = self.width(), self.height()
        if width < 8 or height < 8:
            painter.end()
            return
        radius = max(1.0, min(width, height) / 2.0 - 2.0)
        cx = width / 2.0
        cy = height / 2.0
        self._glass(painter, cx, cy, radius)
        self._rings(painter, cx, cy, radius)
        self._spokes(painter, cx, cy, radius)
        self._sweep(painter, cx, cy, radius)
        self._targets(painter, cx, cy, radius)
        self._marks(painter, cx, cy, radius)
        painter.end()

    def _glass(self, painter, cx, cy, radius):
        path = QPainterPath()
        path.addEllipse(QPointF(cx, cy), radius, radius)
        painter.setClipPath(path)
        painter.fillPath(path, GLASS)
        pen = QPen(self._dim(self.ship, 170))
        pen.setWidthF(max(1.2, radius * 0.008))
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

    def _rings(self, painter, cx, cy, radius):
        pen = QPen(self._dim(self.ship, 90))
        pen.setWidthF(max(1.0, radius * 0.004))
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        for frac in (0.25, 0.5, 0.75, 1.0):
            r = radius * frac
            painter.drawEllipse(QPointF(cx, cy), r, r)

    def _spokes(self, painter, cx, cy, radius):
        pen = QPen(self._dim(self.ship, 55))
        pen.setWidthF(max(1.0, radius * 0.003))
        painter.setPen(pen)
        for deg in range(0, 360, 30):
            self._spoke(painter, cx, cy, radius, deg)

    def _sweep(self, painter, cx, cy, radius):
        for i in range(26, -1, -1):
            deg = self.sweep - i * 1.7
            color = QColor(self.ship)
            color.setAlpha(int(150 * (1.0 - i / 26.0)))
            pen = QPen(color)
            pen.setWidthF(max(1.0, radius * (0.012 if i == 0 else 0.004)))
            painter.setPen(pen)
            self._spoke(painter, cx, cy, radius, deg)

    def _targets(self, painter, cx, cy, radius):
        ordered = sorted(self.targets, key=lambda item: (item[8] == 'plane', -(item[6] or 0)))
        for right, forward, sog, cog, heading, kind, length, beam, layer, on_ground, span, rotor in ordered:
            x = cx + right * radius
            y = cy - forward * radius
            color = PLANE if layer == 'plane' else self.ship
            if layer == 'plane':
                self._plane(painter, x, y, radius, sog, cog, heading, on_ground, color, span, rotor)
                continue
            moving = sog is not None and sog >= 0.4
            sized = length is not None and length > 0
            if kind in (None, '', 'other') and not sized:
                self._dot(painter, x, y, radius, moving, sog, cog, color)
                continue
            len_px, beam_px = mark_pixels(length, beam, kind, radius, self.radius_m or 1)
            rel = heading if heading is not None else cog
            if rel is None:
                rel = self.bearing
            paint = QColor(color)
            paint.setAlpha(230 if moving else 140)
            painter.save()
            painter.translate(x, y)
            painter.rotate(rel - self.bearing)
            pen = QPen(paint)
            pen.setWidthF(max(1.0, min(len_px, beam_px) * 0.08))
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            self._hull(painter, kind, len_px, beam_px)
            painter.restore()

    def _plane(self, painter, x, y, radius, sog, cog, heading, on_ground, color, span, rotor):
        paint = QColor(color)
        paint.setAlpha(120 if on_ground else 235)
        size = _plane_px(radius, span)
        if on_ground:
            dot = max(2.2, size * 0.34)
            pen = QPen(paint)
            pen.setWidthF(max(1.0, size * 0.08))
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(x, y), dot, dot)
            return
        rel = heading if heading is not None else cog
        if rel is None:
            rel = self.bearing
        painter.save()
        painter.translate(x, y)
        painter.rotate(rel - self.bearing)
        pen = QPen(paint)
        pen.setWidthF(max(1.15, size * 0.075))
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        if rotor:
            self._rotor(painter, size)
        elif span is not None and span <= 13.5:
            self._prop(painter, size)
        else:
            self._aircraft(painter, size)
        painter.restore()
        if sog is not None and sog >= 30 and cog is not None:
            pen = QPen(paint)
            pen.setWidthF(max(1.0, radius * 0.004))
            painter.setPen(pen)
            screen = math.radians(cog - self.bearing)
            length = min(radius * 0.1, radius * (0.02 + min(sog, 520) * 0.00011))
            painter.drawLine(
                QPointF(x, y),
                QPointF(x + math.sin(screen) * length,
                        y - math.cos(screen) * length))

    def _aircraft(self, painter, size):
        path = QPainterPath()
        s = size
        path.moveTo(0, -s)
        path.lineTo(s * 0.16, -s * 0.2)
        path.lineTo(s * 0.92, s * 0.02)
        path.lineTo(s * 0.16, s * 0.1)
        path.lineTo(s * 0.26, s * 0.62)
        path.lineTo(0, s * 0.4)
        path.lineTo(-s * 0.26, s * 0.62)
        path.lineTo(-s * 0.16, s * 0.1)
        path.lineTo(-s * 0.92, s * 0.02)
        path.lineTo(-s * 0.16, -s * 0.2)
        path.closeSubpath()
        painter.drawPath(path)

    def _prop(self, painter, size):
        path = QPainterPath()
        s = size
        path.moveTo(0, -s * 0.78)
        path.lineTo(s * 0.12, -s * 0.22)
        path.lineTo(s * 1.05, -s * 0.28)
        path.lineTo(s * 1.05, -s * 0.1)
        path.lineTo(s * 0.14, -s * 0.02)
        path.lineTo(s * 0.16, s * 0.58)
        path.lineTo(0, s * 0.4)
        path.lineTo(-s * 0.16, s * 0.58)
        path.lineTo(-s * 0.14, -s * 0.02)
        path.lineTo(-s * 1.05, -s * 0.1)
        path.lineTo(-s * 1.05, -s * 0.28)
        path.lineTo(-s * 0.12, -s * 0.22)
        path.closeSubpath()
        painter.drawPath(path)

    def _rotor(self, painter, size):
        s = size * 0.9
        painter.drawEllipse(QPointF(0, -s * 0.05), s * 0.72, s * 0.72)
        path = QPainterPath()
        path.moveTo(0, -s * 0.42)
        path.lineTo(s * 0.16, s * 0.12)
        path.lineTo(0, s * 0.48)
        path.lineTo(-s * 0.16, s * 0.12)
        path.closeSubpath()
        painter.drawPath(path)

    def _dot(self, painter, x, y, radius, moving, sog, cog, color):
        dot = max(2.0, radius * (0.014 if moving else 0.009))
        paint = QColor(color)
        paint.setAlpha(230 if moving else 130)
        painter.setPen(Qt.NoPen)
        painter.setBrush(paint)
        painter.drawEllipse(QPointF(x, y), dot, dot)
        if moving and cog is not None:
            pen = QPen(paint)
            pen.setWidthF(max(1.0, radius * 0.004))
            painter.setPen(pen)
            rel = math.radians(cog - self.bearing)
            length = min(radius * 0.09, radius * (0.018 + min(sog, 40) * 0.003))
            painter.drawLine(
                QPointF(x, y),
                QPointF(x + math.sin(rel) * length,
                        y - math.cos(rel) * length))

    def _hull(self, painter, kind, length, beam):
        # bow is up. Qt y grows down, so the bow is the negative-y end.
        path = QPainterPath()
        half_l = length / 2.0
        half_w = max(1.0, beam / 2.0)
        if kind == 'pleasure':
            path.moveTo(0, -half_l)
            path.lineTo(half_w, half_l)
            path.lineTo(-half_w, half_l)
        elif kind == 'fast':
            path.moveTo(0, -half_l)
            path.lineTo(half_w, length * 0.05)
            path.lineTo(beam * 0.14, half_l)
            path.lineTo(-beam * 0.14, half_l)
            path.lineTo(-half_w, length * 0.05)
        elif kind == 'fishing':
            path.moveTo(0, -half_l)
            path.lineTo(half_w, -length / 6.0)
            path.lineTo(half_w, half_l)
            path.lineTo(-half_w, half_l)
            path.lineTo(-half_w, -length / 6.0)
        elif kind == 'tug':
            bow = length * 0.38
            path.moveTo(0, -half_l)
            path.lineTo(half_w, -half_l + bow)
            path.lineTo(half_w, half_l)
            path.lineTo(-half_w, half_l)
            path.lineTo(-half_w, -half_l + bow)
        else:
            bow = length * (0.22 if kind == 'passenger' else 0.16 if kind == 'tanker' else 0.12)
            path.moveTo(0, -half_l)
            path.lineTo(half_w, -half_l + bow)
            path.lineTo(half_w, half_l - length * 0.05)
            path.lineTo(beam * 0.16, half_l)
            path.lineTo(-beam * 0.16, half_l)
            path.lineTo(-half_w, half_l - length * 0.05)
            path.lineTo(-half_w, -half_l + bow)
        path.closeSubpath()
        painter.drawPath(path)
        if kind == 'tanker':
            r = max(1.3, beam * 0.22)
            painter.drawEllipse(QPointF(0, length * 0.02), r, r)
        elif kind == 'passenger':
            painter.drawRect(QRectF(-beam * 0.28, length * 0.02, beam * 0.56, length * 0.28))
        elif kind == 'cargo':
            painter.drawLine(QPointF(-beam * 0.28, -length * 0.02),
                             QPointF(beam * 0.28, -length * 0.02))
            painter.drawLine(QPointF(-beam * 0.28, length * 0.16),
                             QPointF(beam * 0.28, length * 0.16))
        elif kind == 'fishing':
            painter.drawLine(QPointF(-beam * 0.85, -length * 0.02),
                             QPointF(beam * 0.85, -length * 0.02))

    def _marks(self, painter, cx, cy, radius):
        painter.setPen(self._dim(self.ship, 210))
        font = painter.font()
        font.setPixelSize(max(8, int(radius * 0.055)))
        painter.setFont(font)
        for true, text in ((0, 'N'), (90, 'E'), (180, 'S'), (270, 'W')):
            deg = (true - self.bearing) % 360.0
            x, y = self._at(cx, cy, radius * 0.86, deg)
            weight = Qt.AlignCenter
            painter.drawText(QRectF(x - 18, y - 10, 36, 20), weight, text)
        font.setPixelSize(max(7, int(radius * 0.04)))
        painter.setFont(font)
        painter.setPen(self._dim(self.ship, 160))
        for frac, text in ((1.0 / 3.0, self.rings[0]), (2.0 / 3.0, self.rings[1])):
            if not text:
                continue
            y = cy - radius * frac
            painter.drawText(QRectF(cx + 4, y - 12, radius * 0.45, 16),
                             Qt.AlignLeft | Qt.AlignVCenter, text)
        painter.setBrush(self.ship)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(cx, cy), max(2.0, radius * 0.012),
                            max(2.0, radius * 0.012))

    def _spoke(self, painter, cx, cy, radius, deg):
        x, y = self._at(cx, cy, radius, deg)
        painter.drawLine(QPointF(cx, cy), QPointF(x, y))

    def _at(self, cx, cy, radius, deg):
        rad = math.radians(deg)
        return (cx + math.sin(rad) * radius, cy - math.cos(rad) * radius)

    @staticmethod
    def _dim(color, alpha):
        dimmed = QColor(color)
        dimmed.setAlpha(alpha)
        return dimmed


class _FetchThread(QThread):
    """one HTTP GET off the UI thread."""

    def __init__(self, url):
        super().__init__()
        self.url = url
        self.body = b''
        self.error = ''

    def run(self):
        try:
            req = Request(self.url, headers={
                'User-Agent': UA,
                'Accept': 'application/json',
            })
            with urlopen(req, timeout=25) as resp:
                self.body = resp.read()
        except Exception:
            self.error = 'no reply'


class _AisThread(QThread):
    """holds the AISStream socket off the UI thread and queues texts."""

    def __init__(self, api_key, box):
        super().__init__()
        self.api_key = api_key
        self.box = box
        self.error = ''
        self.subscribed = False
        self._stop = False
        self._sock = None
        self._lock = threading.Lock()
        self._queue = []

    def stop(self):
        self._stop = True
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def drain(self):
        with self._lock:
            items = self._queue
            self._queue = []
        return items

    def run(self):
        sock = None
        try:
            sock, rest = open_stream()
            self._sock = sock
            from .stream import send_text
            send_text(sock, dumps(self.api_key, self.box))
            self.subscribed = True
            reader = Reader(sock, rest, lambda: self._stop)
            for text in reader.messages():
                if self._stop:
                    break
                with self._lock:
                    self._queue.append(text)
                    extra = len(self._queue) - 400
                    if extra > 0:
                        del self._queue[:extra]
        except Exception as exc:
            if not self._stop:
                self.error = 'stream down'
                logger.warning('aisstream %s', type(exc).__name__)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


class TrafficRadar(Widget):
    """called whatever suits.  The loader finds the Plugin subclass."""

    def __init__(self, piclock, name, config):
        super().__init__(piclock, name, config)
        self.scope = None
        self.caption = None
        self.anim = None
        self.ships = {}
        self.planes = {}
        self.identity = {}
        self.last_ok = None
        self.ship_status = 'waiting'
        self.plane_status = 'waiting'
        self.sweep = 0.0
        self._sized = None
        self.stream = None
        self.stream_next = 0.0
        self._box = None
        self.ship_thread = None
        self.plane_thread = None
        self.ship_next = 0.0
        self.plane_next = 0.0
        self._ship_mode = None
        self._plane_mode = None
        self._counts = (0, 0)

    def start(self):
        rect = self.region.frameRect()
        self.scope = Scope(self.region)
        self.caption = QLabel(self.region)
        self.caption.setObjectName('trafficradar')
        self.caption.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        self.caption.setTextFormat(Qt.RichText)
        self._layout(rect)
        self.applyEffect(self.caption, rect.height())
        self.anim = QTimer(self)
        self.anim.timeout.connect(self._animate)
        self.anim.start(50)
        self._kick()

    def pageChange(self):
        if self.region.isVisible():
            self.ship_next = 0.0
            self.plane_next = 0.0
            self._kick()
        else:
            self._stop_stream()

    def _animate(self):
        if self.scope is None:
            return
        rect = self.region.frameRect()
        if (rect.width(), rect.height()) != self._sized:
            self._layout(rect)
        if not self.region.isVisible():
            return
        self._collect()
        if self.stream is not None:
            self._drain()
        self._kick()
        period = max(2.0, float(self.config.get('sweep') or 8))
        self.sweep = (self.sweep + (0.050 / period) * 360.0) % 360.0
        self.scope.sweep = self.sweep
        self.scope.bearing = self._bearing()
        self.scope.ship = self._trace()
        try:
            self.scope.radius_m = self._place()[2]
        except (TypeError, ValueError):
            self.scope.radius_m = 1.0
        ships, planes = self._visible()
        self._counts = (len(ships), len(planes))
        self.scope.targets = ships + planes
        self.scope.rings = self._ring_labels()
        self.scope.setFont(self.region.font())
        self.scope.update()
        self._caption()

    def _layout(self, rect):
        props = self.scaleFont({'font-size': self.config.get('font-size', 0.042)},
                               rect.height())
        px = _px(props.get('font-size'), rect.height())
        cap = max(8, int(round(px * 1.35)))
        self.scope.setGeometry(0, 0, rect.width(), max(1, rect.height() - cap))
        self.caption.setGeometry(0, rect.height() - cap, rect.width(), cap)
        color = self.color()
        if color:
            props['color'] = color
        self.caption.setStyleSheet(self.styleRule('trafficradar', props))
        self._sized = (rect.width(), rect.height())
        self._caption()

    def _caption(self):
        if self.caption is None:
            return
        try:
            label = format_radius(self.config['radius'], self.config['radius-unit'])
        except (TypeError, ValueError):
            label = 'radius'
        ship_hex = self._trace().name()
        plane_hex = PLANE.name()
        legend = bool(self.config.get('legend', True))
        ships = _layer_text(self._ships_mode(), self.ship_status, self._counts[0],
                            'ship', 'ships', ship_hex if legend else '')
        planes = _layer_text(self._planes_mode(), self.plane_status, self._counts[1],
                             'aircraft', 'aircraft', plane_hex if legend else '')
        when = self._clock(self.last_ok) if self.last_ok else 'waiting'
        self.caption.setText('%s  ·  %s  ·  %s  ·  %s' % (
            _esc(label), ships, planes, _esc(when)))

    def _clock(self, epoch):
        try:
            zone = self.now().tzinfo
        except Exception:
            zone = None
        moment = datetime.fromtimestamp(epoch, zone or timezone.utc)
        return moment.strftime('%H:%M:%S')

    def _kick(self):
        if self.scope is None or not self.region.isVisible():
            return
        self._kick_ships()
        self._kick_planes()

    def _kick_ships(self):
        mode = self._ships_mode()
        if mode != self._ship_mode:
            self._stop_stream()
            self.ships = {}
            self._ship_mode = mode
            self.ship_status = 'off' if mode == 'off' else 'waiting'
            self.ship_next = 0.0
        if mode == 'off':
            self.ship_status = 'off'
            return
        if mode == 'aisstream':
            self._ensure_stream()
            return
        if self.ship_thread is not None:
            return
        now = time.time()
        if now < self.ship_next:
            return
        try:
            lat, lon, radius_m = self._place()
        except (TypeError, ValueError):
            self.ship_status = 'bad position'
            return
        if mode == 'aishub':
            username = self._secret('username')
            if not username:
                self.ship_status = 'no key'
                return
            url = aishub_url(username, lat, lon, radius_m, self._max_age())
        else:
            url = openwaters_url(lat, lon, radius_m)
            if not url:
                self.ship_status = 'radius too wide'
                return
        self.ship_next = now + self._interval(mode == 'aishub')
        self.ship_thread = _FetchThread(url)
        self.ship_thread.mode = mode
        self.ship_thread.start()

    def _kick_planes(self):
        mode = self._planes_mode()
        if mode != self._plane_mode:
            self.planes = {}
            self._plane_mode = mode
            self.plane_status = 'off' if mode == 'off' else 'waiting'
            self.plane_next = 0.0
        if mode == 'off':
            self.plane_status = 'off'
            return
        if self.plane_thread is not None:
            return
        now = time.time()
        if now < self.plane_next:
            return
        try:
            lat, lon, radius_m = self._place()
        except (TypeError, ValueError):
            self.plane_status = 'bad position'
            return
        if mode == 'opensky':
            url = opensky_url(lat, lon, radius_m)
        else:
            url = adsb_url(lat, lon, radius_m)
        self.plane_next = now + self._interval(False)
        self.plane_thread = _FetchThread(url)
        self.plane_thread.mode = mode
        self.plane_thread.start()

    def _collect(self):
        thread = self.ship_thread
        if thread is not None and not thread.isRunning():
            self.ship_thread = None
            self._take_ships(thread)
        thread = self.plane_thread
        if thread is not None and not thread.isRunning():
            self.plane_thread = None
            self._take_planes(thread)

    def _take_ships(self, thread):
        if thread.error or not thread.body:
            self.ship_status = thread.error or 'no reply'
            return
        parser = ships_from_aishub if getattr(thread, 'mode', '') == 'aishub' else ships_from_openwaters
        try:
            rows, err = parser(thread.body)
        except Exception:
            logger.warning('%s: could not read ship reply', self.name)
            self.ship_status = 'bad reply'
            return
        if err:
            self.ship_status = err
            return
        now = time.time()
        fresh = {}
        for vessel in rows:
            vessel['seen'] = now
            remember_identity(self.identity, vessel)
            fresh[vessel['id']] = vessel
        self.ships = fresh
        self.ship_status = ''
        self.last_ok = now

    def _take_planes(self, thread):
        if thread.error or not thread.body:
            self.plane_status = thread.error or 'no reply'
            return
        parser = planes_from_opensky if getattr(thread, 'mode', '') == 'opensky' else planes_from_adsb
        try:
            rows, err = parser(thread.body)
        except Exception:
            logger.warning('%s: could not read flight reply', self.name)
            self.plane_status = 'bad reply'
            return
        if err:
            self.plane_status = err
            return
        now = time.time()
        fresh = {}
        for plane in rows:
            plane['seen'] = now
            fresh[plane['id']] = plane
        self.planes = fresh
        self.plane_status = ''
        self.last_ok = now

    def _ensure_stream(self):
        if self.stream is not None and self.stream.isRunning():
            if self.stream.box != self._wanted_box():
                self._stop_stream()
            else:
                return
        now = time.time()
        if now < self.stream_next:
            return
        key = self._secret('apikey')
        if not key:
            self.ship_status = 'no key'
            return
        try:
            box = self._wanted_box()
        except (TypeError, ValueError):
            self.ship_status = 'bad position'
            return
        self.stream_next = now + 15
        self.stream = _AisThread(key, box)
        self.stream.start()
        if not self.last_ok:
            self.ship_status = 'connecting'

    def _wanted_box(self):
        lat, lon, radius_m = self._place()
        box = bounding_box(lat, lon, radius_m)
        return (round(box['latmin'], 4), round(box['latmax'], 4),
                round(box['lonmin'], 4), round(box['lonmax'], 4))

    def _drain(self):
        if self.stream is None:
            return
        now = time.time()
        heard = False
        for text in self.stream.drain():
            try:
                note = stream_note(text)
            except Exception:
                note = None
            if note == 'live':
                if self.ship_status == 'connecting':
                    self.ship_status = 'live'
                continue
            if isinstance(note, str) and note.startswith('error:'):
                self.ship_status = note.split(':', 1)[1] or 'rejected'
                continue
            try:
                vessel = ship_from_aisstream(text)
            except Exception:
                continue
            if not vessel:
                continue
            if vessel.get('name_only'):
                remember_identity(self.identity, vessel)
                current = self.ships.get(vessel['id'])
                if current:
                    self._keep_identity(current, vessel)
                continue
            remember_identity(self.identity, vessel)
            self._remember_ship(vessel, now)
            heard = True
        if heard:
            self.last_ok = now
            self.ship_status = ''
        elif self.stream.error and not self.stream.isRunning():
            self.ship_status = self.stream.error

    def _remember_ship(self, vessel, now):
        key = vessel.get('id') or ('mmsi:' + str(vessel.get('mmsi') or ''))
        vessel['seen'] = now
        vessel['id'] = key
        previous = self.ships.get(key)
        if previous:
            self._keep_identity(vessel, previous)
        self.ships[key] = vessel
        if len(self.ships) > 4000:
            ranked = sorted(self.ships.items(), key=lambda item: item[1].get('seen', 0))
            self.ships = dict(ranked[-2000:])

    def _keep_identity(self, vessel, previous):
        if vessel.get('kind') in (None, '', 'other') and previous.get('kind') not in (None, '', 'other'):
            vessel['kind'] = previous['kind']
        if vessel.get('length') is None and previous.get('length') is not None:
            vessel['length'] = previous['length']
        if vessel.get('beam') is None and previous.get('beam') is not None:
            vessel['beam'] = previous['beam']
        if not vessel.get('name') and previous.get('name'):
            vessel['name'] = previous['name']

    def _stop_stream(self):
        thread = self.stream
        self.stream = None
        if thread is not None:
            thread.stop()

    def _visible(self):
        try:
            lat, lon, radius_m = self._place()
        except (TypeError, ValueError):
            return [], []
        bearing = self._bearing()
        return (
            self._project(self.ships, 'ship', lat, lon, bearing, radius_m),
            self._project(self.planes, 'plane', lat, lon, bearing, radius_m),
        )

    def _project(self, bucket, layer, lat, lon, bearing, radius_m):
        now = time.time()
        limit = self._max_age() * 60
        found = []
        for item in bucket.values():
            if now - item.get('seen', 0) > limit:
                continue
            point = project(item['lat'], item['lon'], lat, lon, bearing, radius_m)
            if not in_scope(point):
                continue
            found.append((
                point[0] ** 2 + point[1] ** 2,
                point[0], point[1],
                item.get('sog'), item.get('cog'), item.get('heading'),
                item.get('kind') or ('aircraft' if layer == 'plane' else 'other'),
                item.get('length'), item.get('beam'),
                layer, bool(item.get('on_ground')),
                item.get('span'), bool(item.get('rotor')),
            ))
        found.sort()
        cap = int(self.config.get('max-targets') or 400)
        return [(r, f, sog, cog, heading, kind, length, beam, lay, ground, span, rotor)
                for _d, r, f, sog, cog, heading, kind, length, beam, lay, ground, span, rotor
                in found[:cap]]

    def _ring_labels(self):
        try:
            radius = float(self.config['radius'])
            unit = self.config['radius-unit']
        except (TypeError, ValueError, KeyError):
            return ('', '')
        return (format_radius(radius / 3.0, unit),
                format_radius(radius * 2.0 / 3.0, unit))

    def _place(self):
        lat = self._number('latitude')
        lon = self._number('longitude')
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError('position')
        radius_m = radius_meters(self.config['radius'], self.config['radius-unit'])
        return lat, lon, radius_m

    def _number(self, key):
        value = self.config[key]
        if isinstance(value, str):
            value = self.expand(value).strip()
        return float(value)

    def _bearing(self):
        try:
            return float(self.config.get('bearing') or 0) % 360.0
        except (TypeError, ValueError):
            return 0.0

    def _ships_mode(self):
        name = str(self.config.get('ships') or 'openwaters').strip().lower()
        if name in ('off', 'none', 'no'):
            return 'off'
        if name in ('aishub', 'ais-hub', 'hub'):
            return 'aishub'
        if name in ('aisstream', 'aisstream.io', 'stream'):
            return 'aisstream'
        return 'openwaters'

    def _planes_mode(self):
        name = str(self.config.get('flights') or 'adsb').strip().lower()
        if name in ('off', 'none', 'no'):
            return 'off'
        if name in ('opensky', 'open-sky', 'osky'):
            return 'opensky'
        return 'adsb'

    def _secret(self, key):
        raw = self.config.get(key, '')
        if raw is None:
            return ''
        if not isinstance(raw, str):
            raw = str(raw)
        try:
            value = self.expand(raw).strip()
        except Exception:
            return ''
        if not value or '{' in value or '}' in value:
            return ''
        return value

    def _max_age(self):
        try:
            return max(1, min(180, int(float(self.config.get('max-age') or 10))))
        except (TypeError, ValueError):
            return 10

    def _interval(self, aishub):
        try:
            seconds = int(float(self.config.get('refresh') or 20))
        except (TypeError, ValueError):
            seconds = 20
        if aishub:
            return max(60, seconds)
        return max(15, seconds)

    def _trace(self):
        color = QColor(self.color() or '#5EE08A')
        if not color.isValid() or color.lightness() < 70:
            color = QColor('#5EE08A')
        return color


def _layer_text(mode, status, count, one, many, color):
    if mode == 'off':
        label = '%s off' % one
    elif status:
        label = '%s %s' % (one, status)
    else:
        label = '%d %s' % (count, one if count == 1 else many)
    if color:
        return '<span style="color:%s">%s</span>' % (color, _esc(label))
    return _esc(label)


def _esc(text):
    return (str(text).replace('&', '&').replace('<', '<').replace('>', '>'))


def _px(font_size, height):
    text = str(font_size or '')
    if text.endswith('px'):
        try:
            return max(8, int(float(text[:-2])))
        except ValueError:
            pass
    return max(8, int(height * 0.042))

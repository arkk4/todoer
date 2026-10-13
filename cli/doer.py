#!/usr/bin/env python3
"""
doer — terminal task & calendar manager on top of Apple Reminders & Calendar.

Tasks live in Reminders; events live in Calendar.
Location alerts and travel-time alarms use a compiled Swift/EventKit & MapKit helper
(walk / car / public transit — all routing is done by Apple Maps, no external services).
"""
from __future__ import annotations

import base64 as _b64mod
import calendar
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGO_PATH = REPO_ROOT / "public" / "logo.svg"
VERSION = "1.0.0"
APP = "doer"

def _is_iterm2() -> bool:
    return ("iTerm" in os.environ.get("TERM_PROGRAM", "") or
            "iTerm" in os.environ.get("LC_TERMINAL", ""))

_logo_cache: "str | None" = None

def _logo_png_b64() -> "str | None":
    global _logo_cache
    if _logo_cache is not None:
        return _logo_cache
    p = str(LOGO_PATH)
    if not os.path.exists(p):
        return None
    try:
        svg_bytes = open(p, "rb").read()
    except OSError:
        return None
    try:
        r = subprocess.run(["rsvg-convert", "-w", "128", "-f", "png"],
                           input=svg_bytes, capture_output=True, timeout=5)
        if r.returncode == 0 and r.stdout:
            _logo_cache = _b64mod.b64encode(r.stdout).decode("ascii")
            return _logo_cache
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    _JS = ("import('@resvg/resvg-js').then(({Resvg})=>{" +
           "const png=new Resvg(process.stdin.read(),{fitTo:{mode:'width',value:128}}).render().asPng();" +
           "process.stdout.write(png);}).catch(()=>process.exit(1));")
    try:
        r = subprocess.run(["node", "--input-type=module", "-e", _JS],
                           input=svg_bytes, capture_output=True, timeout=10)
        if r.returncode == 0 and r.stdout:
            _logo_cache = _b64mod.b64encode(r.stdout).decode("ascii")
            return _logo_cache
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None

def get_banner_text() -> str:
    if not _is_iterm2():
        return f"  \033[1m{APP}\033[0m \033[2mv{VERSION}\033[0m"
    logo_b64 = _logo_png_b64()
    if logo_b64:
        nb = _b64mod.b64encode(b"logo").decode()
        esc = f"\033]1337;File=name={nb};width=4;height=2;preserveAspectRatio=1;inline=1:{logo_b64}\a"
        return esc + "\033[1A" + f"  \033[1m{APP}\033[0m \033[2mv{VERSION}\033[0m"
    return f"  \033[1m{APP}\033[0m \033[2mv{VERSION}\033[0m"

# ───────────────────────────── styles ─────────────────────────────

USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _c(code: str) -> str:
    return f"\033[{code}m" if USE_COLOR else ""


RESET, BOLD, DIM = _c("0"), _c("1"), _c("2")
RED, GREEN, YELLOW, MAGENTA, CYAN, BLUE = _c("31"), _c("32"), _c("33"), _c("35"), _c("36"), _c("34")
ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def vlen(s: str) -> int:
    return len(ANSI_RE.sub("", s))


WD_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WD_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# ───────────────────────────── errors & CLI ─────────────────────────────


class CLIError(Exception):
    """reminders-cli / helper error."""


class UserError(Exception):
    """Input / operational error."""


def run(*args: str, timeout: int = 25) -> str:
    try:
        p = subprocess.run(["reminders", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise CLIError("reminders-cli not found: brew tap keith/formulae && brew install reminders-cli")
    except subprocess.TimeoutExpired:
        raise CLIError("reminders-cli timed out (check Reminders access in System Settings)")
    if p.returncode != 0:
        raise CLIError((p.stderr or p.stdout).strip() or f"exit code {p.returncode}")
    return p.stdout


# ───────────────────────────── swift EventKit & MapKit helper ─────────────────────────────

HELPER_SWIFT = r'''
import Foundation
import EventKit
import CoreLocation
import MapKit

let store = EKEventStore()

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(1)
}

func emit(_ obj: Any) {
    guard let data = try? JSONSerialization.data(withJSONObject: obj, options: []) else { fail("JSON serialization error") }
    print(String(data: data, encoding: .utf8)!)
}

func awaitFlag(_ timeout: TimeInterval, _ isDone: () -> Bool) {
    let deadline = Date(timeIntervalSinceNow: timeout)
    while !isDone() && Date() < deadline {
        RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.1))
    }
}

func requestAccess(toEvents: Bool, toReminders: Bool) {
    if toEvents {
        let sem = DispatchSemaphore(value: 0)
        var granted = false
        let handler: (Bool, Error?) -> Void = { ok, _ in granted = ok; sem.signal() }
        if #available(macOS 14.0, *) {
            store.requestFullAccessToEvents(completion: handler)
        } else {
            store.requestAccess(to: .event, completion: handler)
        }
        sem.wait()
        if !granted { fail("Calendar access denied (System Settings > Privacy & Security > Calendars)") }
    }
    if toReminders {
        let sem = DispatchSemaphore(value: 0)
        var granted = false
        let handler: (Bool, Error?) -> Void = { ok, _ in granted = ok; sem.signal() }
        if #available(macOS 14.0, *) {
            store.requestFullAccessToReminders(completion: handler)
        } else {
            store.requestAccess(to: .reminder, completion: handler)
        }
        sem.wait()
        if !granted { fail("Reminders access denied (System Settings > Privacy & Security > Reminders)") }
    }
}

func findReminder(_ id: String) -> EKReminder {
    for item in store.calendarItems(withExternalIdentifier: id) {
        if let r = item as? EKReminder { return r }
    }
    fail("Reminder not found: \(id)")
}

func findEvent(_ id: String) -> EKEvent {
    for item in store.calendarItems(withExternalIdentifier: id) {
        if let e = item as? EKEvent { return e }
    }
    if let e = store.event(withIdentifier: id) {
        return e
    }
    fail("Calendar event not found: \(id)")
}

func geocode(_ q: String) {
    var done = false
    var result: [String: Any]? = nil
    var message = "No results for: \(q)"
    CLGeocoder().geocodeAddressString(q) { marks, err in
        if let p = marks?.first, let c = p.location?.coordinate {
            let street = [p.thoroughfare, p.subThoroughfare].compactMap { $0 }.joined(separator: " ")
            let city = [p.postalCode, p.locality].compactMap { $0 }.joined(separator: " ")
            let addr = [street, city].filter { !$0.isEmpty }.joined(separator: ", ")
            result = ["name": p.name ?? q, "address": addr, "lat": c.latitude, "lon": c.longitude]
        } else if let e = err {
            message = e.localizedDescription
        }
        done = true
    }
    awaitFlag(20) { done }
    if let r = result { emit(r) } else { fail(message) }
}

// ── routing (Apple Maps) ──

struct Estimate {
    var seconds: Double
    var departure: Date?
}

func mapItem(_ c: CLLocationCoordinate2D) -> MKMapItem {
    return MKMapItem(placemark: MKPlacemark(coordinate: c))
}

/// One Apple Maps estimate. Tries calculateETA first (works for every transport type,
/// honours arrivalDate/departureDate), then falls back to a full route calculation.
/// For transit with an arrival date the result is "when must I leave" -> arrival minus
/// the expected departure of the best timetable connection (so waiting time is included).
func estimate(from: CLLocationCoordinate2D, to: CLLocationCoordinate2D, type: MKDirectionsTransportType,
              arrival: Date? = nil, departure: Date? = nil) -> (Estimate?, String?) {
    let req = MKDirections.Request()
    req.source = mapItem(from)
    req.destination = mapItem(to)
    req.transportType = type
    if let a = arrival, a > Date() { req.arrivalDate = a }
    if let d = departure, d > Date() { req.departureDate = d }

    var done = false
    var result: Estimate? = nil
    var errorMsg: String? = nil

    let etaDir = MKDirections(request: req)
    etaDir.calculateETA { res, err in
        if let r = res {
            var secs = r.expectedTravelTime
            if let a = arrival, type == .transit {
                let viaTimetable = a.timeIntervalSince(r.expectedDepartureDate)
                if viaTimetable > 0 { secs = viaTimetable }
            }
            result = Estimate(seconds: secs, departure: r.expectedDepartureDate)
        } else {
            errorMsg = err?.localizedDescription ?? "no route found"
        }
        done = true
    }
    awaitFlag(12) { done }
    if let r = result { return (r, nil) }

    done = false
    let routeDir = MKDirections(request: req)
    routeDir.calculate { res, err in
        if let r = res?.routes.first {
            result = Estimate(seconds: r.expectedTravelTime, departure: nil)
        } else if let e = err {
            errorMsg = e.localizedDescription
        }
        done = true
    }
    awaitFlag(12) { done }
    if let r = result { return (r, nil) }
    return (nil, errorMsg ?? "Apple Maps did not answer in time")
}

func calculateRoute(fromLat: Double, fromLon: Double, toLat: Double, toLon: Double, mode: String, arrivalIso: String?) {
    let from = CLLocationCoordinate2D(latitude: fromLat, longitude: fromLon)
    let to = CLLocationCoordinate2D(latitude: toLat, longitude: toLon)
    var arrival: Date? = nil
    if let s = arrivalIso, !s.isEmpty { arrival = ISO8601DateFormatter().date(from: s) }

    var est: Estimate? = nil
    var err: String? = nil
    var how = "live estimate"

    switch mode {
    case "transit":
        if arrival != nil {
            (est, err) = estimate(from: from, to: to, type: .transit, arrival: arrival)
            if est != nil { how = "timetable" }
        }
        if est == nil {
            (est, err) = estimate(from: from, to: to, type: .transit)
            how = "rough estimate, no timetable"
        }
    case "car":
        (est, err) = estimate(from: from, to: to, type: .automobile)
        if let first = est, let a = arrival {
            // second pass: ask for the traffic forecast at the time we would really be driving
            let (refined, _) = estimate(from: from, to: to, type: .automobile,
                                        departure: a.addingTimeInterval(-first.seconds))
            if let r = refined { est = r; how = "traffic forecast" }
        }
    default:
        (est, err) = estimate(from: from, to: to, type: .walking)
    }

    guard let e = est, e.seconds > 0 else { fail("Route error: \(err ?? "empty route")") }
    emit(["seconds": Int(e.seconds.rounded()), "how": how])
}

// ── reminders / calendar ──

func dumpRemindersMeta() {
    let sem = DispatchSemaphore(value: 0)
    var result: [String: Any] = [:]
    store.fetchReminders(matching: store.predicateForReminders(in: nil)) { reminders in
        for r in reminders ?? [] {
            var item: [String: Any] = [:]
            for a in r.alarms ?? [] {
                if let l = a.structuredLocation, let g = l.geoLocation {
                    item["loc"] = [
                        "title": l.title ?? "",
                        "lat": g.coordinate.latitude,
                        "lon": g.coordinate.longitude,
                        "radius": l.radius,
                        "mode": a.proximity == .leave ? "out" : "in"
                    ]
                } else if a.relativeOffset < 0 {
                    item["alarm_offset"] = Int(a.relativeOffset)
                }
            }
            if !item.isEmpty {
                result[r.calendarItemExternalIdentifier] = item
            }
        }
        sem.signal()
    }
    sem.wait()
    emit(result)
}

func dumpCalendarEvents(startIso: String, endIso: String) {
    let iso = ISO8601DateFormatter()
    guard let start = iso.date(from: startIso), let end = iso.date(from: endIso) else {
        fail("Invalid date range ISO format: \(startIso) - \(endIso)")
    }
    let predicate = store.predicateForEvents(withStart: start, end: end, calendars: nil)
    let events = store.events(matching: predicate)

    var out: [[String: Any]] = []
    for e in events {
        var locDict: [String: Any]? = nil
        if let sl = e.structuredLocation, let g = sl.geoLocation {
            locDict = [
                "title": sl.title ?? e.location ?? "",
                "lat": g.coordinate.latitude,
                "lon": g.coordinate.longitude,
                "radius": sl.radius,
                "mode": "in"
            ]
        }
        var alarmOffset: Int? = nil
        for a in e.alarms ?? [] where a.relativeOffset < 0 {
            alarmOffset = Int(a.relativeOffset)
            break
        }
        let extId = e.calendarItemExternalIdentifier ?? e.eventIdentifier ?? ""
        out.append([
            "id": extId,
            "title": e.title ?? "",
            "start": iso.string(from: e.startDate),
            "end": iso.string(from: e.endDate),
            "calendar": e.calendar.title,
            "notes": e.notes ?? "",
            "isAllDay": e.isAllDay,
            "loc": locDict as Any,
            "alarm_offset": alarmOffset as Any
        ])
    }
    emit(out)
}

func listCalendars() {
    let cals = store.calendars(for: .event).map { $0.title }
    let def = store.defaultCalendarForNewEvents?.title ?? (cals.first ?? "Calendar")
    emit(["default": def, "all": cals])
}

func saveEvent(jsonStr: String) {
    guard let data = jsonStr.data(using: .utf8),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        fail("Invalid JSON payload")
    }
    let iso = ISO8601DateFormatter()
    let id = obj["id"] as? String
    let e: EKEvent
    if let id = id, !id.isEmpty {
        e = findEvent(id)
    } else {
        e = EKEvent(eventStore: store)
    }
    e.title = obj["title"] as? String ?? "Untitled"
    if let startStr = obj["start"] as? String, let s = iso.date(from: startStr) { e.startDate = s }
    if let endStr = obj["end"] as? String, let en = iso.date(from: endStr) { e.endDate = en }
    e.isAllDay = obj["isAllDay"] as? Bool ?? false
    e.notes = obj["notes"] as? String ?? ""

    if let calName = obj["calendar"] as? String,
       let c = store.calendars(for: .event).first(where: { $0.title.lowercased() == calName.lowercased() }) {
        e.calendar = c
    } else if e.calendar == nil {
        guard let defaultCal = store.defaultCalendarForNewEvents else {
            fail("No default calendar found")
        }
        e.calendar = defaultCal
    }

    if let loc = obj["loc"] as? [String: Any],
       let lat = loc["lat"] as? Double, let lon = loc["lon"] as? Double {
        let sl = EKStructuredLocation(title: loc["title"] as? String ?? "")
        sl.geoLocation = CLLocation(latitude: lat, longitude: lon)
        sl.radius = loc["radius"] as? Double ?? 150
        e.structuredLocation = sl
    } else if obj["loc_clear"] as? Bool == true {
        e.structuredLocation = nil
        e.location = nil
    }

    if let offset = obj["alarm_offset"] as? Int {
        e.alarms = [EKAlarm(relativeOffset: TimeInterval(offset))]
    } else if obj["alarm_clear"] as? Bool == true {
        e.alarms = nil
    }

    do {
        try store.save(e, span: .thisEvent, commit: true)
        let extId = e.calendarItemExternalIdentifier ?? e.eventIdentifier ?? ""
        emit(["id": extId])
    } catch {
        fail("Failed to save calendar event: \(error.localizedDescription)")
    }
}

func deleteEvent(_ id: String) {
    let e = findEvent(id)
    do {
        try store.remove(e, span: .thisEvent, commit: true)
        emit(["ok": true])
    } catch {
        fail("Failed to delete event: \(error.localizedDescription)")
    }
}

func setReminderAlarm(_ id: String, offset: Int) {
    let r = findReminder(id)
    r.alarms = (r.alarms ?? []).filter { $0.structuredLocation != nil }
    if offset < 0 {
        r.addAlarm(EKAlarm(relativeOffset: TimeInterval(offset)))
    }
    do { try store.save(r, commit: true); emit(["ok": true]) } catch { fail("Save failed: \(error.localizedDescription)") }
}

func setReminderLocation(_ id: String, title: String, lat: Double, lon: Double, radius: Double, mode: String) {
    let r = findReminder(id)
    for a in r.alarms ?? [] where a.structuredLocation != nil { r.removeAlarm(a) }
    let loc = EKStructuredLocation(title: title)
    loc.geoLocation = CLLocation(latitude: lat, longitude: lon)
    loc.radius = radius
    let alarm = EKAlarm()
    alarm.structuredLocation = loc
    alarm.proximity = (mode == "out") ? .leave : .enter
    r.addAlarm(alarm)
    do { try store.save(r, commit: true); emit(["ok": true]) } catch { fail("Save failed: \(error.localizedDescription)") }
}

func clearReminderLocation(_ id: String) {
    let r = findReminder(id)
    for a in r.alarms ?? [] where a.structuredLocation != nil { r.removeAlarm(a) }
    do { try store.save(r, commit: true); emit(["ok": true]) } catch { fail("Save failed: \(error.localizedDescription)") }
}

let args = CommandLine.arguments
if args.count < 2 { fail("usage: tbhelper <cmd>") }
let cmd = args[1]

switch cmd {
case "geocode":
    if args.count < 3 { fail("usage: geocode <query>") }
    geocode(args[2])
case "route":
    if args.count < 7 { fail("usage: route <fLat> <fLon> <tLat> <tLon> <walk|car|transit> [arrivalIso]") }
    calculateRoute(fromLat: Double(args[2]) ?? 0, fromLon: Double(args[3]) ?? 0,
                   toLat: Double(args[4]) ?? 0, toLon: Double(args[5]) ?? 0, mode: args[6],
                   arrivalIso: args.count > 7 ? args[7] : nil)
case "rem-dump":
    requestAccess(toEvents: false, toReminders: true)
    dumpRemindersMeta()
case "rem-set-loc":
    if args.count < 8 { fail("usage: rem-set-loc <id> <title> <lat> <lon> <radius> <in|out>") }
    requestAccess(toEvents: false, toReminders: true)
    setReminderLocation(args[2], title: args[3], lat: Double(args[4]) ?? 0, lon: Double(args[5]) ?? 0,
                        radius: Double(args[6]) ?? 150, mode: args[7])
case "rem-clear-loc":
    if args.count < 3 { fail("usage: rem-clear-loc <id>") }
    requestAccess(toEvents: false, toReminders: true)
    clearReminderLocation(args[2])
case "rem-set-alarm":
    if args.count < 4 { fail("usage: rem-set-alarm <id> <offset_seconds>") }
    requestAccess(toEvents: false, toReminders: true)
    setReminderAlarm(args[2], offset: Int(args[3]) ?? 0)
case "cal-dump":
    if args.count < 4 { fail("usage: cal-dump <startIso> <endIso>") }
    requestAccess(toEvents: true, toReminders: false)
    dumpCalendarEvents(startIso: args[2], endIso: args[3])
case "cal-lists":
    requestAccess(toEvents: true, toReminders: false)
    listCalendars()
case "cal-save":
    if args.count < 3 { fail("usage: cal-save <json>") }
    requestAccess(toEvents: true, toReminders: false)
    saveEvent(jsonStr: args[2])
case "cal-delete":
    if args.count < 3 { fail("usage: cal-delete <id>") }
    requestAccess(toEvents: true, toReminders: false)
    deleteEvent(args[2])
default:
    fail("Unknown command: \(cmd)")
}
'''

HELPER_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>dev.tb.helper</string>
<key>NSRemindersUsageDescription</key><string>tb sets reminder alerts and checklists.</string>
<key>NSRemindersFullAccessUsageDescription</key><string>tb sets reminder alerts and checklists.</string>
<key>NSCalendarsUsageDescription</key><string>tb manages calendar events and travel alarms.</string>
<key>NSCalendarsFullAccessUsageDescription</key><string>tb manages calendar events and travel alarms.</string>
</dict></plist>
"""

_OLD_CFG = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "tb")
_NEW_CFG = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "doer")
CONFIG_DIR = _NEW_CFG if os.path.exists(_NEW_CFG) or not os.path.exists(_OLD_CFG) else _OLD_CFG
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "doer")
HELPER_BIN = os.path.join(CACHE_DIR, "doerhelper")
HELPER_VER = hashlib.sha1((HELPER_SWIFT + HELPER_PLIST).encode()).hexdigest()[:12]


def helper_ready() -> Optional[str]:
    try:
        with open(HELPER_BIN + ".ver") as f:
            if f.read().strip() == HELPER_VER and os.access(HELPER_BIN, os.X_OK):
                return HELPER_BIN
    except OSError:
        pass
    return None


def ensure_helper() -> str:
    path = helper_ready()
    if path:
        return path
    if not shutil.which("swiftc"):
        raise UserError("Need Swift tools: run 'xcode-select --install'")
    print(f"  {DIM}Compiling EventKit & MapKit helper (~12s, one time)…{RESET}", flush=True)
    src = os.path.join(CACHE_DIR, "src")
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "main.swift"), "w") as f:
        f.write(HELPER_SWIFT)
    with open(os.path.join(src, "Info.plist"), "w") as f:
        f.write(HELPER_PLIST)
    p = subprocess.run(
        ["swiftc", "-O", "-swift-version", "5", "main.swift", "-o", HELPER_BIN,
         "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist", "-Xlinker", "Info.plist"],
        cwd=src, capture_output=True, text=True,
    )
    if p.returncode != 0:
        raise CLIError("Failed to compile helper:\n" + p.stderr.strip())
    with open(HELPER_BIN + ".ver", "w") as f:
        f.write(HELPER_VER)
    return HELPER_BIN


def helper(*args: str, timeout: int = 40) -> str:
    path = ensure_helper()
    try:
        p = subprocess.run([path, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CLIError("Helper timed out")
    if p.returncode != 0:
        raise CLIError((p.stderr or p.stdout).strip() or "Helper execution failed")
    return p.stdout


def geocode(query: str) -> dict:
    try:
        return json.loads(helper("geocode", query))
    except json.JSONDecodeError:
        raise CLIError("Unexpected geocoder response")


# ───────────────────────────── saved places ─────────────────────────────

PLACES_FILE = os.path.join(CONFIG_DIR, "places.json")
DEFAULT_RADIUS = 150


def load_places() -> Dict[str, dict]:
    try:
        with open(PLACES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_places(places: Dict[str, dict]) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(PLACES_FILE, "w", encoding="utf-8") as f:
        json.dump(places, f, ensure_ascii=False, indent=2)


def match_place(q: str, places: Dict[str, dict]) -> Optional[str]:
    ql = q.lower()
    if ql in places:
        return ql
    hits = [k for k in places if k.startswith(ql)]
    return hits[0] if len(hits) == 1 else None


# ───────────────────────────── travel time (Apple Maps only) ─────────────────────────────


def calculate_travel(mode: str, origin: dict, dest: dict, arrival: datetime) -> Tuple[int, str]:
    """Travel time in seconds from `origin` to `dest`, arriving at `arrival`.

    Returns (seconds, how) where `how` says what the number is based on
    (timetable / traffic forecast / live estimate). Never guesses silently:
    an empty or failed route raises CLIError.
    """
    out = helper(
        "route", str(origin["lat"]), str(origin["lon"]), str(dest["lat"]), str(dest["lon"]),
        mode, arrival.astimezone().isoformat(), timeout=75,
    )
    try:
        data = json.loads(out)
        seconds = int(data.get("seconds") or 0)
    except (json.JSONDecodeError, ValueError, TypeError):
        raise CLIError("Unexpected route response from helper")
    if seconds <= 0:
        raise CLIError("Apple Maps returned an empty route")
    return seconds, str(data.get("how") or "estimate")


# ───────────────────────────── model ─────────────────────────────

SUB_RE = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s?(.*)$")
PRIO_NAME = {0: "none", 1: "low", 2: "medium", 3: "high"}


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(re.sub(r"\.\d+", "", s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


@dataclass
class Task:
    id: str
    list: str
    title: str
    desc: str = ""
    subs: List[list] = field(default_factory=list)
    due: Optional[datetime] = None
    end: Optional[datetime] = None
    has_time: bool = False
    priority: int = 0
    done: bool = False
    created: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    loc: Optional[dict] = None
    is_event: bool = False
    alarm_offset: Optional[int] = None

    def set_notes(self, text: str) -> None:
        desc_lines, subs = [], []
        for line in (text or "").splitlines():
            m = SUB_RE.match(line)
            if m:
                subs.append([m.group(1) in "xX", m.group(2).strip()])
            else:
                desc_lines.append(line.rstrip())
        self.desc = "\n".join(desc_lines).strip("\n")
        self.subs = subs

    @property
    def notes(self) -> str:
        parts = []
        if self.desc.strip():
            parts.append(self.desc.strip("\n"))
        if self.subs:
            parts.append("\n".join(f"- [{'x' if d else ' '}] {t}" for d, t in self.subs))
        return "\n\n".join(parts)

    def copy(self) -> "Task":
        return replace(self, subs=[list(s) for s in self.subs], loc=dict(self.loc) if self.loc else None)

    @staticmethod
    def from_json(j: dict, list_name: str) -> "Task":
        due = parse_iso(j.get("dueDate"))
        p = j.get("priority") or 0
        prio = 0 if p == 0 else 3 if p <= 4 else 2 if p == 5 else 1
        t = Task(
            id=str(j.get("externalId") or j.get("id") or ""),
            list=j.get("list") or list_name,
            title=(j.get("title") or "").strip(),
            due=due,
            has_time=bool(due and (due.hour or due.minute)),
            priority=prio,
            done=bool(j.get("isCompleted")),
            created=parse_iso(j.get("creationDate")),
            completed_at=parse_iso(j.get("completionDate")),
        )
        t.set_notes(j.get("notes") or "")
        return t

    @staticmethod
    def from_event_json(j: dict) -> "Task":
        start = parse_iso(j.get("start"))
        end = parse_iso(j.get("end"))
        t = Task(
            id=str(j.get("id") or ""),
            list=j.get("calendar") or "Calendar",
            title=(j.get("title") or "").strip(),
            due=start,
            end=end,
            has_time=not bool(j.get("isAllDay")),
            priority=0,
            done=False,
            is_event=True,
            loc=j.get("loc"),
            alarm_offset=j.get("alarm_offset"),
        )
        t.set_notes(j.get("notes") or "")
        return t

    @property
    def progress(self) -> Tuple[int, int]:
        return sum(1 for d, _ in self.subs if d), len(self.subs)


def fmt_due(t: Task) -> str:
    assert t.due
    return t.due.strftime("%Y-%m-%d %H:%M") if t.has_time else t.due.strftime("%Y-%m-%d")


def loc_text(loc: dict, long: bool = False) -> str:
    out = loc.get("title") or "location"
    if long:
        out += f" ({'on leaving' if loc.get('mode') == 'out' else 'on arrival'}, {int(loc.get('radius') or 0)} m)"
    elif loc.get("mode") == "out":
        out += " ↗"
    return out


def spec_line(t: Task) -> str:
    s = ("*" if t.is_event else "") + t.title
    if t.due:
        s += " @" + t.due.strftime("%d.%m.%Y")
        if t.has_time:
            time_part = t.due.strftime(" %H:%M")
            if t.end and t.is_event:
                time_part += f"-{t.end:%H:%M}"
            s += time_part
    if t.priority and not t.is_event:
        s += " " + "!" * t.priority
    if t.loc:
        places = load_places()
        key = next((k for k, p in places.items() if p.get("title") == t.loc.get("title")), None)
        s += " ~" + (key if key else f'"{t.loc.get("title", "")}"') + (":out" if t.loc.get("mode") == "out" else "")
    if t.alarm_offset:
        mins = abs(int(t.alarm_offset / 60))
        s += f" ^{mins}m"
    return s


# ───────────────────────────── dates & times ─────────────────────────────

TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.I)
TIME_RANGE_RE = re.compile(r"^(\d{1,2}(?::\d{2})?(?:am|pm)?)-(\d{1,2}(?::\d{2})?(?:am|pm)?)$", re.I)
REL_RE = re.compile(r"^\+?(\d+)(mo|min|mins|m|h|hr|hrs|d|w|wk)$", re.I)
CLEAR_WORDS = {"-", "0", "none", "x"}
WEEKDAY_KEYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "пн": 0, "пон": 0, "вт": 1, "вів": 1, "ср": 2, "сер": 2, "чт": 3, "чет": 3,
    "пт": 4, "пя": 4, "пʼя": 4, "п'я": 4, "сб": 5, "суб": 5, "нд": 6, "нед": 6,
}


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def parse_time(tok: str) -> Optional[Tuple[int, int]]:
    m = TIME_RE.match(tok.strip())
    if not m or not (m.group(2) or m.group(3)):
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap:
        if not 1 <= h <= 12:
            raise UserError(f"Invalid time '{tok}'")
        h = h % 12 + (12 if ap == "pm" else 0)
    if h > 23 or mi > 59:
        raise UserError(f"Invalid time '{tok}'")
    return h, mi


def parse_relative_seconds(tok: str) -> Optional[int]:
    m = REL_RE.match(tok)
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2).lower()
    if u in ("m", "min", "mins"):
        return n * 60
    if u in ("h", "hr", "hrs"):
        return n * 3600
    if u == "d":
        return n * 86400
    return None


def parse_date_word(tok: str, today: date) -> Optional[date]:
    t = tok.lower().rstrip(".,")
    if t in ("today", "tod", "сьогодні"):
        return today
    if t in ("tomorrow", "tom", "tmr", "завтра"):
        return today + timedelta(days=1)
    if t in ("післязавтра",):
        return today + timedelta(days=2)
    m = REL_RE.match(t)
    if m:
        n, u = int(m.group(1)), m.group(2).lower()
        if u == "d":
            return today + timedelta(days=n)
        if u in ("w", "wk"):
            return today + timedelta(weeks=n)
        if u == "mo":
            return add_months(today, n)
        return None
    try:
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", t)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.match(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?$", t)
        if m:
            d, mo = int(m.group(1)), int(m.group(2))
            if m.group(3):
                y = int(m.group(3))
                return date(y + 2000 if y < 100 else y, mo, d)
            cand = date(today.year, mo, d)
            return cand if cand >= today else date(today.year + 1, mo, d)
    except ValueError:
        raise UserError(f"No such date: '{tok}'")
    for key, wd in WEEKDAY_KEYS.items():
        if t == key or (len(t) > len(key) and t.startswith(key) and len(t) <= 12):
            return today + timedelta(days=(wd - today.weekday() - 1) % 7 + 1)
    return None


# ───────────────────────────── input syntax ─────────────────────────────

LOC_RE = re.compile(r'(?<!\S)~(?:"([^"]+)"|([^\s:"]+))(?::(in|out|arrive|leave))?(?!\S)')
ALARM_RE = re.compile(r'(?<!\S)\^([a-zA-Z0-9_\-+]+)(?!\S)')


@dataclass
class Spec:
    title: str = ""
    is_event: bool = False
    due_set: bool = False
    due: Optional[datetime] = None
    end: Optional[datetime] = None
    has_time: bool = False
    prio: Optional[int] = None
    list_name: Optional[str] = None
    notes: Optional[str] = None
    subs: List[str] = field(default_factory=list)
    loc_set: bool = False
    loc_query: Optional[str] = None
    loc_quoted: bool = False
    loc_mode: str = "in"
    alarm_set: bool = False
    alarm_query: Optional[str] = None


def match_name(q: str, names: List[str]) -> Optional[str]:
    if q.isdigit():
        n = int(q)
        return names[n - 1] if 1 <= n <= len(names) else None
    ql = q.lower()
    for l in names:
        if l.lower() == ql:
            return l
    for pred in (lambda l: l.lower().startswith(ql), lambda l: ql in l.lower()):
        hits = [l for l in names if pred(l)]
        if len(hits) == 1:
            return hits[0]
    return None


def parse_spec(text: str, rem_lists: List[str], cal_lists: List[str],
               now: Optional[datetime] = None, default_event: bool = False) -> Spec:
    """Parse one input line.

    `default_event` is the context default: True inside the calendar view (a plain line is an
    event), False in reminder views (a plain line is a task). `*` forces an event,
    `todo ` forces a task, whatever the context is.
    """
    now = now or datetime.now()
    today = now.date()
    spec = Spec()

    text = text.strip()
    if text.startswith("*"):
        spec.is_event = True
        text = text[1:].strip()
    elif text.lower().startswith("todo "):
        spec.is_event = False
        text = text[5:].strip()
    else:
        spec.is_event = default_event

    m = re.search(r"\s//\s*", text)
    if m:
        text, spec.notes = text[: m.start()], text[m.end():].strip()
    parts = re.split(r"\s+\+\s+", text)
    head, spec.subs = parts[0], [p.strip() for p in parts[1:] if p.strip()]

    # Extract ~location
    lm = None
    for lm in LOC_RE.finditer(head):
        pass
    if lm:
        spec.loc_set = True
        q = lm.group(1) or lm.group(2)
        spec.loc_quoted = bool(lm.group(1))
        spec.loc_query = None if q in ("-", "0") else q
        spec.loc_mode = "out" if (lm.group(3) or "") in ("out", "leave") else "in"
        head = LOC_RE.sub(" ", head)

    # Extract ^early-alarm
    am = None
    for am in ALARM_RE.finditer(head):
        pass
    if am:
        spec.alarm_set = True
        val = am.group(1)
        spec.alarm_query = None if val in ("-", "0", "none") else val
        head = ALARM_RE.sub(" ", head)

    toks, title, i = head.split(), [], 0
    all_known_lists = cal_lists if spec.is_event else rem_lists

    while i < len(toks):
        tk = toks[i]
        if tk.startswith("@"):
            arg = tk[1:]
            if not arg:
                i += 1
                if i >= len(toks):
                    raise UserError("Nothing after @")
                arg = toks[i]
            if arg.lower() in CLEAR_WORDS:
                spec.due_set, spec.due, spec.end, spec.has_time = True, None, None, False
            else:
                rel_sec = parse_relative_seconds(arg)
                if rel_sec and not any(arg.lower().endswith(x) for x in ("d", "w", "wk", "mo")):
                    spec.due = (now + timedelta(seconds=rel_sec)).replace(second=0, microsecond=0)
                    spec.has_time, spec.due_set = True, True
                    if spec.is_event:
                        spec.end = spec.due + timedelta(hours=1)
                else:
                    tm_start, tm_end = None, None
                    m_rng = TIME_RANGE_RE.match(arg)
                    if m_rng:
                        tm_start = parse_time(m_rng.group(1))
                        tm_end = parse_time(m_rng.group(2))
                        d = today
                    else:
                        tm_start = parse_time(arg)
                        d = None
                        if tm_start is None:
                            d = parse_date_word(arg, today)
                            if d is None:
                                raise UserError(f"Can't read date '{arg}'")
                            if i + 1 < len(toks):
                                next_tok = toks[i + 1]
                                m_rng2 = TIME_RANGE_RE.match(next_tok)
                                if m_rng2:
                                    i += 1
                                    tm_start = parse_time(m_rng2.group(1))
                                    tm_end = parse_time(m_rng2.group(2))
                                elif parse_time(next_tok):
                                    i += 1
                                    tm_start = parse_time(toks[i])

                    if d is None:
                        d = today
                        if tm_start and datetime.combine(d, time(*tm_start)) <= now:
                            d += timedelta(days=1)

                    if tm_start:
                        spec.due = datetime.combine(d, time(*tm_start))
                        spec.has_time = True
                        if tm_end:
                            spec.end = datetime.combine(d, time(*tm_end))
                        elif spec.is_event:
                            spec.end = spec.due + timedelta(hours=1)
                    else:
                        spec.due = datetime.combine(d, time())
                        spec.has_time = False
                        if spec.is_event:
                            spec.end = spec.due + timedelta(days=1)
                    spec.due_set = True
        elif re.fullmatch(r"!{1,3}", tk):
            spec.prio = len(tk)
        elif tk in ("!-", "!0"):
            spec.prio = 0
        elif tk.startswith("#") and len(tk) > 1:
            name = match_name(tk[1:], all_known_lists)
            if name:
                spec.list_name = name
            else:
                title.append(tk)
        else:
            title.append(tk)
        i += 1

    spec.title = " ".join(title)
    return spec


REF_RE = re.compile(r"^(?:\.\d+|\d+(?:\.\d+)?(?:-\d+)?)$")


def split_refs(rest: str) -> Tuple[List[str], str]:
    refs, rest = [], rest.strip()
    while rest:
        tok, _, tail = rest.partition(" ")
        clean = tok.rstrip(",")
        if REF_RE.match(clean):
            refs.append(clean)
            rest = tail.strip()
        else:
            break
    return refs, rest


def resolve_location(spec: Spec, old: Optional[dict] = None) -> Optional[dict]:
    if not spec.loc_query:
        return None
    places = load_places()
    key = match_place(spec.loc_query, places)
    if key:
        p = places[key]
        return {"title": p.get("title") or key, "lat": p["lat"], "lon": p["lon"],
                "radius": p.get("radius", DEFAULT_RADIUS), "mode": spec.loc_mode}
    if not spec.loc_quoted:
        known = ", ".join(places) or "none yet"
        raise UserError(f"Unknown place '{spec.loc_query}' (saved: {known}). "
                        f'Use ~"address, city" or: place add <name> <address>')
    if old and (old.get("title") or "").lower() == spec.loc_query.lower():
        return {**old, "mode": spec.loc_mode}
    g = geocode(spec.loc_query)
    return {"title": g.get("name") or spec.loc_query, "lat": g["lat"], "lon": g["lon"],
            "radius": DEFAULT_RADIUS, "mode": spec.loc_mode}


# ───────────────────────────── early alarms (^) ─────────────────────────────
#
#   ^15m            fixed: 15 minutes before the start
#   ^car            travel time by car,  NO hidden extra buffer
#   ^car+5m         travel time by car + exactly 5 minutes
#   ^walk / ^transit work the same way
#
# The buffer is always exactly what you type after '+' (0 if you type nothing).

TRAVEL_MODES = {"transit": "transit", "pt": "transit", "walk": "walk", "car": "car"}
TRAVEL_LABEL = {"transit": "transit", "walk": "walk", "car": "car"}


def parse_alarm_instruction(query: Optional[str]) -> Tuple[str, int]:
    """Returns (mode, buffer_seconds); mode is none | rel | walk | car | transit."""
    if not query:
        return ("none", 0)
    q = query.strip()
    if "+" in q:
        mode_part, buf_part = q.split("+", 1)
        mode = mode_part.lower()
        buf = parse_relative_seconds(buf_part)
        if buf is None:
            raise UserError(f"Invalid buffer '{buf_part}' in ^{query} (example: ^car+5m)")
        buf_sec = buf
    else:
        rel = parse_relative_seconds(q)
        if rel:
            return ("rel", rel)
        mode, buf_sec = q.lower(), 0
    if mode == "db":
        raise UserError("^db was removed — public transport now uses Apple Maps: ^transit or ^transit+5m")
    if mode in TRAVEL_MODES:
        return (TRAVEL_MODES[mode], buf_sec)
    raise UserError(f"Unknown alarm '^{query}'. Use: ^15m, ^1h, ^walk, ^car, ^transit, ^car+5m")


def compute_alarm_offset(query: str, target_loc: Optional[dict], target_time: Optional[datetime]) -> Tuple[int, str]:
    """Returns (negative offset in seconds, human-readable breakdown)."""
    mode, buf_sec = parse_alarm_instruction(query)
    if mode == "none":
        return 0, ""
    if mode == "rel":
        return -buf_sec, f"{buf_sec // 60}m before start"
    if not target_loc:
        raise UserError(f"^{query} requires a location (~place)")
    if not target_time:
        raise UserError(f"^{query} requires a start time (@HH:MM)")
    origin = load_places().get("home")
    if not origin:
        raise UserError("Travel calculation requires 'home' location: place add home <your address>")
    travel_sec, how = calculate_travel(mode, origin, target_loc, target_time)
    travel_min = math.ceil(travel_sec / 60)          # round the trip up to whole minutes
    total_sec = travel_min * 60 + buf_sec
    note = f"{TRAVEL_LABEL[mode]} {travel_min}m ({how})"
    if buf_sec:
        note += f" + buffer {buf_sec // 60}m"
    note += f" = {total_sec // 60}m before start"
    return -total_sec, note


# ───────────────────────────── backend (Reminders & Calendar) ─────────────────────────────


def fetch_rem_lists() -> List[str]:
    out = run("show-lists", "--format", "json").strip()
    try:
        data = json.loads(out)
        names = [x if isinstance(x, str) else (x.get("title") or x.get("name") or "") for x in data]
    except (json.JSONDecodeError, AttributeError):
        names = [l.strip() for l in run("show-lists").splitlines()]
    return [n for n in names if n] or ["Reminders"]


def fetch_cal_lists() -> Tuple[str, List[str]]:
    """Returns (default_calendar, all_calendars)."""
    try:
        data = json.loads(helper("cal-lists"))
        return data.get("default", "Calendar"), data.get("all", ["Calendar"])
    except (CLIError, json.JSONDecodeError):
        return "Calendar", ["Calendar"]


def fetch_reminders(list_name: str, completed: bool = False) -> List[Task]:
    args = ["show", list_name, "--format", "json"]
    if completed:
        args.append("--only-completed")
    out = run(*args).strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise CLIError("Could not parse JSON from reminders-cli")
    return [Task.from_json(j, list_name) for j in data]


def attach_rem_meta(tasks: List[Task]) -> None:
    if not tasks or not helper_ready():
        return
    try:
        d = json.loads(helper("rem-dump"))
    except (CLIError, UserError, json.JSONDecodeError):
        return
    for t in tasks:
        meta = d.get(t.id)
        if meta:
            t.loc = meta.get("loc")
            t.alarm_offset = meta.get("alarm_offset")


def fetch_calendar_events(start: datetime, end: datetime) -> List[Task]:
    iso_start = start.astimezone().isoformat()
    iso_end = end.astimezone().isoformat()
    out = helper("cal-dump", iso_start, iso_end)
    try:
        items = json.loads(out)
        return [Task.from_event_json(x) for x in items]
    except json.JSONDecodeError:
        return []


def safe_title(title: str) -> str:
    return title.lstrip("-– ") or title


def locate_reminder(t: Task, completed: bool = False) -> Task:
    items = fetch_reminders(t.list, completed)
    found = next((i for i in items if i.id == t.id), None)
    if not found:
        cands = [i for i in items if i.title == t.title or i.title == safe_title(t.title)]
        if cands:
            found = max(cands, key=lambda i: i.created or datetime.min)
    if not found:
        raise UserError("Reminder not found")
    return found


def add_raw_reminder(t: Task) -> str:
    args = ["add"]
    if t.notes:
        args.append("--notes=" + t.notes)
    if t.due:
        args.append("--due-date=" + fmt_due(t))
    if t.priority:
        args.append("--priority=" + PRIO_NAME[t.priority])
    run(*args, t.list, safe_title(t.title))
    fresh = locate_reminder(t)
    if t.loc:
        helper("rem-set-loc", fresh.id, str(t.loc["title"]), str(t.loc["lat"]), str(t.loc["lon"]),
               str(t.loc.get("radius") or DEFAULT_RADIUS), t.loc.get("mode") or "in")
    if t.alarm_offset:
        helper("rem-set-alarm", fresh.id, str(t.alarm_offset))
    return fresh.id


def save_raw_event(t: Task) -> str:
    iso = lambda d: d.astimezone().isoformat() if d else ""
    payload = {
        "id": t.id,
        "title": t.title,
        "calendar": t.list,
        "start": iso(t.due or datetime.now()),
        "end": iso(t.end or (t.due + timedelta(hours=1) if t.due else datetime.now() + timedelta(hours=1))),
        "isAllDay": not t.has_time,
        "notes": t.notes,
        "loc": t.loc,
        "alarm_offset": t.alarm_offset,
        "loc_clear": t.loc is None,
        "alarm_clear": t.alarm_offset is None,
    }
    out = helper("cal-save", json.dumps(payload))
    res = json.loads(out)
    return res.get("id", t.id)


# ───────────────────────────── help ─────────────────────────────


def build_help() -> str:
    h, d, r = BOLD, DIM, RESET
    return f"""
{h}doer — terminal tasks & calendar with travel-time alarms{r}

{h}CONTEXT — where does a new line go?{r}
  The prompt always shows where you are:  {h}@Work ›{r} = reminders list,  {h}cal@Home ›{r} = calendar "Home".

  Reminder views  (@list · today · week · all · search · done)
      title …            task   → current reminders list
      *title …           event  → default calendar
  Calendar view   (cal · cal@Name)
      title …            EVENT  → current calendar (default calendar if "all")
      todo title …       task   → current reminders list
  {h}#name{r} overrides the target:  #list for tasks, #calendar for events.

{h}ADD A TASK OR EVENT{r}
  {h}<title>{r} [@when] [!] [#target] [~place] [^early] [+ sub …] [// notes]

  pay rent @1.11 !!                              (task, in a reminders view)
  call mom @fri 6pm ^15m
  *Dentist @tomorrow 10:00-11:00 #Personal ^15m  (event, from anywhere)
  Team sync @today 12:00                         (inside cal@Work → event in Work)
  todo buy milk @tomorrow                        (task, from inside a calendar view)
  *Flight @fri 09:30 ~"BER Airport" ^transit+20m // terminal 1

  {h}@when{r}   date and/or time. Formats:
    @today  @tomorrow  @fri (next Friday)  @26.11  @26.11.2026  @2026-11-26  @3d  @2w  @1mo
    @14:00  @6pm  @6:30pm        time only: today, or tomorrow if already passed
    @fri 18:00  @26.11 9am       date + time
    @14:00-15:30                 time range (events; default length 1 h)
    @10m  @2h                    relative: from now
    @-                           remove date (tasks only)
    Events without a time become all-day events.
  {h}!{r}       priority (tasks): ! low  !! medium  !!! high     !- clears
  {h}#target{r} list or calendar by name, prefix or number: #work  #2
  {h}~place{r}  location alert (needs the place saved or an address):
    ~home                  alert on arrival            ~home:out  on leaving
    ~"Alexanderplatz 1"    any address (geocoded by Apple)
    ~-                     remove location
  {h}^early{r}  alarm before the start (needs @time):
    ^15m  ^1h              fixed time before start
    ^walk  ^car  ^transit  travel time from your saved place "home" to ~place, via Apple Maps
    ^car+5m  ^transit+10m  travel time + exactly that buffer (no hidden extras; ^car = pure travel time)
    ^-                     remove alarm
    After creating, tb prints the breakdown, e.g.  car 6m (traffic forecast) + buffer 5m = 11m before start
    Basis of the estimate: transit → real timetable arriving by the start time;
    car → traffic forecast for the time you will actually drive; walk → plain walking time.
    Travel time is rounded up to whole minutes and calculated once, when you save.
  {h}+ sub{r}   checklist items (tasks only):  Trip prep + passport + tickets
  {h}// notes{r} everything after // is the note

{h}NAVIGATE{r}
  3              open item 3 (detail card)             Enter  close / go back
  l              show lists & calendars                l work  switch to list or calendar
  t · w · a      today · next 7 days · all reminders
  /query         search reminders (title, notes, list)
  done           completed reminders of the current list
  r              refresh lists                         q  quit        ?  this help

  {h}Calendar view{r}
  cal            open calendar view, keeps the current calendar and date
  cal all        all calendars (clears the calendar filter)
  cal Home       only calendar Home (name, prefix or number)
  cal month      next 30 days (default window: yesterday … +30 days)
  cal 26.11      jump to a date: that day + 7 days. Also: cal fri · cal 2w · cal tomorrow
  {h}@26.11{r}         same jump, shortcut inside the calendar view; the calendar filter stays
  @-             back to the default window

{h}ACT{r}   (numbers refer to the current screen: 1 3 5 · 2-4 · subtask 3.2 · inside a card: .2)
  x 3            toggle done (tasks; subtask: x 3.2)
  e 3 <spec>     edit in place, using the same syntax as adding: e 3 @fri 10:00 !!   (events keep being events)
  e 3            open in $EDITOR (first line uses the same syntax, rest = notes + checklist)
  + 3 sub + sub  add subtasks
  n 3 text       append to notes (n 3 - clears)
  m 3 #work      move to another list / calendar
  rm 3           delete (also: rm 3.2 for a subtask)
  u              undo the last action (up to 30)

{h}PLACES{r}
  place                         list saved places
  place add home Ulmenstr. 1    save a place (the place named "home" is the start of every ^walk/^car/^transit)
  place rm home                 forget a place

{h}ONE-SHOT MODE{r}
  tb "pay rent @1.11"           run a single command and print the result
  tb cal 26.11                  (any command from above works)

{h}ENVIRONMENT{r}
  TB_DEFAULT_LIST   reminders list opened at start (default: Reminders)
  EDITOR / VISUAL   editor for 'e 3'          NO_COLOR   disable colours
"""


def show_text(text: str, interactive: bool) -> None:
    if interactive and sys.stdout.isatty() and shutil.which("less"):
        try:
            subprocess.run(["less", "-R", "-F", "-X"], input=text, text=True)
            return
        except OSError:
            pass
    print(text)


# ───────────────────────────── app ─────────────────────────────


def sort_key(t: Task):
    return (t.due is None, t.due or datetime.max, -t.priority)


class App:
    def __init__(self, interactive: bool = True):
        self.interactive = interactive
        self.rem_lists: List[str] = []
        self.cal_lists: List[str] = []
        self.default_cal: str = "Calendar"
        self.cur_list = ""
        self.cur_cal: Optional[str] = None
        self.cal_target_date: Optional[date] = None
        self.cal_days: int = 30
        self.mode = "list"  # list | done | today | week | all | cal | search | lists
        self.query = ""
        self.items: List[Task] = []
        self.detail_id: Optional[str] = None
        self.msg = ""
        self.warnings: List[str] = []
        self.undo_stack: List[Tuple[str, Callable[[], None]]] = []

    def start(self) -> None:
        self.rem_lists = fetch_rem_lists()
        self.default_cal, self.cal_lists = fetch_cal_lists()
        pref = os.environ.get("TB_DEFAULT_LIST", "Reminders")
        self.cur_list = match_name(pref, self.rem_lists) or self.rem_lists[0]

    def load(self) -> None:
        self.warnings = []
        today = date.today()

        if self.mode in ("list", "done"):
            items = fetch_reminders(self.cur_list, self.mode == "done")
            if self.mode == "done":
                items.sort(key=lambda t: t.completed_at or datetime.min, reverse=True)
                items = items[:35]
            else:
                items.sort(key=sort_key)
            attach_rem_meta(items)
            self.items = items
            return

        if self.mode == "cal":
            if self.cal_target_date:
                start = datetime.combine(self.cal_target_date, time())
                end = datetime.combine(self.cal_target_date + timedelta(days=7), time())
            else:
                start = datetime.combine(today - timedelta(days=1), time())
                end = datetime.combine(today + timedelta(days=self.cal_days), time())
            try:
                events = fetch_calendar_events(start, end)
                if self.cur_cal:
                    events = [e for e in events if e.list.lower() == self.cur_cal.lower()]
                events.sort(key=lambda e: e.due or datetime.max)
                self.items = events
            except CLIError as e:
                self.warnings.append(str(e))
                self.items = []
            return

        if self.mode == "lists":
            self.items = []
            return

        with ThreadPoolExecutor(max_workers=6) as ex:
            rem_groups = list(ex.map(lambda name: fetch_reminders(name), self.rem_lists))
        all_rems = [t for g in rem_groups for t in g]
        attach_rem_meta(all_rems)

        if self.mode in ("today", "week"):
            days = 0 if self.mode == "today" else 7
            lim = today + timedelta(days=days)
            matching_rems = [t for t in all_rems if t.due and t.due.date() <= lim]
            cal_start = datetime.combine(today, time())
            cal_end = datetime.combine(lim + timedelta(days=1), time())
            try:
                cal_events = fetch_calendar_events(cal_start, cal_end)
            except CLIError as e:
                self.warnings.append(str(e))
                cal_events = []
            combined = matching_rems + cal_events
            combined.sort(key=lambda t: (t.due or datetime.max, t.is_event, -t.priority))
            self.items = combined
        else:  # all | search
            q = self.query.lower()
            out: List[Task] = []
            for t in sorted(all_rems, key=sort_key):
                if self.mode == "search":
                    if q in t.title.lower() or q in t.notes.lower() or q in t.list.lower():
                        out.append(t)
                else:
                    out.append(t)
            self.items = out

    # ── prompt & rendering ──

    def prompt(self) -> str:
        cal_tag = f"@{self.cur_cal}" if self.cur_cal else ""
        base = {
            "list": f"{MAGENTA}{BOLD}@{self.cur_list}{RESET}",
            "done": f"{MAGENTA}{BOLD}@{self.cur_list}{RESET}{DIM} done{RESET}",
            "today": f"{YELLOW}{BOLD}today{RESET}",
            "week": f"{YELLOW}{BOLD}week{RESET}",
            "cal": f"{BLUE}{BOLD}cal{cal_tag}{RESET}",
            "all": f"{CYAN}{BOLD}all{RESET}",
            "search": f"{CYAN}{BOLD}/{self.query}{RESET}",
            "lists": f"{CYAN}{BOLD}lists{RESET}",
        }[self.mode]
        if self.detail_id:
            idx = next((i for i, t in enumerate(self.items, 1) if t.id == self.detail_id), None)
            if idx:
                base += f"{DIM} #{idx}{RESET}"
        return f"{base} {BOLD}›{RESET} "

    def due_label(self, t: Task) -> Tuple[str, str]:
        if not t.due:
            return "", ""
        today, dd = date.today(), t.due.date()
        delta = (dd - today).days
        if delta == 0:
            base = "today"
        elif delta == 1:
            base = "tomorrow"
        elif delta == -1:
            base = "yesterday"
        elif 1 < delta < 7:
            base = f"{WD_SHORT[dd.weekday()]} {dd:%d.%m}"
        else:
            base = f"{dd:%d.%m}" + (f".{dd:%y}" if dd.year != today.year else "")
            if delta < -1:
                base += f" ({delta}d)"

        if t.has_time:
            time_part = f"{t.due:%H:%M}"
            if t.end and t.is_event:
                time_part += f"-{t.end:%H:%M}"
            base += f" {time_part}"

        overdue = (t.due < datetime.now()) if t.has_time else (dd < today)
        color = RED if overdue and not t.is_event else YELLOW if delta == 0 else DIM
        return base, color

    def task_line(self, n: int, t: Task, width: int) -> str:
        if t.is_event:
            box = f"{BLUE}📅{RESET}"
        else:
            box = f"{GREEN}[x]{RESET}" if t.done else f"{DIM}[ ]{RESET}"

        prio = f"{({1: DIM, 2: YELLOW, 3: RED}.get(t.priority, ''))}{'!' * t.priority}{RESET} " if t.priority else ""
        meta = ""
        if t.subs:
            d, tot = t.progress
            meta += f" {DIM}[{d}/{tot}]{RESET}"
        if t.desc:
            meta += f" {DIM}✎{RESET}"
        if t.loc:
            short = loc_text(t.loc)
            meta += f" {DIM}⌖ {short if len(short) <= 15 else short[:14] + '…'}{RESET}"
        if t.alarm_offset:
            mins = abs(int(t.alarm_offset / 60))
            if t.due:
                alarm_time = t.due + timedelta(seconds=t.alarm_offset)
                meta += f" {YELLOW}⏰ {alarm_time:%H:%M} (-{mins}m){RESET}"
            else:
                meta += f" {YELLOW}⏰ -{mins}m{RESET}"

        label, color = self.due_label(t)
        right = f"{color}{label}{RESET}" if label else ""
        title = f"{DIM}{t.title}{RESET}" if t.done else t.title
        left_fixed = f"  {CYAN}{n:>3}.{RESET} {box} {prio}"
        room = width - vlen(left_fixed) - vlen(meta) - vlen(right) - 2
        if len(t.title) > room > 3:
            title = t.title[: room - 1] + "…"
        left = f"{left_fixed}{title}{meta}"
        pad = max(2, width - vlen(left) - vlen(right) - 1)
        return left + (" " * pad + right if right else "")

    def group_of(self, t: Task) -> Optional[Tuple[str, str]]:
        if self.mode in ("today", "week", "cal") and t.due:
            delta = (t.due.date() - date.today()).days
            if delta < 0:
                return "Overdue", RED
            if delta == 0:
                return "Today", YELLOW
            if delta == 1:
                return "Tomorrow", BOLD
            return f"{WD_FULL[t.due.weekday()]} {t.due:%d.%m}", BOLD
        if self.mode in ("all", "search"):
            return f"@{t.list}", MAGENTA
        return None

    def render(self) -> None:
        width = max(44, min(shutil.get_terminal_size((80, 24)).columns, 105))
        out: List[str] = []
        if self.interactive and sys.stdout.isatty():
            out.append("\033[2J\033[H")
            b = get_banner_text()
            if b:
                out.append(b)
        out.append("")

        tabs = []
        for i, l in enumerate(self.rem_lists[:5], 1):
            on = self.mode in ("list", "done") and l == self.cur_list
            tabs.append(f"{MAGENTA}{BOLD}{i} {l}{RESET}" if on else f"{DIM}{i} {l}{RESET}")
        cal_active = self.mode == "cal"
        tabs.append(f"{BLUE}{BOLD}cal{RESET}" if cal_active else f"{DIM}cal{RESET}")
        out.append("  " + f"{DIM} · {RESET}".join(tabs))
        out.append("")

        task = next((t for t in self.items if t.id == self.detail_id), None) if self.detail_id else None
        if self.detail_id and not task:
            self.detail_id = None

        if task:
            out += self.render_detail(task)
        else:
            cal_info = f"@{self.cur_cal}" if self.cur_cal else "All"
            dt_info = f"{self.cal_target_date:%d.%m.%Y} +7d" if self.cal_target_date else f"next {self.cal_days}d"
            title_text = {
                "list": f"@{self.cur_list}",
                "done": f"Done · @{self.cur_list}",
                "today": "Today (Schedule & Tasks)",
                "week": "Next 7 Days (Schedule & Tasks)",
                "cal": f"Calendar · {cal_info} ({dt_info})",
                "all": "All Reminders",
                "search": f"Search · {self.query}",
                "lists": "Lists & Calendars",
            }[self.mode]
            out.append(f"  {MAGENTA if self.mode != 'cal' else BLUE}{BOLD}{title_text}{RESET}\n")

            if self.mode == "lists":
                out.append(f"  {BOLD}Reminders Lists:{RESET}")
                for i, l in enumerate(self.rem_lists, 1):
                    out.append(f"  {CYAN}{i:>3}.{RESET} {l}")
                out.append(f"\n  {BOLD}Apple Calendars:{RESET}  {DIM}(default: {self.default_cal}){RESET}")
                for i, l in enumerate(self.cal_lists, 1):
                    star = " ★" if l == self.default_cal else ""
                    out.append(f"  {BLUE}{i:>3}.{RESET} 📅 {l}{star}")
            elif not self.items:
                out.append(f"    {DIM}Nothing scheduled.{RESET}")
            else:
                last_grp = None
                for n, t in enumerate(self.items, 1):
                    g = self.group_of(t)
                    if g and g[0] != last_grp:
                        if last_grp is not None:
                            out.append("")
                        out.append(f"  {g[1]}{g[0]}{RESET}")
                        last_grp = g[0]
                    out.append(self.task_line(n, t, width))
                out.append("")
                open_cnt = sum(1 for t in self.items if not t.done and not t.is_event)
                ev_cnt = sum(1 for t in self.items if t.is_event)
                status = []
                if open_cnt:
                    status.append(f"{open_cnt} tasks")
                if ev_cnt:
                    status.append(f"{ev_cnt} events")
                out.append(f"  {DIM}{' · '.join(status) or len(self.items)}{RESET}")

        for w in self.warnings:
            out.append(f"  {YELLOW}⚠ {w}{RESET}")
        if self.msg:
            out.append(f"\n  {GREEN}›{RESET} {self.msg}")
        out.append("")
        print("\n".join(out))

    def render_detail(self, t: Task) -> List[str]:
        n = self.items.index(t) + 1
        icon = f"{BLUE}📅 Event:{RESET}" if t.is_event else (f"{GREEN}[x]{RESET}" if t.done else "[ ]")
        out = [f"  {CYAN}{n}.{RESET} {icon} {BOLD}{t.title}{RESET}", ""]
        label, color = self.due_label(t)
        if label:
            out.append(f"      {DIM}{'time' if t.is_event else 'due'}{RESET}        {color}{label}{RESET}")
        if t.priority:
            out.append(f"      {DIM}priority{RESET}   {'!' * t.priority}")
        if t.loc:
            out.append(f"      {DIM}place{RESET}      ⌖ {loc_text(t.loc, long=True)}")
        if t.alarm_offset:
            mins = abs(int(t.alarm_offset / 60))
            at_time = f" ({t.due + timedelta(seconds=t.alarm_offset):%H:%M})" if t.due else ""
            out.append(f"      {DIM}alarm{RESET}      ⏰ {mins} min before{at_time}")
        out.append(f"      {DIM}{'calendar' if t.is_event else 'list'}{RESET}      @{t.list}")
        if t.desc:
            out.append("")
            out += [f"      {line}" for line in t.desc.splitlines()]
        if t.subs:
            out.append("")
            for k, (dn, text) in enumerate(t.subs, 1):
                box = f"{GREEN}[x]{RESET}" if dn else f"{DIM}[ ]{RESET}"
                txt = f"{DIM}{text}{RESET}" if dn else text
                out.append(f"      {CYAN}{n}.{k}{RESET} {box} {txt}")
        out.append(f"\n  {DIM}e edit · rm delete · m move · Enter back{RESET}")
        return out

    # ── mutations ──

    def push_undo(self, label: str, fns: List[Callable[[], None]]) -> None:
        def run_all():
            for f in reversed(fns):
                f()
        self.undo_stack.append((label, run_all))
        del self.undo_stack[:-30]

    def undo_last(self) -> None:
        if not self.undo_stack:
            raise UserError("Nothing to undo")
        label, fn = self.undo_stack.pop()
        fn()
        self.msg = f"Undone: {label}"

    def update_task(self, old: Task, new: Task) -> None:
        if old.is_event:
            save_raw_event(new)
            return

        opts: List[str] = []
        if new.notes != old.notes:
            opts.append("--notes=" + new.notes)
        due_changed = (new.due, new.has_time) != (old.due, old.has_time)
        prio_changed = new.priority != old.priority
        core = new.title != old.title or bool(opts) or due_changed or prio_changed
        if core:
            done = False
            if not (due_changed and new.due is None):
                if due_changed:
                    opts.append("--due-date=" + fmt_due(new))
                if prio_changed:
                    opts.append("--priority=" + PRIO_NAME[new.priority])
                try:
                    run("edit", *opts, old.list, old.id, safe_title(new.title))
                    done = True
                except CLIError:
                    pass
            if not done:
                add_raw_reminder(new)
                run("delete", old.list, old.id)
                if self.detail_id == old.id:
                    self.detail_id = locate_reminder(new).id

        cur_id = locate_reminder(new).id
        if new.loc != old.loc:
            if new.loc:
                helper("rem-set-loc", cur_id, str(new.loc["title"]), str(new.loc["lat"]), str(new.loc["lon"]),
                       str(new.loc.get("radius") or DEFAULT_RADIUS), new.loc.get("mode") or "in")
            else:
                helper("rem-clear-loc", cur_id)
        if new.alarm_offset != old.alarm_offset:
            helper("rem-set-alarm", cur_id, str(new.alarm_offset or 0))

    def cmd_add(self, text: str) -> None:
        # Context rule: inside the calendar view a plain line is an event.
        spec = parse_spec(text, self.rem_lists, self.cal_lists, default_event=(self.mode == "cal"))
        if not spec.title:
            raise UserError("Empty title")

        loc = resolve_location(spec)
        alarm_sec, alarm_note = None, ""
        if spec.alarm_set and spec.alarm_query:
            alarm_sec, alarm_note = compute_alarm_offset(
                spec.alarm_query, loc, spec.due if spec.has_time else None)

        if spec.is_event:
            target_cal = spec.list_name or self.cur_cal or self.default_cal or self.cal_lists[0]
            if spec.due:
                due, end, has_time = spec.due, spec.end or (spec.due + timedelta(hours=1)), spec.has_time
            else:  # no date given → all-day event today
                due = datetime.combine(date.today(), time())
                end, has_time = due + timedelta(days=1), False
            t = Task(
                id="", list=target_cal, title=spec.title, desc=spec.notes or "",
                due=due, end=end, has_time=has_time, is_event=True, loc=loc, alarm_offset=alarm_sec,
            )
            ev_id = save_raw_event(t)
            t.id = ev_id
            self.push_undo(f"creation of event '{t.title}'", [lambda: helper("cal-delete", ev_id)])
            self.msg = f"Created event '{t.title}' in 📅 {target_cal}"
        else:
            target_list = spec.list_name or self.cur_list
            t = Task(
                id="", list=target_list, title=spec.title, desc=spec.notes or "",
                subs=[[False, s] for s in spec.subs], due=spec.due, has_time=spec.has_time,
                priority=spec.prio or 0, loc=loc, alarm_offset=alarm_sec,
            )
            r_id = add_raw_reminder(t)
            self.push_undo(f"creation of task '{t.title}'", [lambda: run("delete", target_list, r_id)])
            self.msg = f"Created '{t.title}' in @{target_list}"

        if alarm_sec and t.due:
            leave = t.due + timedelta(seconds=alarm_sec)
            self.msg += f"\n    ⏰ {alarm_note} → {leave:%H:%M}"

    def cmd_edit(self, pairs: List[Tuple[Task, Optional[int]]], tail: str) -> None:
        if any(k is not None for _, k in pairs):
            if len(pairs) != 1 or not tail:
                raise UserError("Usage: e <num>.<sub_num> new text")
            t, k = pairs[0]
            if t.is_event:
                raise UserError("Calendar events do not have checklists")
            new = t.copy()
            new.subs[k][1] = tail
            self.update_task(t, new)
            self.push_undo(f"subtask edit in '{t.title}'", [lambda: self.update_task(locate_reminder(new), t)])
            self.msg = "Updated subtask"
            return

        if not tail:
            if len(pairs) != 1:
                raise UserError("Select one item to open in editor")
            return self.editor_edit(pairs[0][0])

        # Each item is parsed in its own kind (an event stays an event, a task stays a task).
        probe = parse_spec(tail, self.rem_lists, self.cal_lists, default_event=pairs[0][0].is_event)
        if probe.list_name:
            raise UserError("To move to another list or calendar use: m <num> #target")
        if probe.title and len(pairs) > 1:
            raise UserError("Cannot set title for multiple items")

        fns, names, notes = [], [], []
        for t, _ in pairs:
            spec = parse_spec(tail, self.rem_lists, self.cal_lists, default_event=t.is_event)
            new = t.copy()
            if spec.title:
                new.title = spec.title
            if spec.due_set:
                if t.is_event and spec.due is None:
                    raise UserError("An event must have a date (use rm to delete it)")
                new.due, new.end, new.has_time = spec.due, spec.end, spec.has_time
            if spec.prio is not None and not t.is_event:
                new.priority = spec.prio
            if spec.notes is not None:
                new.desc = spec.notes
            if spec.loc_set:
                new.loc = resolve_location(spec, t.loc) if spec.loc_query else None
            if spec.alarm_set:
                if spec.alarm_query:
                    target_loc = new.loc or t.loc
                    due = new.due or t.due
                    has_time = new.has_time if new.due else t.has_time
                    new.alarm_offset, note = compute_alarm_offset(
                        spec.alarm_query, target_loc, due if has_time else None)
                    if note:
                        notes.append(note)
                else:
                    new.alarm_offset = None
            if not t.is_event and spec.subs:
                new.subs += [[False, s] for s in spec.subs]

            self.update_task(t, new)
            if t.is_event:
                fns.append(lambda n=new, old=t: self.update_task(n, old))
            else:
                fns.append(lambda n=new, old=t: self.update_task(locate_reminder(n), old))
            names.append(f"'{new.title}'")

        self.push_undo("edit of " + ", ".join(names), fns)
        self.msg = "Updated " + ", ".join(names)
        if notes:
            self.msg += "\n    ⏰ " + "; ".join(notes)

    def editor_edit(self, t: Task) -> None:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(spec_line(t) + "\n\n" + t.notes + "\n")
            path = f.name
        try:
            subprocess.call(shlex.split(editor) + [path])
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        finally:
            os.unlink(path)

        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines:
            raise UserError("Empty file — nothing changed")

        spec = parse_spec(lines[0], self.rem_lists, self.cal_lists, default_event=t.is_event)
        if not spec.title:
            raise UserError("Empty title")

        new = t.copy()
        new.title = spec.title
        if spec.due_set:
            if t.is_event and spec.due is None:
                raise UserError("An event must have a date")
            new.due, new.end, new.has_time = spec.due, spec.end, spec.has_time
        if not t.is_event:
            new.priority = spec.prio or 0
        if spec.loc_set:
            new.loc = resolve_location(spec, t.loc) if spec.loc_query else None
        note = ""
        if spec.alarm_set:
            if spec.alarm_query:
                target_loc = new.loc or t.loc
                due = new.due or t.due
                has_time = new.has_time if new.due else t.has_time
                new.alarm_offset, note = compute_alarm_offset(
                    spec.alarm_query, target_loc, due if has_time else None)
            else:
                new.alarm_offset = None

        new.set_notes("\n".join(lines[1:]))
        self.update_task(t, new)
        self.msg = f"Saved '{new.title}'" + (f"\n    ⏰ {note}" if note else "")

    def cmd_complete(self, pairs) -> None:
        fns, names = [], []
        for t, k in pairs:
            if t.is_event:
                continue
            if k is not None:
                new = t.copy()
                new.subs[k][0] = not new.subs[k][0]
                self.update_task(t, new)
                fns.append(lambda n=new, old=t: self.update_task(locate_reminder(n), old))
                names.append(f"subtask of '{t.title}'")
            else:
                run("uncomplete" if t.done else "complete", t.list, t.id)
                fns.append(lambda item=t: run("complete" if item.done else "uncomplete", item.list, item.id))
                names.append(f"'{t.title}'")
        if not names:
            raise UserError("Calendar events can't be completed (use rm to delete)")
        self.push_undo("complete toggle", fns)
        self.msg = "Updated " + ", ".join(names)

    def cmd_delete(self, pairs) -> None:
        fns, names = [], []
        for t, k in pairs:
            if k is not None and not t.is_event:
                new = t.copy()
                del new.subs[k]
                self.update_task(t, new)
                fns.append(lambda n=new, old=t: self.update_task(locate_reminder(n), old))
                names.append(f"subtask of '{t.title}'")
            else:
                if t.is_event:
                    helper("cal-delete", t.id)
                    fns.append(lambda item=t: save_raw_event(item))
                else:
                    run("delete", t.list, t.id)
                    fns.append(lambda item=t: add_raw_reminder(item))
                names.append(f"'{t.title}'")
                if self.detail_id == t.id:
                    self.detail_id = None
        self.push_undo("deletion", fns)
        self.msg = "Deleted " + ", ".join(names)

    def cmd_move(self, rest: str, in_detail: bool) -> None:
        toks = rest.split()
        target_tok = toks[-1]
        ref_toks = [x.rstrip(",") for x in toks[:-1]]
        if not ref_toks and in_detail:
            pairs = [(self.detail_task(), None)]
        elif ref_toks and all(REF_RE.match(x) for x in ref_toks):
            pairs = self.resolve(ref_toks)
        else:
            raise UserError("Usage: m <numbers> <#list|#cal>   e.g. m 3 #Work")
        if any(k is not None for _, k in pairs):
            raise UserError("Only whole items can be moved")

        for t, _ in pairs:
            if t.is_event:
                target_cal = match_name(target_tok.lstrip("#"), self.cal_lists)
                if not target_cal:
                    raise UserError(f"No calendar '{target_tok}'")
                new = replace(t, list=target_cal)
                save_raw_event(new)
                self.push_undo(f"move event '{t.title}'", [lambda old=t: save_raw_event(old)])
            else:
                target_list = match_name(target_tok.lstrip("#"), self.rem_lists)
                if not target_list:
                    raise UserError(f"No reminders list '{target_tok}'")
                new = replace(t, list=target_list)
                add_raw_reminder(new)
                run("delete", t.list, t.id)

                def back(old=t, n=new):
                    cur = locate_reminder(n)
                    run("delete", cur.list, cur.id)
                    add_raw_reminder(old)

                self.push_undo(f"move '{t.title}'", [back])

        self.msg = f"Moved {len(pairs)} items → @{target_tok.lstrip('#')}"

    def cmd_place(self, rest: str) -> None:
        places = load_places()
        sub, _, arg = rest.partition(" ")
        sub, arg = sub.lower(), arg.strip()
        if not sub:
            if not places:
                self.msg = "No saved places. Run: place add home <address>"
                return
            self.msg = "Places: " + "; ".join(f"~{k} ({p.get('address') or p.get('title')})" for k, p in places.items())
            return
        if sub == "add":
            name, _, addr = arg.partition(" ")
            if not name or not addr.strip():
                raise UserError("Usage: place add <name> <address>")
            g = geocode(addr.strip())
            places[name.lower()] = {
                "title": name[:1].upper() + name[1:],
                "address": g.get("address") or addr.strip(),
                "lat": g["lat"], "lon": g["lon"], "radius": DEFAULT_RADIUS
            }
            save_places(places)
            self.msg = f"Saved place ~{name.lower()} → {g.get('address') or addr.strip()}"
        elif sub in ("rm", "delete"):
            key = match_place(arg, places)
            if not key:
                raise UserError(f"No saved place '{arg}'")
            del places[key]
            save_places(places)
            self.msg = f"Removed ~{key}"
        else:
            raise UserError("Usage: place [add <name> <address> | rm <name>]")

    # ── calendar navigation ──

    def show_cal_date(self, d: date) -> None:
        """Show `d` and the following 7 days; the selected calendar (if any) is kept."""
        self.mode, self.cal_target_date, self.detail_id = "cal", d, None
        self.msg = f"{self.cur_cal or 'All calendars'}: {d:%d.%m.%Y} + 7 days"

    def jump_to_date(self, arg: str) -> None:
        """Bare '@<date>' line: only meaningful inside the calendar view."""
        if self.mode != "cal":
            raise UserError(f"'@{arg}' jumps within the calendar view. Use: cal {arg}")
        if arg.lower() in CLEAR_WORDS:
            self.cal_target_date, self.detail_id = None, None
            self.msg = f"{self.cur_cal or 'All calendars'}: default window"
            return
        d = parse_date_word(arg, date.today())
        if d is None:
            raise UserError(f"Can't read date '{arg}'")
        self.show_cal_date(d)

    # ── references & handler ──

    def detail_task(self) -> Task:
        t = next((x for x in self.items if x.id == self.detail_id), None)
        if not t:
            raise UserError("No item is open")
        return t

    def _item(self, n: int) -> Task:
        if not 1 <= n <= len(self.items):
            have = f"1-{len(self.items)}" if self.items else "the list is empty"
            raise UserError(f"No item {n} on this screen ({have})")
        return self.items[n - 1]

    @staticmethod
    def _sub(t: Task, k: int) -> int:
        if t.is_event:
            raise UserError("Calendar events have no subtasks")
        if not 1 <= k <= len(t.subs):
            raise UserError(f"'{t.title}' has no subtask {k} ({len(t.subs)} total)")
        return k - 1

    def resolve(self, refs: List[str]) -> List[Tuple[Task, Optional[int]]]:
        """Turn references (3, 2-4, 3.2, .2) into (task, subtask_index) pairs.
        Every index is validated against what is on screen right now."""
        out = []
        for r in refs:
            if r.startswith("."):
                cur = next((t for t in self.items if t.id == self.detail_id), None)
                if not cur:
                    raise UserError("Open a task first")
                out.append((cur, self._sub(cur, int(r[1:]))))
            elif "." in r:
                n, k = r.split(".")
                t = self._item(int(n))
                out.append((t, self._sub(t, int(k))))
            elif "-" in r:
                a, b = sorted(map(int, r.split("-")))
                out += [(self._item(n), None) for n in range(a, b + 1)]
            else:
                out.append((self._item(int(r)), None))
        return out

    def handle(self, line: str) -> None:
        line = line.strip()
        self.msg = ""
        if not line:
            self.detail_id = None if self.detail_id else self.detail_id
            return

        low = line.lower()
        word, _, rest = line.partition(" ")
        cmd, rest = word.lower(), rest.strip()

        if low in ("?", "h", "help"):
            show_text(build_help(), self.interactive)
            return
        if low in ("r", "refresh"):
            self.rem_lists = fetch_rem_lists()
            self.default_cal, self.cal_lists = fetch_cal_lists()
            self.msg = "Refreshed"
            return
        if low in ("u", "undo"):
            return self.undo_last()
        if low in ("t", "today", "w", "week", "a", "all", "done"):
            self.mode = {"t": "today", "w": "week", "a": "all"}.get(low, low)
            self.detail_id = None
            return
        if cmd in ("cal", "calendar"):
            self.detail_id = None
            if not rest:  # open the calendar view, keep calendar filter & date
                self.mode = "cal"
                self.msg = f"Calendar: {self.cur_cal or 'all calendars'}"
                return
            if rest.lower() in ("a", "all", "*"):
                self.mode, self.cur_cal, self.cal_target_date = "cal", None, None
                self.msg = "All calendars"
                return

            tok = rest.lstrip("@#")
            exact = next((c for c in self.cal_lists if c.lower() == tok.lower()), None)
            if exact:  # an exact calendar name always beats date words like "sun…"
                self.mode, self.cur_cal = "cal", exact
                self.msg = f"Calendar: {exact}"
                return

            if tok.lower() in ("m", "month", "1mo", "місяць"):
                self.cal_days = 31
                self.cal_target_date = None
                self.mode = "cal"
                self.msg = "Calendar: next 30 days"
                return

            d = parse_date_word(tok, date.today())
            if d:
                self.show_cal_date(d)
                return

            matched = match_name(tok, self.cal_lists)
            if matched:
                self.mode, self.cur_cal = "cal", matched
                self.msg = f"Calendar: {matched}"
            else:
                raise UserError(f"No calendar or date '{rest}'. Try: cal 2w, cal 26.11, cal Work, cal all")
            return
        if re.fullmatch(r"@\S+", line):  # bare "@26.11": date jump
            return self.jump_to_date(line[1:])
        if cmd in ("l", "lists"):
            if not rest:
                self.mode, self.detail_id = "lists", None
            else:
                matched_rem = match_name(rest, self.rem_lists)
                if matched_rem:
                    self.cur_list, self.mode, self.detail_id = matched_rem, "list", None
                else:
                    matched_cal = match_name(rest, self.cal_lists)
                    if matched_cal:
                        self.cur_cal, self.mode, self.detail_id = matched_cal, "cal", None
                    else:
                        raise UserError(f"No list or calendar '{rest}'")
            return
        if line.startswith("/") and len(line) > 1:
            self.mode, self.query, self.detail_id = "search", line[1:].strip(), None
            return
        if cmd in ("place", "places"):
            return self.cmd_place(rest)

        toks = line.split()
        if all(REF_RE.match(t.rstrip(",")) for t in toks):
            pairs = self.resolve([toks[0].rstrip(",")])
            self.detail_id = pairs[0][0].id
            return

        refs, tail = split_refs(rest)
        in_detail = bool(self.detail_id)

        if cmd in ("x", "c", "done") and (refs or (not rest and in_detail)):
            pairs = self.resolve(refs) if refs else [(self.detail_task(), None)]
            return self.cmd_complete(pairs)
        if cmd in ("rm", "delete") and (refs or (not rest and in_detail)):
            pairs = self.resolve(refs) if refs else [(self.detail_task(), None)]
            return self.cmd_delete(pairs)
        if cmd in ("e", "edit") and (refs or in_detail):
            pairs = self.resolve(refs) if refs else [(self.detail_task(), None)]
            return self.cmd_edit(pairs, tail if refs else rest)
        if cmd in ("m", "mv", "move") and rest:
            return self.cmd_move(rest, in_detail)
        if cmd == "+" and rest and (refs or in_detail):
            t = self.resolve(refs[:1])[0][0] if refs else self.detail_task()
            if t.is_event:
                raise UserError("Checklists not supported on calendar events")
            new = t.copy()
            new.subs += [[False, s.strip()] for s in re.split(r"\s+\+\s+", tail if refs else rest) if s.strip()]
            self.update_task(t, new)
            self.msg = f"Added subtasks to '{t.title}'"
            return
        if cmd in ("n", "note") and (refs or (rest and in_detail)):
            t = self.resolve(refs[:1])[0][0] if refs else self.detail_task()
            new = t.copy()
            txt = tail if refs else rest
            new.desc = "" if txt == "-" else (t.desc + "\n" + txt).strip("\n")
            self.update_task(t, new)
            self.msg = "Updated note"
            return

        # Default: add a task or an event (decided by the current context, see parse_spec)
        self.cmd_add(line)


# ───────────────────────────── entry point ─────────────────────────────


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(__doc__ + build_help())
        return
    if not shutil.which("reminders"):
        print(f"{RED}reminders-cli is missing:{RESET} brew install reminders-cli")
        sys.exit(1)

    app = App(interactive=not args)
    try:
        app.start()
        if args:
            app.load()
            try:
                app.handle(" ".join(args))
            except (UserError, CLIError) as e:
                app.msg = f"{RED}{e}{RESET}"
            app.load()
            app.render()
            return
    except CLIError as e:
        print(f"{RED}Error:{RESET} {e}")
        sys.exit(1)

    wrap = lambda s: s
    try:
        import atexit
        import readline
        hist = os.path.expanduser("~/.doer_history")
        old_hist = os.path.expanduser("~/.tb_history")
        try:
            if os.path.exists(hist):
                readline.read_history_file(hist)
            elif os.path.exists(old_hist):
                readline.read_history_file(old_hist)
        except OSError:
            pass
        atexit.register(lambda: readline.write_history_file(hist))
        if USE_COLOR:
            wrap = lambda s: ANSI_RE.sub(lambda m: "\001" + m.group(0) + "\002", s)
    except ImportError:
        pass

    while True:
        try:
            app.load()
        except CLIError as e:
            app.items, app.msg = [], f"{RED}Error:{RESET} {e}"
        app.render()
        try:
            line = input(wrap(app.prompt()))
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip().lower() in ("q", "quit", "exit"):
            break
        try:
            app.handle(line)
        except UserError as e:
            app.msg = f"{RED}{e}{RESET}"
        except CLIError as e:
            app.msg = f"{RED}Error:{RESET} {e}"
        except KeyboardInterrupt:
            app.msg = "Interrupted"


if __name__ == "__main__":
    main()
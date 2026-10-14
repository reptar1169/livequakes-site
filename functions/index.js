const { onCall, onRequest, HttpsError } = require("firebase-functions/v2/https");
const { onSchedule } = require("firebase-functions/v2/scheduler");
const { setGlobalOptions } = require("firebase-functions/v2");
const logger = require("firebase-functions/logger");
const { initializeApp } = require("firebase-admin/app");
const { getFirestore, FieldValue } = require("firebase-admin/firestore");
const crypto = require("crypto");

initializeApp();
const db = getFirestore();

// Keep this cheap & close to users; adjust the region if you deploy Hosting
// traffic from elsewhere. us-central1 is the Cloud Functions default.
setGlobalOptions({ region: "us-central1", maxInstances: 10 });

const DEVICE_TYPES = new Set(["raspberry_shake", "diy", "phone", "other"]);
const MAX_STATIONS_PER_USER = 5;

function hashKey(rawKey) {
  return crypto.createHash("sha256").update(rawKey).digest("hex");
}

function isFiniteNumber(n) {
  return typeof n === "number" && Number.isFinite(n);
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

// ---------------------------------------------------------------------------
// registerStation — callable, requires a signed-in Firebase Auth user.
// Creates a station document owned by that user and returns a one-time API
// key (only the SHA-256 hash of the key is ever stored). The client re-shows
// this key once for the user to save; it cannot be recovered later, only
// rotated by registering again.
// ---------------------------------------------------------------------------
exports.registerStation = onCall(async (request) => {
  if (!request.auth) {
    throw new HttpsError("unauthenticated", "Sign in to register a station.");
  }
  const uid = request.auth.uid;
  const data = request.data || {};

  const name = typeof data.name === "string" ? data.name.trim() : "";
  const lat = Number(data.lat);
  const lon = Number(data.lon);
  const deviceType = DEVICE_TYPES.has(data.deviceType) ? data.deviceType : "other";
  const deviceModel = typeof data.deviceModel === "string" ? data.deviceModel.trim().slice(0, 80) : "";

  if (name.length < 1 || name.length > 60) {
    throw new HttpsError("invalid-argument", "Station name must be 1-60 characters.");
  }
  if (!isFiniteNumber(lat) || lat < -90 || lat > 90) {
    throw new HttpsError("invalid-argument", "Latitude must be between -90 and 90.");
  }
  if (!isFiniteNumber(lon) || lon < -180 || lon > 180) {
    throw new HttpsError("invalid-argument", "Longitude must be between -180 and 180.");
  }

  const existing = await db.collection("stations").where("ownerUid", "==", uid).get();
  if (existing.size >= MAX_STATIONS_PER_USER) {
    throw new HttpsError(
      "resource-exhausted",
      `You can register at most ${MAX_STATIONS_PER_USER} stations.`
    );
  }

  const apiKey = crypto.randomBytes(24).toString("base64url");
  const apiKeyHash = hashKey(apiKey);

  const stationRef = db.collection("stations").doc();
  await stationRef.set({
    ownerUid: uid,
    name,
    lat,
    lon,
    deviceType,
    deviceModel,
    apiKeyHash,
    status: "pending", // flips to "active" on first ingested event
    eventCount: 0,
    createdAt: FieldValue.serverTimestamp(),
    lastSeenAt: null,
  });

  logger.info("Station registered", { stationId: stationRef.id, uid });

  // apiKey is returned exactly once -- it is never stored or logged in the clear.
  return { stationId: stationRef.id, apiKey, name };
});

// ---------------------------------------------------------------------------
// ingestEvent — plain HTTP endpoint for stations to POST trigger events to.
// Auth is a per-station API key (header: X-Api-Key), not Firebase Auth,
// since the caller is an unattended device/script, not a signed-in browser.
//
// Expected JSON body:
//   {
//     "ts": 1735689600000,      // optional, epoch ms or ISO string; defaults to now
//     "amplitude": 42,          // required, unitless relative trigger strength 0-1000
//     "durationMs": 8000,       // required, 0-600000 (10 min sanity cap)
//     "note": "optional text"
//   }
// ---------------------------------------------------------------------------
exports.ingestEvent = onRequest(async (req, res) => {
  res.set("Access-Control-Allow-Origin", "*");
  res.set("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.set("Access-Control-Allow-Headers", "Content-Type, X-Api-Key");

  if (req.method === "OPTIONS") {
    res.status(204).send("");
    return;
  }
  if (req.method !== "POST") {
    res.status(405).json({ error: "Use POST." });
    return;
  }

  const rawKey = req.get("X-Api-Key");
  if (!rawKey || typeof rawKey !== "string") {
    res.status(401).json({ error: "Missing X-Api-Key header." });
    return;
  }

  const body = req.body || {};
  const amplitude = Number(body.amplitude);
  const durationMs = Number(body.durationMs);
  const note = typeof body.note === "string" ? body.note.slice(0, 200) : "";

  if (!isFiniteNumber(amplitude) || amplitude < 0 || amplitude > 1000) {
    res.status(400).json({ error: "amplitude must be a number between 0 and 1000." });
    return;
  }
  if (!isFiniteNumber(durationMs) || durationMs < 0 || durationMs > 600000) {
    res.status(400).json({ error: "durationMs must be a number between 0 and 600000." });
    return;
  }

  let ts = new Date();
  if (body.ts != null) {
    const parsed = typeof body.ts === "number" ? new Date(body.ts) : new Date(String(body.ts));
    if (!isNaN(parsed.getTime())) ts = parsed;
  }

  const apiKeyHash = hashKey(rawKey);
  const stationQuery = await db
    .collection("stations")
    .where("apiKeyHash", "==", apiKeyHash)
    .limit(1)
    .get();

  if (stationQuery.empty) {
    res.status(401).json({ error: "Unrecognized API key." });
    return;
  }

  const stationDoc = stationQuery.docs[0];
  const eventRef = stationDoc.ref.collection("events").doc();

  await eventRef.set({
    ts,
    amplitude,
    durationMs,
    note,
    receivedAt: FieldValue.serverTimestamp(),
    // Filled in later by checkUsgsCoincidence -- USGS's catalog has a few
    // minutes of latency for a real quake to appear, so this can't be
    // checked synchronously at ingest time.
    usgsChecked: false,
    usgsMatch: null,
  });

  await stationDoc.ref.update({
    lastSeenAt: FieldValue.serverTimestamp(),
    eventCount: FieldValue.increment(1),
    status: "active",
  });

  logger.info("Event ingested", { stationId: stationDoc.id, amplitude, durationMs });

  res.status(200).json({ ok: true, stationId: stationDoc.id, eventId: eventRef.id });
});

// ---------------------------------------------------------------------------
// deleteStation — callable, requires the signed-in owner of the station.
// Removes the station doc and every event in its events subcollection
// (deleted in batches, since a batch write is capped at 500 operations).
// ---------------------------------------------------------------------------
exports.deleteStation = onCall(async (request) => {
  if (!request.auth) {
    throw new HttpsError("unauthenticated", "Sign in to delete a station.");
  }
  const stationId = typeof request.data?.stationId === "string" ? request.data.stationId : "";
  if (!stationId) {
    throw new HttpsError("invalid-argument", "stationId is required.");
  }

  const stationRef = db.collection("stations").doc(stationId);
  const stationSnap = await stationRef.get();
  if (!stationSnap.exists) {
    throw new HttpsError("not-found", "Station not found.");
  }
  if (stationSnap.data().ownerUid !== request.auth.uid) {
    throw new HttpsError("permission-denied", "You don't own this station.");
  }

  await deleteAllInBatches(stationRef.collection("events"));

  await stationRef.delete();

  logger.info("Station deleted", { stationId, uid: request.auth.uid });

  return { ok: true };
});

// Deletes every doc in a query/collection ref in batches of <=500 (a single
// batch write's cap). Shared by deleteStation and clearStationEvents.
async function deleteAllInBatches(ref) {
  let total = 0;
  while (true) {
    const batchSnap = await ref.limit(500).get();
    if (batchSnap.empty) break;
    const batch = db.batch();
    batchSnap.docs.forEach((d) => batch.delete(d.ref));
    await batch.commit();
    total += batchSnap.size;
    if (batchSnap.size < 500) break;
  }
  return total;
}

// ---------------------------------------------------------------------------
// deleteEvent — callable, requires the signed-in owner of the event's
// station. Removes a single event, e.g. to clean out a one-off test trigger
// without wiping the whole station's history.
// ---------------------------------------------------------------------------
exports.deleteEvent = onCall(async (request) => {
  if (!request.auth) {
    throw new HttpsError("unauthenticated", "Sign in to delete an event.");
  }
  const stationId = typeof request.data?.stationId === "string" ? request.data.stationId : "";
  const eventId = typeof request.data?.eventId === "string" ? request.data.eventId : "";
  if (!stationId || !eventId) {
    throw new HttpsError("invalid-argument", "stationId and eventId are required.");
  }

  const stationRef = db.collection("stations").doc(stationId);
  const stationSnap = await stationRef.get();
  if (!stationSnap.exists) {
    throw new HttpsError("not-found", "Station not found.");
  }
  if (stationSnap.data().ownerUid !== request.auth.uid) {
    throw new HttpsError("permission-denied", "You don't own this station.");
  }

  const eventRef = stationRef.collection("events").doc(eventId);
  const eventSnap = await eventRef.get();
  if (!eventSnap.exists) {
    throw new HttpsError("not-found", "Event not found.");
  }

  await eventRef.delete();

  const updates = { eventCount: FieldValue.increment(-1) };
  if (eventSnap.data().usgsMatch) {
    updates.usgsConfirmedCount = FieldValue.increment(-1);
  }
  await stationRef.update(updates);

  logger.info("Event deleted", { stationId, eventId, uid: request.auth.uid });

  return { ok: true };
});

// ---------------------------------------------------------------------------
// clearStationEvents — callable, requires the signed-in owner of the
// station. Removes every event for a station in one go (e.g. to wipe out a
// pile of test triggers from initial setup) without deleting the station
// itself or its API key.
// ---------------------------------------------------------------------------
exports.clearStationEvents = onCall(async (request) => {
  if (!request.auth) {
    throw new HttpsError("unauthenticated", "Sign in to clear events.");
  }
  const stationId = typeof request.data?.stationId === "string" ? request.data.stationId : "";
  if (!stationId) {
    throw new HttpsError("invalid-argument", "stationId is required.");
  }

  const stationRef = db.collection("stations").doc(stationId);
  const stationSnap = await stationRef.get();
  if (!stationSnap.exists) {
    throw new HttpsError("not-found", "Station not found.");
  }
  if (stationSnap.data().ownerUid !== request.auth.uid) {
    throw new HttpsError("permission-denied", "You don't own this station.");
  }

  const deletedCount = await deleteAllInBatches(stationRef.collection("events"));

  await stationRef.update({ eventCount: 0, usgsConfirmedCount: 0 });

  logger.info("Station events cleared", { stationId, uid: request.auth.uid, deletedCount });

  return { ok: true, deletedCount };
});

// ---------------------------------------------------------------------------
// checkUsgsCoincidence -- scheduled, runs every 5 minutes. Community-station
// events are cheap DIY sensors that can be tripped by non-seismic local
// noise (see main.py's own impulsive-event rejection for the firmware-side
// half of this), so this cross-checks each event against USGS's real
// catalog: was there an actual reported earthquake near this station at
// roughly this time? The result is stored on the event (usgsMatch) rather
// than used to delete/hide anything -- so the raw community data stays
// intact and a frontend can choose how to use the confirmation later.
//
// USGS's catalog isn't instant -- small/local quakes can take several
// minutes to appear -- so this only checks events once they're at least
// USGS_CATCHUP_MIN old, and gives up (marks them checked with no match)
// once they're older than USGS_GIVE_UP_MIN so a permanently-unmatched
// event doesn't get retried forever.
// ---------------------------------------------------------------------------
const USGS_CATCHUP_MIN = 10; // wait this long after an event before checking
const USGS_GIVE_UP_MIN = 65; // stop retrying (mark checked, unmatched) after this
const USGS_WINDOW_MIN = 10; // +/- window around the event's ts to search
const USGS_RADIUS_KM = 500; // how far a "coincident" quake can be from the station
const USGS_MIN_MAGNITUDE = 1.0; // ignore anything USGS itself considers noise-tier
const USGS_BATCH_SIZE = 50; // events processed per run, to bound execution time

exports.checkUsgsCoincidence = onSchedule("every 5 minutes", async () => {
  const now = Date.now();
  const catchupCutoff = new Date(now - USGS_CATCHUP_MIN * 60 * 1000);

  const pending = await db
    .collectionGroup("events")
    .where("usgsChecked", "==", false)
    .where("ts", "<=", catchupCutoff)
    .orderBy("ts")
    .limit(USGS_BATCH_SIZE)
    .get();

  if (pending.empty) {
    logger.info("checkUsgsCoincidence: nothing pending");
    return;
  }

  // Cache one station lookup per stationId within this run -- a batch is
  // very unlikely to span many distinct stations, and this avoids a
  // redundant read per event when a station has several pending events.
  const stationCache = new Map();

  for (const eventDoc of pending.docs) {
    const eventData = eventDoc.data();
    const eventTs = eventData.ts.toDate();

    // Too old to ever usefully check (function was down, backlog, etc.) --
    // mark it checked-but-unmatched rather than retrying forever.
    if (eventTs.getTime() < now - USGS_GIVE_UP_MIN * 60 * 1000) {
      await eventDoc.ref.update({ usgsChecked: true, usgsMatch: null });
      continue;
    }

    const stationRef = eventDoc.ref.parent.parent;
    let station = stationCache.get(stationRef.path);
    if (station === undefined) {
      const stationSnap = await stationRef.get();
      station = stationSnap.exists ? stationSnap.data() : null;
      stationCache.set(stationRef.path, station);
    }
    if (!station || !isFiniteNumber(station.lat) || !isFiniteNumber(station.lon)) {
      // Station was deleted, or somehow has no coordinates -- can't check
      // radius, so just mark it done rather than blocking the batch on it.
      await eventDoc.ref.update({ usgsChecked: true, usgsMatch: null });
      continue;
    }

    const windowMs = USGS_WINDOW_MIN * 60 * 1000;
    const params = new URLSearchParams({
      format: "geojson",
      starttime: new Date(eventTs.getTime() - windowMs).toISOString(),
      endtime: new Date(eventTs.getTime() + windowMs).toISOString(),
      latitude: String(station.lat),
      longitude: String(station.lon),
      maxradiuskm: String(USGS_RADIUS_KM),
      minmagnitude: String(USGS_MIN_MAGNITUDE),
      orderby: "time",
    });

    let usgsMatch = null;
    try {
      const resp = await fetch(
        "https://earthquake.usgs.gov/fdsnws/event/1/query?" + params.toString()
      );
      if (!resp.ok) {
        logger.warn("USGS query failed", { status: resp.status, eventId: eventDoc.id });
      } else {
        const geojson = await resp.json();
        const features = geojson.features || [];
        let best = null;
        let bestDeltaS = Infinity;
        for (const f of features) {
          const deltaS = Math.abs(f.properties.time - eventTs.getTime()) / 1000;
          if (deltaS < bestDeltaS) {
            best = f;
            bestDeltaS = deltaS;
          }
        }
        if (best) {
          const [qLon, qLat] = best.geometry.coordinates;
          usgsMatch = {
            usgsId: best.id,
            magnitude: best.properties.mag,
            place: best.properties.place,
            distanceKm: Math.round(haversineKm(station.lat, station.lon, qLat, qLon)),
            timeDeltaS: Math.round(bestDeltaS),
          };
        }
      }
    } catch (e) {
      logger.warn("USGS query error", { error: String(e), eventId: eventDoc.id });
      // Leave usgsChecked false so it's retried on the next run rather than
      // being permanently marked unmatched due to a transient network error.
      continue;
    }

    await eventDoc.ref.update({ usgsChecked: true, usgsMatch });
    if (usgsMatch) {
      await stationRef.update({ usgsConfirmedCount: FieldValue.increment(1) });
    }
    logger.info("checkUsgsCoincidence", {
      eventId: eventDoc.id,
      stationId: stationRef.id,
      matched: !!usgsMatch,
    });
  }
});

// ---------------------------------------------------------------------------
// areaRisk — public GET endpoint behind Hosting at /api/risk.
//   /api/risk?zip=94110   or   /api/risk?lat=37.77&lon=-122.42
//
// Returns the long-term chance of strong shaking from the USGS National
// Seismic Hazard Model (a forecast, not a prediction) plus a short local
// earthquake history from the USGS catalog.
//
// NSHM answers are slow (5-20 s) and only change when USGS releases a new
// model, so they're cached forever in Firestore per ~5 km grid cell (the
// model id is part of the cache key). The catalog history is refreshed daily.
// Responses are also CDN-cached by Hosting via Cache-Control.
// ---------------------------------------------------------------------------
const NSHM_BASE = "https://earthquake.usgs.gov/ws/nshmp/";
const COMCAT_BASE = "https://earthquake.usgs.gov/fdsnws/event/1/";
// Site class D ("stiff soil", Vs30 = 360 m/s): the building-code default when
// soil isn't known, and closer to where most people live than bedrock (760).
const RISK_VS30 = 360;
const RISK_GRID = 0.05; // degrees (~5 km)
const RISK_YEARS = 50;
// Local activity: M5+ within 100 km (roughly how far moderate quakes are felt).
const HISTORY_RADIUS_KM = 100;
const HISTORY_MIN_MAG = 5;
const HISTORY_START = "1950-01-01";
// "Most significant nearby": big quakes damage far from the epicenter (Loma
// Prieta hit San Francisco from 92 km, the 1964 M9.2 hit Anchorage from
// ~120 km), so this looks wider and further back than the activity count.
const NOTABLE_RADIUS_KM = 200;
const NOTABLE_MIN_MAG = 6;
const NOTABLE_START = "1900-01-01";
const HISTORY_TTL_MS = 24 * 60 * 60 * 1000;
const HISTORY_VERSION = 2; // bump when the history shape changes to bypass old cache entries
// Peak ground acceleration (g) at the start of each Modified Mercalli level,
// per the ShakeMap PGA-to-intensity relation (Wald et al., 1999).
const SHAKING_LEVELS = [
  { mmi: "VI", label: "Strong", pga: 0.092, effect: "Felt by everyone; heavy furniture may move; slight damage." },
  { mmi: "VII", label: "Very strong", pga: 0.18, effect: "Damage to poorly built structures; slight damage to well-built ones." },
  { mmi: "VIII", label: "Severe", pga: 0.34, effect: "Considerable damage to ordinary buildings; partial collapse possible." },
];

let zctaCache = null;
function zipToLatLon(zip) {
  if (!zctaCache) zctaCache = require("./data/zcta.json"); // Census 2024 ZCTA gazetteer
  return zctaCache[zip] || null;
}

// Which NSHM model covers a point (null = outside every model, e.g. Puerto Rico).
function nshmModelFor(lat, lon) {
  if (lat >= 18 && lat <= 23 && lon >= -161 && lon <= -154) return "hawaii-2021";
  if (lat >= 50 && lat <= 72 && (lon <= -129 || lon >= 170)) return "alaska-2023";
  if (lat >= 24 && lat <= 50 && lon >= -125.5 && lon <= -65) return "conus-2023";
  return null;
}

// Annual exceedance rate at `a` g, log-log interpolated along the hazard curve.
function annualRate(xs, ys, a) {
  if (a <= xs[0]) return ys[0];
  for (let i = 0; i < xs.length - 1; i++) {
    if (a >= xs[i] && a <= xs[i + 1]) {
      if (!(ys[i] > 0 && ys[i + 1] > 0)) return 0;
      const t = (Math.log(a) - Math.log(xs[i])) / (Math.log(xs[i + 1]) - Math.log(xs[i]));
      return Math.exp(Math.log(ys[i]) + t * (Math.log(ys[i + 1]) - Math.log(ys[i])));
    }
  }
  return 0;
}

async function fetchJson(url, timeoutMs) {
  const res = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  return res.json();
}

async function fetchHazardCurve(model, lat, lon) {
  const url = `${NSHM_BASE}${model}/dynamic/hazard/${lon}/${lat}/${RISK_VS30}`;
  const json = await fetchJson(url, 45000);
  if (json.status !== "success") throw new Error(`NSHM ${model} returned status ${json.status}`);
  const pga = (json.response.hazardCurves || []).find((c) => c.imt && c.imt.value === "PGA");
  const total = pga && pga.data.find((d) => d.component === "Total");
  if (!total) throw new Error("NSHM response had no total PGA curve");
  return { xs: total.values.xs, ys: total.values.ys };
}

async function fetchHistory(lat, lon) {
  const near = `latitude=${lat}&longitude=${lon}`;
  const local = `${near}&maxradiuskm=${HISTORY_RADIUS_KM}&minmagnitude=${HISTORY_MIN_MAG}&starttime=${HISTORY_START}`;
  const wide = `${near}&maxradiuskm=${NOTABLE_RADIUS_KM}&minmagnitude=${NOTABLE_MIN_MAG}&starttime=${NOTABLE_START}`;
  const [countText, localLargest, notable] = await Promise.all([
    fetch(`${COMCAT_BASE}count?${local}`, { signal: AbortSignal.timeout(15000) }).then((r) => {
      if (!r.ok) throw new Error(`ComCat count HTTP ${r.status}`);
      return r.text();
    }),
    fetchJson(`${COMCAT_BASE}query?format=geojson&${local}&orderby=magnitude&limit=1`, 15000),
    fetchJson(`${COMCAT_BASE}query?format=geojson&${wide}&orderby=magnitude&limit=1`, 15000),
  ]);
  const summarize = (fc) => {
    const f = fc.features && fc.features[0];
    if (!f) return null;
    const [eLon, eLat] = f.geometry.coordinates;
    return {
      id: f.id,
      mag: f.properties.mag,
      place: f.properties.place,
      time: f.properties.time,
      distanceKm: Math.round(haversineKm(lat, lon, eLat, eLon)),
    };
  };
  return {
    v: HISTORY_VERSION,
    count: parseInt(countText, 10) || 0,
    largest: summarize(localLargest),
    notable: summarize(notable),
  };
}

exports.areaRisk = onRequest({ timeoutSeconds: 60, memory: "512MiB" }, async (req, res) => {
  res.set("Access-Control-Allow-Origin", "*");
  if (req.method !== "GET") {
    res.status(405).json({ error: "Use GET." });
    return;
  }

  let lat, lon, zip = null;
  if (req.query.zip !== undefined) {
    zip = String(req.query.zip).trim();
    if (!/^\d{5}$/.test(zip)) {
      res.status(400).json({ error: "Enter a 5-digit U.S. ZIP code." });
      return;
    }
    const ll = zipToLatLon(zip);
    if (!ll) {
      res.status(404).json({ error: `ZIP code ${zip} wasn't found.` });
      return;
    }
    [lat, lon] = ll;
  } else {
    lat = Number(req.query.lat);
    lon = Number(req.query.lon);
    if (!isFiniteNumber(lat) || !isFiniteNumber(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
      res.status(400).json({ error: "Provide a ZIP code or valid lat/lon." });
      return;
    }
  }

  const model = nshmModelFor(lat, lon);
  if (!model) {
    res.status(422).json({ error: "Forecasts are available for the 50 U.S. states only." });
    return;
  }

  // Snap to the cache grid; every lookup in a cell shares one NSHM call.
  const gLat = Number((Math.round(lat / RISK_GRID) * RISK_GRID).toFixed(2));
  const gLon = Number((Math.round(lon / RISK_GRID) * RISK_GRID).toFixed(2));
  const cacheRef = db.collection("riskCache").doc(`${model}_${RISK_VS30}_${gLat}_${gLon}`);

  try {
    const snap = await cacheRef.get();
    const cached = snap.exists ? snap.data() : {};
    let curve = cached.curve;
    let history = cached.history;
    const updates = {};

    const historyStale = !history || history.v !== HISTORY_VERSION || !cached.historyAt ||
      Date.now() - cached.historyAt.toMillis() > HISTORY_TTL_MS;
    const [newCurve, newHistory] = await Promise.all([
      curve ? null : fetchHazardCurve(model, gLat, gLon),
      historyStale ? fetchHistory(gLat, gLon).catch((err) => {
        logger.warn("areaRisk history fetch failed", { err: err.message });
        return null; // fall back to stale history (or none) rather than failing
      }) : null,
    ]);
    if (newCurve) {
      curve = newCurve;
      Object.assign(updates, { curve, model, vs30: RISK_VS30, lat: gLat, lon: gLon, curveAt: FieldValue.serverTimestamp() });
    }
    if (newHistory) {
      history = newHistory;
      Object.assign(updates, { history, historyAt: FieldValue.serverTimestamp() });
    }
    if (Object.keys(updates).length) await cacheRef.set(updates, { merge: true });

    const levels = SHAKING_LEVELS.map((lvl) => {
      const rate = annualRate(curve.xs, curve.ys, lvl.pga);
      return {
        mmi: lvl.mmi,
        label: lvl.label,
        effect: lvl.effect,
        pga: lvl.pga,
        probability: 1 - Math.exp(-RISK_YEARS * rate),
      };
    });

    res.set("Cache-Control", "public, max-age=3600, s-maxage=86400");
    res.json({
      query: { zip, lat, lon },
      grid: { lat: gLat, lon: gLon },
      model,
      vs30: RISK_VS30,
      years: RISK_YEARS,
      levels,
      history: history ? Object.assign({
        radiusKm: HISTORY_RADIUS_KM, minMag: HISTORY_MIN_MAG, since: HISTORY_START,
        notableRadiusKm: NOTABLE_RADIUS_KM, notableMinMag: NOTABLE_MIN_MAG, notableSince: NOTABLE_START,
      }, history) : null,
    });
  } catch (err) {
    logger.error("areaRisk failed", { model, gLat, gLon, err: err.message });
    res.status(502).json({ error: "USGS hazard data is temporarily unavailable. Please try again in a minute." });
  }
});

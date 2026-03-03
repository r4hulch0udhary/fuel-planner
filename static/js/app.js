/**
 * Fuel Route Planner — frontend app
 *
 * Communicates with the Django backend via fetch() — no page reloads.
 * Uses Leaflet.js for the interactive map.
 *
 * Flow:
 *   1. User submits the form.
 *   2. POST /api/route/ → get stops + GeoJSON route.
 *   3. Draw polyline + markers on the Leaflet map.
 *   4. Render stop cards in the sidebar.
 */

"use strict";

// ---------------------------------------------------------------------------
// Map initialisation
// ---------------------------------------------------------------------------

const map = L.map("map").setView([39.5, -98.35], 4); // continental US

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 19,
}).addTo(map);

// Layers we'll clear on each new request
let routeLayer = null;
const markerLayer = L.layerGroup().addTo(map);

// ---------------------------------------------------------------------------
// Custom marker icons
// ---------------------------------------------------------------------------

const fuelIcon = L.divIcon({
  className: "",
  html: `<div style="
    background:#0f3460;color:#fff;border-radius:50%;
    width:28px;height:28px;display:flex;align-items:center;
    justify-content:center;font-size:15px;box-shadow:0 2px 6px rgba(0,0,0,.35);">
    ⛽</div>`,
  iconSize: [28, 28],
  iconAnchor: [14, 14],
});

const pinIcon = (emoji, color) =>
  L.divIcon({
    className: "",
    html: `<div style="
      background:${color};color:#fff;border-radius:50%;
      width:34px;height:34px;display:flex;align-items:center;
      justify-content:center;font-size:18px;box-shadow:0 2px 8px rgba(0,0,0,.4);">
      ${emoji}</div>`,
    iconSize: [34, 34],
    iconAnchor: [17, 17],
  });

// ---------------------------------------------------------------------------
// DOM references
// ---------------------------------------------------------------------------

const form        = document.getElementById("route-form");
const submitBtn   = document.getElementById("submit-btn");
const summaryCard = document.getElementById("summary-card");
const stopList    = document.getElementById("stop-list");
const errorBox    = document.getElementById("error-box");

const statDistance = document.getElementById("stat-distance");
const statCost     = document.getElementById("stat-cost");
const statGallons  = document.getElementById("stat-gallons");
const statStops    = document.getElementById("stat-stops");

// ---------------------------------------------------------------------------
// Form submit handler
// ---------------------------------------------------------------------------

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  clearUI();

  const origin          = document.getElementById("origin").value.trim();
  const destination     = document.getElementById("destination").value.trim();
  const mpg             = parseFloat(document.getElementById("mpg").value);
  const tankRangeMiles  = parseFloat(document.getElementById("range").value);

  if (!origin || !destination) {
    showError("Please enter both an origin and a destination.");
    return;
  }

  setLoading(true);

  try {
    const resp = await fetch("/api/route/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        origin,
        destination,
        mpg,
        tank_range_miles: tankRangeMiles,
      }),
    });

    const data = await resp.json();

    if (!resp.ok) {
      // Format DRF field validation errors as a readable list
      if (data.errors && typeof data.errors === "object") {
        const msgs = Object.entries(data.errors)
          .map(([field, errs]) => `${field}: ${Array.isArray(errs) ? errs.join(", ") : errs}`)
          .join("\n");
        showError(msgs);
      } else {
        showError(data.error || "Unknown error.");
      }
      return;
    }

    renderResult(data);
  } catch (err) {
    showError("Network error. Is the server running?");
    console.error(err);
  } finally {
    setLoading(false);
  }
});

// ---------------------------------------------------------------------------
// Render result on map + sidebar
// ---------------------------------------------------------------------------

/**
 * Render the API response onto the map and sidebar.
 * @param {Object} data - The JSON response from /api/route/
 */
function renderResult(data) {
  // -- Route polyline --
  // GeoJSON coords are [lon, lat]; Leaflet wants [lat, lon]
  const latLngs = data.route_geometry.map(([lon, lat]) => [lat, lon]);

  if (routeLayer) map.removeLayer(routeLayer);
  routeLayer = L.polyline(latLngs, {
    color: "#0f3460",
    weight: 4,
    opacity: 0.75,
  }).addTo(map);

  map.fitBounds(routeLayer.getBounds(), { padding: [40, 40] });

  // -- Origin / destination markers --
  markerLayer.clearLayers();

  if (latLngs.length > 0) {
    L.marker(latLngs[0], { icon: pinIcon("🚀", "#276749") })
      .bindPopup(`<b>Start</b><br>${data.origin}`)
      .addTo(markerLayer);

    L.marker(latLngs[latLngs.length - 1], { icon: pinIcon("🏁", "#c53030") })
      .bindPopup(`<b>Finish</b><br>${data.destination}`)
      .addTo(markerLayer);
  }

  // -- Fuel stop markers --
  data.fuel_stops.forEach((stop, i) => {
    const marker = L.marker([stop.latitude, stop.longitude], { icon: fuelIcon })
      .bindPopup(`
        <b>${stop.name}</b><br>
        ${stop.address}, ${stop.city}, ${stop.state}<br>
        <b>$${stop.retail_price.toFixed(3)}/gal</b> &nbsp;·&nbsp;
        ${stop.gallons_to_fill.toFixed(1)} gal &nbsp;·&nbsp;
        <b>$${stop.cost_at_stop.toFixed(2)}</b>
      `)
      .addTo(markerLayer);

    // Cross-link sidebar card → map popup
    marker._stopIndex = i;
  });

  // -- Summary card --
  statDistance.textContent = `${data.total_distance_miles.toLocaleString()} mi`;
  statCost.textContent     = `$${data.total_fuel_cost.toFixed(2)}`;
  statGallons.textContent  = `${data.total_gallons.toFixed(1)} gal`;
  statStops.textContent    = data.fuel_stops.length;
  summaryCard.classList.remove("hidden");

  // -- Stop list in sidebar --
  stopList.innerHTML = "";

  // Warning banner — shown for partial routes (stops found but route incomplete),
  // AND for the zero-stops-but-incomplete case below.
  if (data.warning) {
    const banner = document.createElement("div");
    banner.style.cssText = [
      "background:#fffbeb","border:1px solid #f59e0b","border-radius:8px",
      "padding:10px 14px","margin-bottom:12px","font-size:13px",
      "color:#92400e","line-height:1.5",
    ].join(";");
    banner.innerHTML = `<strong>⚠️ Partial route</strong><br>${data.warning}`;
    stopList.appendChild(banner);
  }

  if (data.fuel_stops.length === 0) {
    if (data.station_count === 0) {
      // DB is empty — the import command has not been run yet
      stopList.innerHTML =
        `<p style="color:#c53030;font-size:13px;font-weight:600;">
          ⚠️ No station data loaded.<br>
          <span style="font-weight:400;">Run <code>python manage.py import_stations</code> first.</span>
        </p>`;
    } else if (!data.warning) {
      // No warning set — check if trip is within range
      if (data.total_distance_miles <= (data.tank_range_miles ?? Infinity)) {
        stopList.innerHTML =
          `<p style="color:#276749;font-size:13px;">
            ✅ No fuel stops needed — destination is within tank range!
          </p>`;
      } else {
        stopList.innerHTML =
          `<p style="color:#b7791f;font-size:13px;">
            ⚠️ No stops could be planned. Check that stations exist along this route.
          </p>`;
      }
    }
    // If data.warning is set and fuel_stops is empty, the banner above is sufficient.
    return;
  }

  const markers = markerLayer.getLayers().filter(
    (l) => l instanceof L.Marker && l._stopIndex !== undefined
  );

  data.fuel_stops.forEach((stop, i) => {
    const card = document.createElement("div");
    card.className = "stop-card";
    card.innerHTML = `
      <div class="stop-name">Stop ${i + 1}: ${stop.name}</div>
      <div class="stop-location">${stop.city}, ${stop.state}</div>
      <div class="stop-meta">
        <span class="price">$${stop.retail_price.toFixed(3)}/gal</span>
        <span class="gals">${stop.gallons_to_fill.toFixed(1)} gal</span>
        <span class="cost">$${stop.cost_at_stop.toFixed(2)}</span>
      </div>
    `;

    // Click card → open popup on map
    card.addEventListener("click", () => {
      const m = markers[i];
      if (m) {
        map.setView(m.getLatLng(), 11, { animate: true });
        m.openPopup();
      }
    });

    stopList.appendChild(card);
  });
}

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------

/** Clear previous results from map and sidebar. */
function clearUI() {
  markerLayer.clearLayers();
  if (routeLayer) { map.removeLayer(routeLayer); routeLayer = null; }
  summaryCard.classList.add("hidden");
  stopList.innerHTML = "";
  errorBox.classList.add("hidden");
  errorBox.textContent = "";
}

/** Display an error message in the sidebar. */
function showError(msg) {
  errorBox.textContent = msg;
  errorBox.classList.remove("hidden");
}

/** Toggle the loading state on the submit button. */
function setLoading(loading) {
  submitBtn.disabled = loading;
  submitBtn.textContent = loading ? "Computing route…" : "Find Cheapest Route";
}

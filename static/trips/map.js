"use strict";

const byId = id => document.getElementById(id);
const defaults = JSON.parse(byId("trip-defaults").textContent);
byId("initial-fuel").value = defaults.initial_fuel_gallons;
byId("initial-price").value = defaults.initial_fuel_price_usd_per_gallon;
byId("verify-stops").checked = defaults.verify_stops;
byId("corridor").value = defaults.station_corridor_miles;

function syncLocationMode(name) {
  const coordinates = byId(`${name}-mode`).value === "coordinates";
  byId(`${name}-text-field`).hidden = coordinates;
  byId(`${name}-coordinate-fields`).hidden = !coordinates;
  byId(`${name}-text`).disabled = coordinates;
  byId(`${name}-text`).required = !coordinates;
  for (const axis of ["latitude", "longitude"]) {
    byId(`${name}-${axis}`).disabled = !coordinates;
    byId(`${name}-${axis}`).required = coordinates;
  }
}
for (const name of ["start", "finish"]) {
  byId(`${name}-mode`).addEventListener("change", () => syncLocationMode(name));
}

const map = L.map("map").setView([38, -97], 4);
const tiles = JSON.parse(byId("map-tiles").textContent);
L.tileLayer(tiles.url, { maxNativeZoom: 16, maxZoom: 19, attribution: tiles.attribution }).addTo(map);
const overlays = L.featureGroup().addTo(map);
let lastResult;
const dollars = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const quantities = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const gallons = new Intl.NumberFormat("en-US", { maximumFractionDigits: 7 });
const prices = new Intl.NumberFormat("en-US", { minimumFractionDigits: 3, maximumFractionDigits: 8 });
const dollar = value => dollars.format(Number(value));
const decimal = value => quantities.format(Number(value));

function setStatus(text, error = false) {
  byId("status").className = error ? "status error" : "status";
  byId("status").textContent = text;
  if (error) byId("status").focus();
}

function clearResult() {
  lastResult = undefined;
  byId("result").hidden = true;
  byId("fit-route").disabled = true;
  byId("stop-legend").hidden = true;
  byId("map-caption").textContent = "Calculate a trip to see the route.";
  overlays.clearLayers();
}

function inputsChanged() {
  if (lastResult) {
    clearResult();
    setStatus("Locations or options changed. Calculate the route again.");
  }
}
byId("trip-form").addEventListener("input", inputsChanged);
byId("trip-form").addEventListener("change", inputsChanged);
byId("trip-form").addEventListener("invalid", event => {
  const options = event.target.closest("details");
  if (options) options.open = true;
}, true);
byId("swap").addEventListener("click", () => {
  for (const field of ["mode", "text", "latitude", "longitude"]) {
    const start = byId(`start-${field}`);
    const finish = byId(`finish-${field}`);
    [start.value, finish.value] = [finish.value, start.value];
  }
  syncLocationMode("start");
  syncLocationMode("finish");
  inputsChanged();
});

function locationInput(name) {
  if (byId(`${name}-mode`).value === "text") return byId(`${name}-text`).value.trim();
  return {
    latitude: Number(byId(`${name}-latitude`).value),
    longitude: Number(byId(`${name}-longitude`).value)
  };
}

function paragraph(parent, text, className = "") {
  const item = document.createElement("p");
  item.textContent = text;
  item.className = className;
  parent.appendChild(item);
}

function marker(coordinates, label, popup, endpoint = false) {
  const content = document.createElement("div");
  content.textContent = popup;
  return L.marker([coordinates[1], coordinates[0]], {
    icon: L.divIcon({
      className: `route-marker${endpoint ? " endpoint" : ""}`,
      html: label,
      iconSize: [32, 32],
      iconAnchor: [16, 16]
    }),
    title: popup
  }).bindPopup(content).addTo(overlays);
}

function fitRoute() {
  if (lastResult) {
    map.fitBounds(overlays.getBounds(), {
      paddingTopLeft: [28, 126], paddingBottomRight: [28, 84], maxZoom: 13
    });
  }
}
byId("fit-route").addEventListener("click", fitRoute);

function render(result) {
  lastResult = result;
  overlays.clearLayers();
  L.geoJSON(result.route.geometry, { style: { color: "#215c47", weight: 5 } }).addTo(overlays);
  marker(result.start.coordinates, "A", result.start.label, true);
  marker(result.finish.coordinates, "B", result.finish.label, true);
  byId("stops").replaceChildren();
  for (const stop of result.fuel.stops) {
    const mapMarker = marker(
      [stop.location.longitude, stop.location.latitude], String(stop.sequence),
      `${stop.name} · Buy ${gallons.format(Number(stop.gallons_purchased))} gal`
    );
    const item = document.createElement("li");
    const number = document.createElement("span");
    number.className = "stop-number";
    number.textContent = stop.sequence;
    number.setAttribute("aria-hidden", "true");
    const content = document.createElement("div");
    const name = document.createElement("button");
    name.type = "button";
    name.className = "stop-name";
    name.textContent = stop.name;
    name.setAttribute("aria-label", `Stop ${stop.sequence}: ${stop.name}. Show on map.`);
    name.addEventListener("click", () => {
      map.panTo(mapMarker.getLatLng(), { animate: false });
      mapMarker.openPopup();
      byId("map").scrollIntoView({ block: "nearest" });
    });
    content.appendChild(name);
    paragraph(content, `${stop.address}, ${stop.city}, ${stop.state}`, "muted");
    paragraph(content, `Route mile ${decimal(stop.route_mile)}`, "muted");
    paragraph(content, `${gallons.format(Number(stop.gallons_purchased))} gal × $${prices.format(Number(stop.price_usd_per_gallon))}/gal = ${dollar(stop.cost_usd)}`, "stop-cost");
    paragraph(content, `Arrive with ${decimal(stop.arrival_fuel_gallons)} gal · Leave with ${decimal(stop.departure_fuel_gallons)} gal`, "muted");
    item.append(number, content);
    byId("stops").appendChild(item);
  }
  const count = result.fuel.stops.length;
  byId("route-label").textContent = `${result.start.label} → ${result.finish.label}`;
  byId("total-cost").textContent = dollar(result.fuel.total_cost_usd);
  byId("summary").textContent = `${decimal(result.route.distance_miles)} miles · ${count} fuel ${count === 1 ? "stop" : "stops"} · ${decimal(result.fuel.gallons_consumed)} gal consumed`;
  byId("purchase-cost").textContent = dollar(result.fuel.purchase_cost_usd);
  byId("initial-cost").textContent = dollar(result.fuel.initial_fuel_cost_usd);
  byId("ending-fuel").textContent = `${decimal(result.fuel.ending_fuel_gallons)} gal`;
  byId("stop-count").textContent = count;
  byId("no-stops").hidden = count > 0;
  byId("stop-legend").hidden = count === 0;
  byId("accuracy").textContent = result.planning.road_distances_verified
    ? "Distances include the selected stops. Other routes or stations may cost less. Starting fuel value includes the whole initial tank."
    : "Estimate excludes station detours. Enable routing through stops to check road distances. Starting fuel value includes the whole initial tank.";
  const meta = result.metadata;
  byId("coverage").textContent = `${meta.station_dataset.mapped_stations} of ${meta.station_dataset.us_stations} US stations have coordinates. Provider calls: ${meta.routing_calls} routing, ${meta.geocoding_calls} geocoding. Cache hits: ${meta.routing_cache_hits + meta.geocoding_cache_hits}.`;
  byId("map-caption").textContent = `${decimal(result.route.distance_miles)} miles · ${count} fuel ${count === 1 ? "stop" : "stops"}`;
  byId("result").hidden = false;
  byId("fit-route").disabled = false;
  fitRoute();
  byId("result").scrollIntoView({ block: "start" });
  byId("result-heading").focus({ preventScroll: true });
}

byId("trip-form").addEventListener("submit", async event => {
  event.preventDefault();
  clearResult();
  byId("trip-inputs").disabled = true;
  byId("trip-form").setAttribute("aria-busy", "true");
  byId("submit").textContent = "Calculating…";
  setStatus("Calculating the route and fuel purchases…");
  try {
    const response = await fetch("/api/routes/", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        start: locationInput("start"), finish: locationInput("finish"),
        initial_fuel_gallons: byId("initial-fuel").value,
        initial_fuel_price_usd_per_gallon: byId("initial-price").value,
        verify_stops: byId("verify-stops").checked,
        station_corridor_miles: byId("corridor").value
      })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "The route could not be calculated. Try again.");
    render(result);
    setStatus("Route calculated.");
  } catch (error) {
    clearResult();
    setStatus(error instanceof SyntaxError ? "The server returned an unreadable response. Try again." : error.message || "The server could not be reached. Try again.", true);
  } finally {
    byId("trip-inputs").disabled = false;
    byId("trip-form").setAttribute("aria-busy", "false");
    byId("submit").textContent = "Calculate route";
  }
});

byId("download").addEventListener("click", () => {
  if (!lastResult) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(lastResult, null, 2)], { type: "application/json" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "fuel-route.json";
  anchor.click();
  URL.revokeObjectURL(url);
});

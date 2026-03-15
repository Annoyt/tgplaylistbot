/**
 * Weather Dashboard — Frontend Logic
 * Двойная панель: автогеолокация + ручной выбор города
 */

const API = "/api/weather";
const WEEKDAYS = ["Вс","Пн","Вт","Ср","Чт","Пт","Сб"];
const WEATHER_ICONS = {
    "01d":"☀️","01n":"🌙","02d":"⛅","02n":"☁️","03d":"☁️","03n":"☁️",
    "04d":"☁️","04n":"☁️","09d":"🌧","09n":"🌧","10d":"🌦","10n":"🌧",
    "11d":"⛈","11n":"⛈","13d":"❄️","13n":"❄️","50d":"🌫","50n":"🌫",
};

// State per panel
const panels = {
    auto:   { lat: null, lon: null },
    search: { lat: null, lon: null },
};

/* ── Init ──────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", () => {
    initGeolocation();
    initCitySearch();
    initTabs();
});

/* ── Geolocation panel ─────────────────────────── */
function initGeolocation() {
    // Check if we already saved location in localStorage
    const savedLat = localStorage.getItem("weather_auto_lat");
    const savedLon = localStorage.getItem("weather_auto_lon");

    if (savedLat && savedLon) {
        panels.auto.lat = parseFloat(savedLat);
        panels.auto.lon = parseFloat(savedLon);
        // Load immediately
        fetchJSON(`${API}/reverse-geocode?lat=${panels.auto.lat}&lon=${panels.auto.lon}`).then(geo => {
            document.getElementById("auto-location-name").textContent = geo.name ? `${geo.name}, ${geo.country}` : "Определено";
            loadAll("auto");
        });
        return;
    }

    if (!navigator.geolocation) {
        document.getElementById("auto-location-name").textContent = "Геолокация недоступна";
        return;
    }

    // Automatically ask for geolocation if not saved
    navigator.geolocation.getCurrentPosition(
        async (pos) => {
            panels.auto.lat = pos.coords.latitude;
            panels.auto.lon = pos.coords.longitude;

            // Save to localStorage
            localStorage.setItem("weather_auto_lat", panels.auto.lat);
            localStorage.setItem("weather_auto_lon", panels.auto.lon);

            // Reverse geocode
            const geo = await fetchJSON(`${API}/reverse-geocode?lat=${panels.auto.lat}&lon=${panels.auto.lon}`);
            document.getElementById("auto-location-name").textContent = geo.name ? `${geo.name}, ${geo.country}` : "Определено";
            loadAll("auto");
        },
        () => {
            document.getElementById("auto-location-name").textContent = "Разрешите геолокацию";
        }
    );
}

/* ── City search panel ─────────────────────────── */
function initCitySearch() {
    const input = document.getElementById("city-input");
    const btn = document.getElementById("city-search-btn");

    const doSearch = async () => {
        const city = input.value.trim();
        if (!city) return;
        const geo = await fetchJSON(`${API}/geocode?city=${encodeURIComponent(city)}`);
        if (geo.error) { alert(geo.error); return; }
        panels.search.lat = geo.lat;
        panels.search.lon = geo.lon;
        input.value = `${geo.name}, ${geo.country}`;
        loadAll("search");
    };

    if(btn && input) {
        btn.addEventListener("click", doSearch);
        input.addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
    }
}

/* ── Load all data for a panel ─────────────────── */
async function loadAll(panel) {
    const { lat, lon } = panels[panel];
    if (lat === null) return;
    loadCurrent(panel, lat, lon);
    loadAQI(panel, lat, lon);
    loadForecast(panel, lat, lon, 7);
}

/* ── Current Weather ───────────────────────────── */
async function loadCurrent(panel, lat, lon) {
    const el = document.getElementById(`${panel}-current-body`);
    if(!el) return;
    el.innerHTML = "<div class=\"loader\"></div>";
    const d = await fetchJSON(`${API}/current?lat=${lat}&lon=${lon}`);
    if (!d || !d.main) { el.innerHTML = "<span class=\"hint\">Данные недоступны</span>"; return; }
    const icon = WEATHER_ICONS[d.weather?.[0]?.icon] || "🌡";
    const pop = d.rain ? Math.round((d.rain["1h"] || 0) * 100) : 0;
    el.innerHTML = `
        <div class="temp-value">${icon} ${Math.round(d.main.temp)}°</div>
        <div class="temp-desc">${d.weather?.[0]?.description || ""}</div>
        <div class="temp-detail">
            Ощущается ${Math.round(d.main.feels_like)}° · 💧${d.main.humidity}% · 💨${d.wind?.speed}м/с
        </div>
    `;
    // Also update rain panel
    const rainEl = document.getElementById(`${panel}-rain-body`);
    if(rainEl) {
        rainEl.innerHTML = `
            <div class="rain-chance">${pop}%</div>
            <div class="rain-label">Вероятность осадков</div>
            <div class="temp-detail" style="margin-top:0.5rem">
                Облачность: ${d.clouds?.all || 0}% · Видимость: ${((d.visibility || 0)/1000).toFixed(1)}км
            </div>
        `;
    }
}

/* ── Air Quality ───────────────────────────────── */
async function loadAQI(panel, lat, lon) {
    const el = document.getElementById(`${panel}-aqi-body`);
    if(!el) return;
    el.innerHTML = "<div class=\"loader\"></div>";
    const d = await fetchJSON(`${API}/air-quality?lat=${lat}&lon=${lon}`);
    if (!d?.list?.[0]) { el.innerHTML = "<span class=\"hint\">Данные недоступны</span>"; return; }
    const aqi = d.list[0].main.aqi;
    const comp = d.list[0].components;
    const labels = ["","Хорошее","Нормальное","Среднее","Плохое","Опасное"];
    el.innerHTML = `
        <span class="aqi-badge aqi-${aqi}">${labels[aqi]} (${aqi}/5)</span>
        <div class="temp-detail">
            PM2.5: ${comp.pm2_5?.toFixed(1)} · PM10: ${comp.pm10?.toFixed(1)}<br>
            O₃: ${comp.o3?.toFixed(1)} · NO₂: ${comp.no2?.toFixed(1)}
        </div>
    `;
}

/* ── Forecast ──────────────────────────────────── */
async function loadForecast(panel, lat, lon, days) {
    const el = document.getElementById(`${panel}-forecast`);
    if(!el) return;
    el.innerHTML = "<div class=\"loader\"></div>";
    const d = await fetchJSON(`${API}/forecast?lat=${lat}&lon=${lon}&days=${days}`);
    if (!d?.list) { el.innerHTML = "<span class=\"hint\">Данные недоступны</span>"; return; }

    // Group by day
    const dayMap = {};
    d.list.forEach(item => {
        const date = item.dt_txt?.split(" ")[0];
        if (!date) return;
        if (!dayMap[date]) dayMap[date] = { temps: [], icons: [], pops: [] };
        dayMap[date].temps.push(item.main.temp);
        dayMap[date].icons.push(item.weather?.[0]?.icon || "01d");
        dayMap[date].pops.push(item.pop || 0);
    });

    let html = "";
    Object.entries(dayMap).forEach(([date, info]) => {
        const d = new Date(date);
        const dayName = WEEKDAYS[d.getDay()];
        const dateStr = `${d.getDate()}.${String(d.getMonth()+1).padStart(2,"0")}`;
        const maxT = Math.round(Math.max(...info.temps));
        const minT = Math.round(Math.min(...info.temps));
        const icon = WEATHER_ICONS[info.icons[Math.floor(info.icons.length/2)]] || "🌡";
        const pop = Math.round(Math.max(...info.pops) * 100);
        html += `
            <div class="forecast-day glass">
                <div class="day-name">${dayName} ${dateStr}</div>
                <div class="day-icon">${icon}</div>
                <div class="day-temp">${maxT}°</div>
                <div class="day-temp-min">${minT}° · 💧${pop}%</div>
            </div>
        `;
    });
    el.innerHTML = html || "<span class=\"hint\">Нет данных</span>";
}

/* ── Tabs ──────────────────────────────────────── */
function initTabs() {
    document.querySelectorAll(".tab").forEach(tab => {
        tab.addEventListener("click", () => {
            const panel = tab.dataset.panel;
            const days = parseInt(tab.dataset.days);
            // Toggle active class
            tab.closest(".forecast-tabs").querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
            tab.classList.add("active");
            // Reload
            const { lat, lon } = panels[panel];
            if (lat !== null) loadForecast(panel, lat, lon, days);
        });
    });
}

/* ── Fetch Helper ──────────────────────────────── */
async function fetchJSON(url) {
    try {
        const r = await fetch(url);
        return await r.json();
    } catch (e) {
        console.error("Fetch error:", e);
        return null;
    }
}

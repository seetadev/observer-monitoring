// Dashboard line charts, drawn with Chart.js (MIT). Same look as before: dark background, UTC date axis,
// one line per series. series: [{values: [[epoch_ms, value], ...], color: "#rrggbb", width: 2}]
var CHART_BACKGROUND = "#333333";
var CHART_PALETTE = ["#3d99d6", "#da5930", "#6aa84f", "#f1c232", "#8e7cc3", "#e06666"];

var chartBackground = {
    id: "observerBackground",
    beforeDraw: function(chart){
        var ctx = chart.ctx;
        ctx.save();
        ctx.globalCompositeOperation = "destination-over";
        ctx.fillStyle = CHART_BACKGROUND;
        ctx.fillRect(0, 0, chart.width, chart.height);
        ctx.restore();
    }
};

function twoDigits(n){
    return n < 10 ? "0" + n : "" + n;
}

function utcLabel(epochMs){
    var d = new Date(epochMs);
    return twoDigits(d.getUTCMonth() + 1) + "-" + twoDigits(d.getUTCDate()) + " " +
           twoDigits(d.getUTCHours()) + ":" + twoDigits(d.getUTCMinutes());
}

// [[x, y], null, [x, y], ...] -> Chart.js points. A null entry is a gap in the data (the "disjoint" view).
function chartPoints(values){
    var points = [];
    var lastX = null;
    values.forEach(function(p){
        if(p === null || p === undefined){
            if(lastX !== null) points.push({x: lastX + 1, y: null});
        }
        else{
            points.push({x: p[0], y: p[1]});
            lastX = p[0];
        }
    });
    return points;
}

// Draws (or redraws) a line chart inside the element with the given id. Returns the Chart instance.
function renderLineChart(containerId, series, height, width){
    var container = document.getElementById(containerId);
    if(!container) return null;
    var previous = container.querySelector("canvas");
    if(previous){
        var instance = Chart.getChart(previous);
        if(instance) instance.destroy();
    }
    while(container.firstChild) container.removeChild(container.firstChild);

    var canvas = document.createElement("canvas");
    canvas.width = Math.max(300, Math.round(width) || 800);
    canvas.height = height || 400;
    container.appendChild(canvas);

    var datasets = series.map(function(s, i){
        return {
            data: chartPoints(s.values),
            borderColor: s.color || CHART_PALETTE[i % CHART_PALETTE.length],
            borderWidth: s.width || 1,
            pointRadius: 0, pointHitRadius: 6, tension: 0, fill: false, spanGaps: false
        };
    });
    return new Chart(canvas, {
        type: "line",
        data: {datasets: datasets},
        plugins: [chartBackground],
        options: {
            responsive: false, animation: false, parsing: false, normalized: true,
            interaction: {mode: "nearest", axis: "x", intersect: false},
            plugins: {
                legend: {display: false},
                tooltip: {callbacks: {title: function(items){ return items.length ? utcLabel(items[0].parsed.x) : ""; }}}
            },
            scales: {
                x: {type: "linear", bounds: "data",
                    ticks: {color: "#dddddd", maxTicksLimit: 8, callback: function(value){ return utcLabel(value); }},
                    grid: {color: "rgba(255,255,255,0.12)"}},
                y: {ticks: {color: "#dddddd"}, grid: {color: "rgba(255,255,255,0.12)"}}
            }
        }
    });
}

// The four charts of the statistics and cost pages (CPU has one line per core, network has two).
function drawResourceCharts(cpu, ram, net, storage, width){
    var cpuSeries = [];
    for(var i = 0; i < cpu.length; i++){
        cpuSeries.push({values: cpu[i], color: cpuColors[i], width: 2});
    }
    renderLineChart("cpu_chart", cpuSeries, 400, width);
    renderLineChart("ram_chart", [{values: ram, color: ramColor}], 400, width);
    renderLineChart("net_chart", [{values: net[0], color: netColor[0]}, {values: net[1], color: netColor[1]}], 400, width);
    renderLineChart("storage_chart", [{values: storage, color: storageColor}], 400, width);
}

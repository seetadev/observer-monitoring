var _selectedRun = null;
var _lastDetail = null;

$(document).ready(function(){
    refreshLoadtests();
    setInterval(refreshLoadtests, 2000);
});

var METRIC_LABELS = {
    wall_ms: "Navigation time (ms)", ttfb_ms: "Time to first byte (ms)", dom_content_loaded_ms: "DOM content loaded (ms)",
    load_ms: "Page load (ms)", transfer_bytes: "Document size (bytes)", resources: "Sub-resources (count)",
    resource_bytes: "Sub-resource size (bytes)"
};

function cell(text, className){
    var $c = $("<td></td>").text(text === null || text === undefined ? "" : text);
    if(className) $c.addClass(className);
    return $c;
}

function fmt(value){
    return value === null || value === undefined ? "" : value;
}

function statusClass(status){
    return status == "done" ? "text-success" : (status == "failed" ? "text-danger" : "text-muted");
}

function startLoadtest(){
    $("#lt-error").text("");
    $.ajax({"type":"POST", "url":_apiEndPoint, "dataType":"json",
        "data":{action:"START_LOADTEST", url:$("#lt-url").val(), concurrency:$("#lt-concurrency").val(),
                duration:$("#lt-duration").val()},
        "success":function(response){ _selectedRun = response.id; refreshLoadtests(); },
        "error":function(xhr){ $("#lt-error").text((xhr.responseJSON && xhr.responseJSON.message) || "Could not start the test."); }});
    return false;
}

function cancelLoadtest(){
    if(!_selectedRun) return false;
    $.ajax({"type":"POST", "url":_apiEndPoint, "dataType":"json", "data":{action:"CANCEL_LOADTEST", id:_selectedRun},
            "complete":refreshLoadtests});
    return false;
}

function refreshLoadtests(){
    $.ajax({"type":"POST", "url":_apiEndPoint, "dataType":"json", "data":{action:"GET_LOADTESTS"},
        "success":function(response){
            $("#worker-warning").css("display", response.worker.running ? "none" : "");
            var hosts = response.allowed_hosts.length ? response.allowed_hosts.join(", ") : "none configured (OBSERVER_LOADTEST_ALLOWED_HOSTS)";
            $("#lt-hint").text("Allowed targets: " + hosts + ". Up to " + response.limits.max_concurrency +
                               " users and " + response.limits.max_duration + " seconds.");
            var $body = $("#lt-runs tbody").empty();
            response.runs.forEach(function(run){
                var p95 = run.result && run.result.summary.load_ms ? run.result.summary.load_ms.p95 : "";
                var $row = $("<tr style='cursor:pointer'></tr>").append(cell(new Date(run.created * 1000).toLocaleTimeString()),
                    cell(run.params.url), cell(run.params.concurrency), cell(run.params.duration),
                    cell(run.status, statusClass(run.status)), cell(run.result ? run.result.loads : (run.progress ? run.progress.loads : "")),
                    cell(p95));
                $row.on("click", function(){ _selectedRun = run.id; refreshLoadtests(); });
                if(run.id == _selectedRun) $row.addClass("active");
                $body.append($row);
            });
            if(_selectedRun) showDetail(_selectedRun);
        }});
}

function showDetail(id){
    $.ajax({"type":"POST", "url":_apiEndPoint, "dataType":"json", "data":{action:"GET_LOADTEST", id:id},
        "success":function(run){
            if(id != _selectedRun) return;
            _lastDetail = run;
            $("#lt-detail").css("display", "");
            $("#lt-detail-title").text(run.params.url);
            var info = run.params.concurrency + " user(s), " + run.params.duration + " s - " + run.status;
            if(run.status == "running" && run.progress) info += " (" + run.progress.elapsed + " s, " + run.progress.loads + " loads, " + run.progress.errors + " errors)";
            if(run.error) info += " - " + run.error;
            $("#lt-detail-info").text(info);
            $("#lt-cancel").css("display", (run.status == "running" || run.status == "queued") ? "" : "none");
            $("#lt-csv").css("display", run.result ? "" : "none");
            var $body = $("#lt-summary tbody").empty();
            var extra = $("#lt-extra").empty();
            if(run.result){
                Object.keys(METRIC_LABELS).forEach(function(metric){
                    var s = run.result.summary[metric];
                    if(!s) return;
                    $body.append($("<tr></tr>").append(cell(METRIC_LABELS[metric]), cell(s.count), cell(s.min), cell(s.mean),
                        cell(s.p50), cell(s.p95), cell(s.max)));
                });
                var codes = Object.keys(run.result.status_codes).map(function(k){ return k + ": " + run.result.status_codes[k]; }).join(", ");
                var errors = Object.keys(run.result.errors).filter(function(k){ return run.result.errors[k] > 0; })
                    .map(function(k){ return k + ": " + run.result.errors[k]; }).join(", ") || "none";
                extra.append($("<p></p>").text("Loads: " + run.result.loads + " (" + run.result.ok_loads + " ok), " +
                    run.result.throughput_per_s + " per second. Status codes: " + codes + ". Errors: " + errors + "."));
                if(run.result.blocked_requests > 0){
                    extra.append($("<p class='text-warning'></p>").text(run.result.blocked_requests +
                        " request(s) to hosts that are not allowed were blocked: " + run.result.blocked_hosts.join(", ")));
                }
            }
        }});
}

function downloadLoadtestCsv(){
    if(!_lastDetail || !_lastDetail.result) return false;
    var rows = [];
    Object.keys(METRIC_LABELS).forEach(function(metric){
        var s = _lastDetail.result.summary[metric];
        if(s) rows.push({metric: METRIC_LABELS[metric], loads: s.count, min: s.min, mean: s.mean, p50: s.p50, p95: s.p95, max: s.max});
    });
    $.ajax({"type":"POST", "url":_apiEndPoint, "dataType":"json",
        "data":{action:"SAVE_TEST_RESULT_AS", file_type:"CSV", test_name:"loadtest", jsonData:JSON.stringify({live_data: rows})},
        "success":function(response){ window.location = _filesEndPoint + "/" + response.file_name; }});
    return false;
}

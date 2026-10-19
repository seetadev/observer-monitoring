$(document).ready(function(){
    refreshStatus();
    setInterval(refreshStatus, 5000);
});

function formatTime(epochSeconds){
    return epochSeconds ? new Date(epochSeconds * 1000).toLocaleString() : "";
}

function textCell(text, className){
    var $cell = $("<td></td>").text(text);
    if(className) $cell.addClass(className);
    return $cell;
}

function stateClass(state){
    return state == "up" ? "text-success" : (state == "down" ? "text-danger" : "text-muted");
}

function refreshStatus(){
    $.ajax({"type":"POST", "url":_apiEndPoint, "dataType":"json", "data":{action:"GET_STATES"},
        "success":function(response){
            $("#monitor-warning").css("display", response.monitor.running ? "none" : "");
            var $body = $("#states-table tbody").empty();
            response.states.forEach(function(item){
                $body.append($("<tr></tr>").append(textCell(item.kind), textCell(item.name),
                    textCell(item.state, stateClass(item.state)), textCell(formatTime(item.since)),
                    textCell(item.detail || "")));
            });
        }});
    $.ajax({"type":"POST", "url":_apiEndPoint, "dataType":"json", "data":{action:"GET_EVENTS"},
        "success":function(response){
            var $body = $("#events-table tbody").empty();
            response.events.forEach(function(item){
                $body.append($("<tr></tr>").append(textCell(formatTime(item.time)),
                    textCell(item.event, stateClass(item.event)), textCell(item.kind), textCell(item.name),
                    textCell(item.detail || "")));
            });
        }});
}

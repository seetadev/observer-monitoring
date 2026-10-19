$(document).ready(pageInit);

// Every request carries the session cookie and, for non-GET requests, the XSRF token.
$.ajaxSetup({
    xhrFields: {withCredentials: true},
    beforeSend: function(xhr, settings){
        if(settings.type && settings.type.toUpperCase() !== "GET"){
            var token = getCookie("_xsrf");
            if(token) xhr.setRequestHeader("X-XSRFToken", token);
        }
    }
});
$(document).ajaxError(function(event, xhr){
    if(xhr.status === 401 && window.location.pathname !== "/login"){
        window.location = "/login?next=" + encodeURIComponent(window.location.pathname);
    }
});

function logout(){
    $.ajax({"type":"POST", "url":_apiEndPoint, "data":{action:"LOGOUT_ADMIN"},
            "complete":function(){ window.location = "/login"; }});
    return false;
}

_common = {"currentMachine":document.cookie};
_apiEndPoint = "http://"+window.location.hostname+":9000/api";
_liveEndPoint = "http://"+window.location.hostname+":9002/live";
_filesEndPoint = "http://"+window.location.hostname+":9000/files";
_computeEndPoint = "http://"+window.location.hostname+":9003/compute";
loc = window.location.pathname;
_currentPage= loc.substring(loc.lastIndexOf('/')+1,loc.length);


function pageInit(){
    remoteMachinesUpdater();
    if(document.cookie == null){
        document.cookie = "machine-name=remote-laptop";
        _common = {"currentMachine":getCookie("machine-name")};
    }
    var headerMachineName = getCookie("machine-name");
    if(headerMachineName == null)
        headerMachineName = "";
    $("#currentMachineHeader").empty()
        .append($("<i style=\"margin-right:5px\" class=\"fa fa-server\" aria-hidden=\"true\"></i>"))
        .append(document.createTextNode(" " + headerMachineName));
    setPageActive();
}

function getCookie(name) {
  var value = "; " + document.cookie;
  var parts = value.split("; " + name + "=");
  if (parts.length == 2) return parts.pop().split(";").shift();
}

function setPageActive(){
    if(_currentPage!=null && _currentPage!=""){
        var navList = $("#sidebar-nav").children();
        for(var i=0 ; i<navList.length ; i++){
            if($(navList[i]).attr("id")==_currentPage+"-nav"){
                $(navList[i]).addClass("active");
                break;
            }
        }
    }
}

function remoteMachinesUpdater(){
    $.ajax({
        "type":"POST",
        "data" : {action:"GET_REMOTE_MACHINES"},
        "dataType" : "json",
        "success" : function(response){
                        loadRemoteMachines((JSON.parse(response.remoteMachines))["machines"]);
                        setTimeout(function(){
                            remoteMachinesUpdater();
                        },1000);
                    },
        "error" : function(response){ },
        "url" : _apiEndPoint
    });
}


function loadRemoteMachines(remoteMachines){
    // Up/down is decided by the server (monitor process); the browser only displays it.
    $("#machines-remote").empty();
    var current = getCookie("machine-name");
    var selected = null;
    for(var i=0 ; i<remoteMachines.length ; i++){
        var status = remoteMachines[i].state == "up" ? "online" : "offline";
        if(remoteMachines[i].name == current){
            selected = remoteMachines[i];
        }
        $("#machines-remote").append(getListItem(remoteMachines[i].name, remoteMachines[i].ip, status));
    }

    if(selected !== null){
        $("#sys-offline-msg").css("display", selected.state == "up" ? "none" : "");
    }
}

function getListItem(name,ip,status){
    // Names and addresses come from the server: they are inserted as text, never as markup.
    var $listItemElement = $("<a class=\"list-group-item\"></a>");
    $listItemElement.on("click", function(){ changeMachine(name, ip); });
    $listItemElement.append($("<b style=\"padding-right:10px\"></b>").text(name));

    if(status=="online")
        $listItemElement.append($("<i style=\"float:right\" class=\"fa fa-exchange\" aria-hidden=\"true\"></i>"));
    else
        $listItemElement.append($("<i style=\"float:right; visibility:hidden;\" class=\"fa fa-exchange\" aria-hidden=\"true\"></i>"));
    if(getCookie("machine-name")==name)
        $listItemElement.addClass("active");

    $listItemElement.append($("<div style=\"padding-right:10px;\"></div>").text(ip));
    return $listItemElement;
}

function changeMachine(machineName,machineIP){
    document.cookie = "machine-name="+encodeURIComponent(machineName)+"; path=/; SameSite=Lax";
    location.reload();
}

function showIdle(status){
    if(status == true) // Show idle
    {
        $("#main-area").css('display','none');
        $("#idle-area").show();
    }
    else{   // Remove idle
        $("#idle-area").css('display','none');
        $("#main-area").show();
    }
}

function roundOff(num){
    return (Math.round(num*100))/100;
}

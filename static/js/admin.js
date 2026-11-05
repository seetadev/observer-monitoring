
$(document).ready(function(){
    populateMachinesList();
});

function populateMachinesList(){
    $.ajax({
        "type":"POST",
        "data" : {action:"GET_REMOTE_MACHINES"},
        "dataType" : "json",
        "success" : function(response){
                        loadRemoteMachines_Admin((JSON.parse(response.remoteMachines))["machines"]);
                    },
        "error" : function(response){ },
        "url" : _apiEndPoint
    });
}

function loadRemoteMachines_Admin(machinesList){
    $("#machines-list").empty();
    for(var i=0 ; i<machinesList.length ; i++){
        var $item = $("<div class=\"admin-machine-list-item\"></div>").attr("data-machine", machinesList[i]['name']);
        $item.append(document.createTextNode(machinesList[i]['name']));
        $item.append($("<i class=\"fa fa-trash machine-list-item-del-btn\" aria-hidden=\"true\"></i>")
            .on("click", function(){ deleteMachine(this); }));
        $("#machines-list").append($item);
    }
}

function deleteMachine(el){
    var listElement = $(el).parent();
    var machineName = $(listElement).attr("data-machine");
    $.ajax({
        "type":"POST",
        "data" : {action:"DELETE_REMOTE_MACHINE",machine_id:machineName},
        "dataType" : "json",
        "success" : function(response){
                        if(response.status == "True"){
                            alert("Remote machine deleted successfully.");
                            listElement.remove();
                        }
                    },
        "error" : function(response){ },
        "url" : _apiEndPoint
    });
}


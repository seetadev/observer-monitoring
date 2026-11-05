function xsrfToken() {
    var parts = ("; " + document.cookie).split("; _xsrf=");
    return parts.length == 2 ? parts.pop().split(";").shift() : "";
}

function safeNext(value) {
    return (value && value.charAt(0) == "/" && value.charAt(1) != "/" && value.indexOf("\\") < 0) ? value : "/live";
}

$("#login-form").on("submit", function (event) {
    event.preventDefault();
    $("#login-error").text("");
    $.ajax({
        type: "POST",
        url: "/api",
        dataType: "json",
        data: {action: "LOGIN_ADMIN", password: $("#password-field").val()},
        headers: {"X-XSRFToken": xsrfToken()},
        success: function () {
            window.location = safeNext($("#next-url").val());
        },
        error: function (response) {
            var message = response.responseJSON && response.responseJSON.message;
            $("#login-error").text(message || "Sign-in failed.");
            $("#password-field").val("");
        }
    });
});

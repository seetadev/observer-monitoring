// Where the page finds the dashboard API and the live / compute services.
//  * Opened on the dashboard's own port (9000): the services are on their own ports of the same host
//    (the Compose / bare-metal layout without a proxy).
//  * Anywhere else (behind the TLS reverse proxy): everything is same-origin, the proxy routes the paths.
function observerEndpoints(loc) {
    if (loc.port === "9000") {
        var base = loc.protocol + "//" + loc.hostname;
        return {api: base + ":9000/api", live: base + ":9002/live", compute: base + ":9003/compute",
                files: base + ":9000/files"};
    }
    return {api: loc.origin + "/api", live: loc.origin + "/_live", compute: loc.origin + "/_compute",
            files: loc.origin + "/files"};
}

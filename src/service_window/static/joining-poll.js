/* Joining-page poll. Loaded by setup/joining.html. Runnable under node. */
(function (root) {
  var RECONNECT = "Reconnect to this unit's Wi-Fi, or check BlueSignal Cloud.";
  var MAX_TRIES = 90;

  function headerType(r) {
    if (!r) return "";
    if (r.headers && typeof r.headers.get === "function") {
      return r.headers.get("Content-Type") || "";
    }
    return r.contentType || "";
  }

  function droppedHotspot(r) {
    if (!r || r.status === 403) return true;
    return String(headerType(r)).indexOf("json") === -1;
  }

  function afterPoll(j, tries, maxTries) {
    var limit = maxTries == null ? MAX_TRIES : maxTries;
    if (!j) return tries + 1 < limit ? "retry" : "reconnect";
    if (j.state === "ok") return "ok";
    if (j.state === "fail") return "fail";
    return tries + 1 < limit ? "retry" : "reconnect";
  }

  var api = {
    RECONNECT: RECONNECT,
    MAX_TRIES: MAX_TRIES,
    droppedHotspot: droppedHotspot,
    afterPoll: afterPoll,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.WQM1JoiningPoll = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);

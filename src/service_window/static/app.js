/* WQM-1 Service Window — client-side JavaScript */

// Auto-refresh dashboard every 30 seconds
(function() {
    if (window.location.pathname === '/') {
        setTimeout(function() { window.location.reload(); }, 30000);
    }
})();

// Copy buttons ([data-copy]) — the Service Window is served over plain HTTP
// on the LAN, so navigator.clipboard (secure-context only) usually isn't
// available; fall back to a hidden textarea + execCommand.
(function () {
    function legacyCopy(text) {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        var ok = false;
        try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
        document.body.removeChild(ta);
        return ok;
    }

    document.addEventListener('click', function (ev) {
        var btn = ev.target.closest('[data-copy]');
        if (!btn) return;
        var text = btn.getAttribute('data-copy');
        var done = function (ok) {
            var original = btn.textContent;
            btn.textContent = ok ? 'Copied ✓' : 'Select + copy manually';
            setTimeout(function () { btn.textContent = original; }, 2000);
        };
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text).then(function () { done(true); },
                function () { done(legacyCopy(text)); });
        } else {
            done(legacyCopy(text));
        }
    });
})();

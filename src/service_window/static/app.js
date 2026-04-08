/* WQM-1 Service Window — client-side JavaScript */

// Auto-refresh dashboard every 30 seconds
(function() {
    if (window.location.pathname === '/') {
        setTimeout(function() { window.location.reload(); }, 30000);
    }
})();

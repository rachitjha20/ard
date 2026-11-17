// Applies a saved colour scheme before first paint. With no saved value the
// page follows the system preference.
try {
  var saved = localStorage.getItem("color-scheme");
  if (saved === "light" || saved === "dark") {
    document.querySelector('meta[name="color-scheme"]').content = saved;
  }
} catch (e) { /* storage blocked: follow the system */ }

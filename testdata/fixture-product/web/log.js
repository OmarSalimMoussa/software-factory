// The page's one log module: every message goes to the console and to the server's error report.
export function warn(message) {
  console.warn(message);
  navigator.sendBeacon("/api/log", JSON.stringify({ level: "warning", message }));
}

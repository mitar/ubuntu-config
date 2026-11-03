// Chromium binds no key to Exit on Linux. On Ctrl+Q this starts the native messaging host local.chromium_quit, which
// ends Chromium with SIGTERM, and Chromium then saves its whole session as at the end of the desktop session. The host
// exits without answering, so the error sendNativeMessage reports for it is expected and ignored.
chrome.commands.onCommand.addListener((command) => {
  if (command === "quit") {
    chrome.runtime.sendNativeMessage("local.chromium_quit", {}).catch(() => {});
  }
});

#!/bin/sh
#
# Installed as /usr/local/libexec/chromium-quit by configure-gnome-desktop.sh. Chromium starts it as the native
# messaging host local.chromium_quit when Ctrl+Q is pressed, for the extension in chromium-extensions/quit-shortcut/.
#
# Ends the Chromium which started it with SIGTERM, which Chromium handles like the end of the desktop session: it saves
# its whole session and quits, so all its windows return at the next start. Chromium binds no key to Exit on Linux, an
# extension can neither run Exit nor open chrome://quit, and closing the windows one by one leaves only the last one in
# the session. It reads nothing and answers nothing, and acts only when its parent is Chromium, so started any other
# way it does nothing.

if [ "$(readlink "/proc/$PPID/exe")" = /usr/lib/chromium/chromium ]; then
  kill -TERM "$PPID"
fi

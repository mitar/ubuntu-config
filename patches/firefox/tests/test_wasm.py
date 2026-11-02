"""Exercise Firefox's six wasm sandboxed libraries through Marionette, in headless mode. See README.

Usage: test_wasm.py FIREFOX OUT_DIR

OUT_DIR is removed first, then receives the profile, Firefox's output in firefox.log and the Ogg file the test
plays. Prints one line for each library, with what it produced or the error it raised.
"""

import os
import shutil
import subprocess
import sys

# No __pycache__ is left in the repository, from where it would also be installed with the patches.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import driver  # noqa: E402

# Both from the fonts-sil-andika package. Andika has Graphite tables, so laying out text in it goes through
# graphite, and loading the WOFF2 file goes through woff2.
WOFF2_FONT = "/usr/share/fonts-sil-andika/woff2/Andika-Regular.woff2"
GRAPHITE_FONT = "/usr/share/fonts/truetype/andika/Andika-Regular.ttf"

# The Graphite text includes an eng and a nasalized open e, which Andika renders through its Graphite rules.
SCRIPT = r"""
let [resolve] = arguments;
(async () => {
const out = {};
const win = Services.wm.getMostRecentWindow("navigator:browser");
const doc = win.document;
const HTML = "http://www.w3.org/1999/xhtml";
try {
  const xml = new win.DOMParser().parseFromString("<root><item a='1'>x</item></root>", "text/xml");
  out.expat = xml.documentElement.nodeName + ", parsererror elements: " + xml.getElementsByTagName("parsererror").length;
} catch (e) { out.expat = "error " + e; }
try {
  const sc = Cc["@mozilla.org/spellchecker/engine;1"].getService(Ci.mozISpellCheckingEngine);
  const dicts = sc.getDictionaryList();
  const dict = dicts.includes("en-US") ? "en-US" : dicts[0];
  if (typeof sc.setDictionaries == "function") { await sc.setDictionaries([dict]); } else { sc.dictionaries = [dict]; }
  out.hunspell = dict + ": hello=" + sc.check("hello") + " helo=" + sc.check("helo");
} catch (e) { out.hunspell = "error " + e; }
try {
  const bytes = await IOUtils.read("%(ogg)s");
  const ctx = new win.OfflineAudioContext(1, 44100, 44100);
  const buf = await ctx.decodeAudioData(bytes.buffer);
  out.ogg = "decoded " + buf.duration.toFixed(2) + " s";
} catch (e) { out.ogg = "error " + e; }
try {
  const bytes = await IOUtils.read("%(woff2)s");
  const face = new win.FontFace("woff2test", bytes.buffer);
  await face.load();
  out.woff2 = face.status;
} catch (e) { out.woff2 = "error " + e; }
try {
  const bytes = await IOUtils.read("%(graphite)s");
  const face = new win.FontFace("graphitetest", bytes.buffer);
  await face.load();
  doc.fonts.add(face);
  const span = doc.createElementNS(HTML, "span");
  span.style.fontFamily = "graphitetest";
  span.style.fontSize = "40px";
  span.textContent = "Graphite shaping ffi fl \u014b \u025b\u0303";
  doc.documentElement.appendChild(span);
  out.graphite = face.status + ", laid out width " + span.getBoundingClientRect().width.toFixed(1);
  span.remove();
} catch (e) { out.graphite = "error " + e; }
try {
  const audio = doc.createElementNS(HTML, "audio");
  audio.src = "file://%(ogg)s";
  audio.preservesPitch = true;
  audio.playbackRate = 1.5;
  doc.documentElement.appendChild(audio);
  await audio.play();
  await new Promise(r => win.setTimeout(r, 1500));
  out.soundtouch = "played to " + audio.currentTime.toFixed(2) + " s at rate " + audio.playbackRate;
  audio.pause();
  audio.remove();
} catch (e) { out.soundtouch = "error " + e; }
resolve(out);
})().catch(e => resolve({fatal: String(e)}));
"""


def main():
    firefox, out_dir = sys.argv[1:3]
    out_dir = os.path.abspath(out_dir)
    profile = os.path.join(out_dir, "profile")
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(profile)

    # A two second tone, which is long enough to still play after the 1.5 s the test waits at rate 1.5.
    ogg = os.path.join(out_dir, "test.ogg")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", "libvorbis", ogg], check=True)

    driver.write_prefs(profile, 0)
    with open(os.path.join(profile, "user.js"), "a") as f:
        f.write('user_pref("media.cubeb.force_null_context", true);\n')
        f.write('user_pref("media.autoplay.default", 0);\n')
    os.environ["MOZ_HEADLESS"] = "1"
    proc, m = driver.start(firefox, profile, os.path.join(out_dir, "firefox.log"))
    script = SCRIPT % {"ogg": ogg, "woff2": WOFF2_FONT, "graphite": GRAPHITE_FONT}
    try:
        result = m.script(script)
        for key in ("expat", "hunspell", "ogg", "woff2", "graphite", "soundtouch"):
            print("  %-10s %s" % (key, result.get(key)))
        if "fatal" in result:
            print("  FATAL: %s" % result["fatal"])
    except (ConnectionError, OSError, RuntimeError) as e:
        print("  FAILED: %s" % e)
    alive = driver.alive_after(proc, 2)
    print("  still running afterwards: %s" % alive)
    print("  exit status %s" % driver.quit(m, proc))


if __name__ == "__main__":
    main()

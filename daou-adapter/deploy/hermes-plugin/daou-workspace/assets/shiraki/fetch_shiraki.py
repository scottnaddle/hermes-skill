#!/usr/bin/env python3
"""Download shiraki.design slide-style YAMLs listed in index.json.

YAML bodies are third-party content (shiraki.design) and are NOT bundled in
this repo. Run this once on the host that runs the daou-workspace plugin:

    python3 fetch_shiraki.py [--dest /opt/ubm-template-gate/shiraki] [--delay 1.0]

The plugin reads <dest>/NNN.yaml + <dest>/index.json (env DAOU_SHIRAKI_DIR,
default /opt/ubm-template-gate/shiraki). Existing files are skipped.
Please respect shiraki.design's terms of use.
"""
import argparse, html, json, os, re, shutil, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (compatible; hermes-skill shiraki fetch)"

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")

def extract_yaml(page):
    m = re.search(r'<pre[^>]*class="[^"]*yaml[^"]*"[^>]*>(.*?)</pre>', page, re.S)
    if not m:
        return None
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() + "\n"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=os.environ.get("DAOU_SHIRAKI_DIR", "/opt/ubm-template-gate/shiraki"))
    ap.add_argument("--delay", type=float, default=1.0)
    a = ap.parse_args()
    os.makedirs(a.dest, exist_ok=True)
    idx = json.load(open(os.path.join(HERE, "index.json"), encoding="utf-8"))
    ok = fail = 0
    for e in idx["styles"]:
        out = os.path.join(a.dest, e["yaml"])
        if os.path.exists(out) and os.path.getsize(out) > 0:
            ok += 1
            continue
        try:
            y = extract_yaml(get(e["source"]))
            if not y:
                raise ValueError("no <pre class=yaml> block")
            open(out, "w", encoding="utf-8").write(y)
            e["chars"] = len(y)
            ok += 1
        except Exception as ex:
            fail += 1
            print(f"skip {e['id']}: {ex}", file=sys.stderr)
        time.sleep(a.delay)
    # plugin index format: {"styles":[{id,name,category,type,ratio,thumb,yaml,chars}]}
    json.dump({"styles": idx["styles"]}, open(os.path.join(a.dest, "index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"done: {ok} ok, {fail} failed -> {a.dest}")
    return 1 if fail else 0

if __name__ == "__main__":
    sys.exit(main())

"""Download every source in data/sources.csv to raw/, and SEC XBRL company facts.

- SEC pages use the User-Agent from config.yaml (required by the SEC).
- Each file gets a SHA-256 hash in raw/manifest.csv, so later edits are detectable.
- --wayback finds the closest Internet Archive snapshot for each URL and records it.
  If a direct download fails, the script tries that snapshot.
- If a site blocks scripts, save the page by hand as raw/<doc_id>.html; verify.py uses it.

Run:  python scripts/fetch_sources.py [--force] [--only D01,D02] [--wayback] [--no-xbrl]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_config, path, read_csv  # noqa: E402

BROWSER_UA = "Mozilla/5.0 (research script; contact in config.yaml)"
MANIFEST_FIELDS = ["doc_id", "url", "fetched_at_utc", "method", "http_status", "content_type", "bytes", "sha256", "local_file", "wayback_url", "error"]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def ext_for(content_type: str, url: str) -> str:
    ct = (content_type or "").lower()
    if "pdf" in ct or url.lower().endswith(".pdf"):
        return ".pdf"
    if "json" in ct:
        return ".json"
    return ".html"


def is_sec(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("sec.gov")


def headers_for(url: str, cfg: dict) -> dict:
    ua = cfg["sec_user_agent"] if is_sec(url) else BROWSER_UA
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


def wayback_lookup(url: str, doc_date: str) -> str | None:
    ts = doc_date.replace("-", "") if doc_date and doc_date[:4].isdigit() else ""
    api = f"https://archive.org/wayback/available?url={quote(url, safe='')}" + (f"&timestamp={ts}" if ts else "")
    try:
        r = requests.get(api, timeout=30)
        snap = r.json().get("archived_snapshots", {}).get("closest")
        if snap and snap.get("available"):
            return snap["url"]
    except Exception:  # noqa: BLE001 - lookup is best effort
        return None
    return None


def raw_snapshot_url(wb_url: str) -> str:
    # https://web.archive.org/web/20260811000000/https://... -> .../20260811000000id_/https://...
    parts = wb_url.split("/web/", 1)
    if len(parts) != 2:
        return wb_url
    ts, rest = parts[1].split("/", 1)
    return f"{parts[0]}/web/{ts}id_/{rest}"


def load_manifest(p: Path) -> dict:
    if not p.exists():
        return {}
    with open(p, newline="", encoding="utf-8") as f:
        return {r["doc_id"]: r for r in csv.DictReader(f)}


def save_manifest(p: Path, rows: dict):
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS, lineterminator="\n")
        w.writeheader()
        for k in sorted(rows):
            w.writerow({f: rows[k].get(f, "") for f in MANIFEST_FIELDS})


def existing_file(raw: Path, doc_id: str) -> Path | None:
    for e in (".html", ".pdf", ".json", ".htm", ".txt"):
        p = raw / f"{doc_id}{e}"
        if p.exists():
            return p
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="download again even if a file exists")
    ap.add_argument("--only", default="", help="comma-separated doc IDs")
    ap.add_argument("--wayback", action="store_true", help="look up Internet Archive snapshots; use them if direct download fails")
    ap.add_argument("--no-xbrl", action="store_true", help="skip SEC XBRL company facts")
    args = ap.parse_args()

    cfg = load_config()
    if "example.com" in cfg["sec_user_agent"]:
        print("Set sec_user_agent in config.yaml (name and email) before you download from the SEC.")
        return 2
    raw = path(cfg, "raw")
    raw.mkdir(parents=True, exist_ok=True)
    mpath = raw / "manifest.csv"
    manifest = load_manifest(mpath)
    only = {x.strip() for x in args.only.split(",") if x.strip()}

    for s in read_csv("sources.csv"):
        did, url = s["doc_id"], s["url"].strip()
        if only and did not in only:
            continue
        if not url.startswith("http"):
            continue
        have = existing_file(raw, did)
        if have and not args.force:
            m = manifest.get(did, {"doc_id": did, "url": url, "method": "manual"})
            m.update({"sha256": sha256(have), "bytes": have.stat().st_size, "local_file": have.name})
            manifest[did] = m
            print(f"{did}: exists ({have.name}), hash recorded")
            continue
        rec = {"doc_id": did, "url": url, "fetched_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        if args.wayback:
            rec["wayback_url"] = wayback_lookup(url, s.get("doc_date", "")) or ""
        tries = [("direct", url)]
        if rec.get("wayback_url"):
            tries.append(("wayback", raw_snapshot_url(rec["wayback_url"])))
        for method, u in tries:
            try:
                r = requests.get(u, headers=headers_for(url, cfg), timeout=60)
                rec.update({"method": method, "http_status": r.status_code, "content_type": r.headers.get("Content-Type", "")})
                if r.status_code == 200 and len(r.content) > 500:
                    out = raw / f"{did}{ext_for(rec['content_type'], url)}"
                    out.write_bytes(r.content)
                    rec.update({"bytes": len(r.content), "sha256": sha256(out), "local_file": out.name, "error": ""})
                    print(f"{did}: {method} OK -> {out.name}")
                    break
                rec["error"] = f"HTTP {r.status_code}"
            except Exception as e:  # noqa: BLE001
                rec.update({"method": method, "error": f"{type(e).__name__}: {e}"})
            time.sleep(0.2 if is_sec(u) else 1.0)
        else:
            print(f"{did}: FAILED ({rec.get('error')}). Save the page by hand as raw/{did}.html and run again.")
        manifest[did] = rec
        time.sleep(0.2 if is_sec(url) else 1.0)

    if not args.no_xbrl:
        cik = cfg["company"]["cik"].zfill(10)
        xurl = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        (raw / "xbrl").mkdir(exist_ok=True)
        try:
            r = requests.get(xurl, headers={"User-Agent": cfg["sec_user_agent"]}, timeout=60)
            r.raise_for_status()
            out = raw / "xbrl" / "companyfacts.json"
            out.write_bytes(r.content)
            manifest["XBRL"] = {"doc_id": "XBRL", "url": xurl, "fetched_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                "method": "direct", "http_status": r.status_code, "content_type": r.headers.get("Content-Type", ""),
                                "bytes": len(r.content), "sha256": sha256(out), "local_file": "xbrl/companyfacts.json"}
            print("XBRL company facts: OK")
        except Exception as e:  # noqa: BLE001
            print(f"XBRL company facts: FAILED ({e})")
    save_manifest(mpath, manifest)
    print(f"manifest: {mpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

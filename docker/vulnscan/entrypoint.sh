#!/bin/sh
# Dispatches to one of the bundled tools by name, e.g.:
#   docker run --rm hackdeepwiki-vulnscan nmap -sT -Pn --top-ports 50 example.com
#   docker run --rm hackdeepwiki-vulnscan nikto -h https://example.com
#   docker run --rm hackdeepwiki-vulnscan whatweb -a 1 https://example.com
#   docker run --rm hackdeepwiki-vulnscan testssl.sh --jsonfile-pretty /data/output/out.json example.com
#   docker run --rm hackdeepwiki-vulnscan nuclei -u https://example.com -jsonl
#
# No persistent server -- the Python orchestrator (api/web_vuln_scanner/
# docker_tools.py) runs one `docker run --rm ...` per tool per scan and reads
# stdout/the mounted /data/output volume, matching the "one scan, one
# result" shape the rest of HackDeepWiki's scanners use.
set -e
exec "$@"

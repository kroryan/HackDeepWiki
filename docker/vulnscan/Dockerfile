# HackDeepWiki vuln-scan toolkit -- a lean, purpose-built image containing
# only the tools api/web_vuln_scanner/docker_tools.py actually invokes
# (nmap, nikto, whatweb, testssl.sh, nuclei, subfinder+dnsx, httpx, ffuf,
# dalfox, wpscan, gitleaks, semgrep), plus the code-scan pass
# api/vuln_scanner/orchestrator.py opts into (gitleaks, semgrep).
#
# This intentionally does NOT carry RedAmon kali-sandbox's full offensive
# arsenal (Metasploit, exploitdb, John/Hashcat/Hydra, sqlmap, AD/Kerberos/
# BloodHound tooling, Windows/Linux priv-esc payload staging, GraphQL/SSTI/
# JWT/deserialization exploitation kits, cloud SDKs, Playwright+Chromium,
# ...) -- none of that is ever called by this project's scanner code, and it
# was the overwhelming majority of a previous ~15GB build of this image.
# Still a real Kali base, though: `docker exec` in and `apt install`/`go
# install`/`pip install` whatever else you need, same as any other Kali box.
#
# No MCP servers -- HackDeepWiki's Python backend invokes tools directly as
# one-shot subprocesses (docker run --rm ...), not as long-running SSE/HTTP
# servers, since a wiki security scan is "one scan, one result" rather than
# an interactive agent session.

# ---------------------------------------------------------------------------
# Stage 1: build the handful of Go-based scanners this toolkit needs.
# Building in a separate stage and copying only the resulting binaries into
# the final image (stage 2) keeps the Go toolchain + module cache (several
# hundred MB) out of what actually ships.
# ---------------------------------------------------------------------------
FROM golang:1-bookworm AS builder

RUN printf '#!/bin/sh\nmax=5; n=0; until "$@"; do n=$((n+1)); [ $n -ge $max ] && exit 1; echo "Retry $n/$max ..."; sleep $((n*5)); done\n' \
    > /usr/local/bin/retry && chmod +x /usr/local/bin/retry

# Source: https://github.com/projectdiscovery/nuclei (AGPL-3.0)
# Source: https://github.com/projectdiscovery/subfinder (MIT)
# Source: https://github.com/projectdiscovery/dnsx (MIT)
# Source: https://github.com/projectdiscovery/httpx (MIT)
# Source: https://github.com/ffuf/ffuf (MIT)
# Source: https://github.com/hahwul/dalfox (MIT)
# Source: https://github.com/gitleaks/gitleaks (MIT) -- module path is the
# old org (zricethezav), not the new org (gitleaks): known issue #1839.
RUN retry go install -v github.com/projectdiscovery/nuclei/v3/cmd/nuclei@latest \
    && retry go install -v github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest \
    && retry go install -v github.com/projectdiscovery/dnsx/cmd/dnsx@latest \
    && retry go install -v github.com/projectdiscovery/httpx/cmd/httpx@latest \
    && retry go install -v github.com/ffuf/ffuf/v2@latest \
    && retry go install -v github.com/hahwul/dalfox/v2@latest \
    && retry go install -v github.com/zricethezav/gitleaks/v8@latest

# ---------------------------------------------------------------------------
# Stage 2: the runtime image.
# ---------------------------------------------------------------------------
FROM kalilinux/kali-rolling:latest

LABEL maintainer="HackDeepWiki"
LABEL description="Lean vulnerability-scan toolkit -- only the tools api/web_vuln_scanner/docker_tools.py invokes"

ENV DEBIAN_FRONTEND=noninteractive

# Bootstrap CA certificates using the direct CDN mirror over HTTP first,
# then upgrade to HTTPS once ca-certificates is installed -- avoids the
# unreliable http.kali.org redirector that causes 403/timeout errors.
RUN echo "deb http://kali.download/kali kali-rolling main contrib non-free non-free-firmware" > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && update-ca-certificates \
    && echo "deb https://kali.download/kali kali-rolling main contrib non-free non-free-firmware" > /etc/apt/sources.list \
    && apt-get update

RUN echo 'Acquire::https::Redirect::Allowed "true";' > /etc/apt/apt.conf.d/99allow-redirect

RUN printf '#!/bin/sh\nmax=5; n=0; until "$@"; do n=$((n+1)); [ $n -ge $max ] && exit 1; echo "Retry $n/$max ..."; sleep $((n*5)); done\n' \
    > /usr/local/bin/retry && chmod +x /usr/local/bin/retry

# apt-installed tools, all directly invoked from docker_tools.py:
#   nmap                  -> run_nmap
#   nikto                 -> run_nikto
#   whatweb                -> run_whatweb
#   testssl.sh (binary: testssl) -> run_testssl
#   wpscan                 -> run_wpscan
# curl/wget/git/jq are general-purpose glue (used by later RUN steps and
# handy for interactive `docker exec` debugging).
RUN apt-get update && apt-get install -y --fix-missing \
    curl \
    wget \
    git \
    jq \
    python3 \
    python3-pip \
    python3-venv \
    nmap \
    nikto \
    whatweb \
    testssl.sh \
    wpscan \
    && rm -rf /var/lib/apt/lists/*

# Kali's wpscan apt package pulls in a chain that ends up with addressable
# 2.9.0 on the system, but one of wpscan's own dependencies
# (opt_parse_validator) pins addressable < 2.9 -- verified empirically:
# wpscan crashes on startup with Gem::MissingSpecError before ever making a
# request. Installing a compatible 2.8.x version alongside satisfies both
# (RubyGems activates whichever installed version satisfies each
# dependency's own constraint).
RUN gem install addressable -v '2.8.7'

# Go-based scanners built in stage 1 -- copied in directly, no Go toolchain
# in this image.
COPY --from=builder /go/bin/* /usr/local/bin/

# nuclei needs its community template pack to find anything.
RUN nuclei -update-templates || true

# semgrep (SAST, run_semgrep) -- isolated venv so it doesn't fight Kali's
# system Python packages.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:/usr/local/bin:${PATH}"
RUN retry pip install --no-cache-dir semgrep

# semgrep pulls in the Python `httpx` HTTP client library as a transitive
# dependency, which ships its own `httpx` console script into
# /opt/venv/bin -- verified empirically: since /opt/venv/bin comes first on
# PATH, that Python CLI silently shadowed the actual ProjectDiscovery `httpx`
# scanner binary run_httpx expects, so every `httpx` invocation ran the
# wrong tool entirely. Force the real one to win.
RUN ln -sf /usr/local/bin/httpx /opt/venv/bin/httpx

# SecLists wordlist run_ffuf fuzzes against.
RUN mkdir -p /usr/share/seclists/Discovery/Web-Content \
    && retry curl -fsSL https://raw.githubusercontent.com/danielmiessler/SecLists/47c02ddb16744a80c79fe19880096868c4c90cee/Discovery/Web-Content/common.txt \
        -o /usr/share/seclists/Discovery/Web-Content/common.txt

RUN mkdir -p /data/output && chmod 777 /data/output

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /data

# No EXPOSE / server ports -- this image is invoked as one-shot
# `docker run --rm hackdeepwiki-vulnscan <tool> <args>` per tool call, not
# as a long-running server.
ENTRYPOINT ["/entrypoint.sh"]

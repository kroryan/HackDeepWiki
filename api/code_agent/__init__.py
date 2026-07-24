"""Embedded opencode coding agent (Code Editing mode).

This package manages everything needed to run the opencode agent
(https://opencode.ai, GitHub anomalyco/opencode) *inside* HackDeepWiki:

  binary.py   -- locating/downloading the opencode binary (bundled, DATABASE
                 override, PATH, lazy download from GitHub releases)
  config.py   -- generating the per-repo opencode config (provider/model/keys
                 mapped from HackDeepWiki's own configuration, full-auto
                 permissions, and the wiki exposed via the inbound MCP server)
  manager.py  -- spawning and supervising one `opencode serve` process per
                 repository (cwd = the local clone), sessions, prompts, diffs
  context.py  -- the system prompt for code sessions (repo identity, wiki
                 structure of the OPEN wiki release, version-consistency check
                 between the wiki and the on-disk clone)
  events.py   -- consuming opencode's SSE /event stream and fanning it out to
                 chat/panel WebSocket subscribers
  routes.py   -- the FastAPI surface the frontend talks to
"""

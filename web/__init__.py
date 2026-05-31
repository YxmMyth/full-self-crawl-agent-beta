"""Helmsman — operator web console for the Full-Self-Crawl-Agent.

A read-mostly FastAPI console that launches missions as subprocesses, streams
recon + harvest progress over SSE, embeds the live Camoufox browser via a
same-origin noVNC reverse proxy (Phase 2), and surfaces human-assist (Phase 3)
— all over one SSH tunnel on the firewalled server.

Design: the agent runs in its OWN process (isolation — the console must not die
with the thing it observes). The console reads the run three ways (status.json
heartbeat [Phase 2], Postgres read-only, filesystem artifacts) and writes it two
narrow ways (gate stdin, human-assist response file).

See memory: frontend-helmsman-design. CLAUDE.md §一.
"""

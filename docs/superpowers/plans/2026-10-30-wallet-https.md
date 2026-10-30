# Password-free wallet HTTPS implementation plan

**Goal:** Make MetaYoshi exchange public chain data and locally signed transactions with ElectrumX over HTTPS, without Core credentials in the browser, with reproducible installation.

**Architecture:** A small independent aiohttp gateway forwards an explicit allowlist of public Electrum JSON-RPC calls to loopback Electrum TCP. Each browser uses a bounded, expiring connection session so connection-bound pagination remains valid. Nginx terminates trusted TLS on 443 and serves ACME HTTP challenges on 80. Certbot 5.4+ obtains and renews domain or short-lived public-IP certificates. MetaYoshi uses HTTP POST JSON-RPC and polls subscriptions; existing WebSocket transport remains supported.

**Tech stack:** Existing Python/aiohttp and TypeScript/Vitest, nginx, Certbot, systemd.

## Constraints
- No seed, private key, Core RPC password or server signing in the public transport.
- Core RPC and Electrum admin RPC stay private; the gateway cannot call either.
- HTTP transport never automatically retries a transaction broadcast; unknown submission results remain errors.
- HTTPS certificate validation stays enabled. Public IP certificates use Let's Encrypt shortlived, with automatic renewal.
- Preserve existing node, database, unrelated nginx configuration, credentials and custom service options during reinstall.
- Continue in the requested existing repositories; preserve unrelated LICENCE and wallet UI work. User delegated solution choice and previously requested main and GitHub push.
- Do not use SSH. Live server installation cannot be claimed until performed through an authorized accessible administration channel.

## Tasks
1. Gateway: add `electrumx/http_gateway.py`, public JSON-RPC POST `/api/v1/electrum`, GET `/api/v1/health`, optional GET `/api/v1/status` from local explorer. Fixed loopback upstream, bounded request size/concurrency/session pool and idle expiry; numeric protocol errors; explicit public method allowlist; CORS without cookies. Test with local TCP/HTTP servers, rejected administrative methods, error/retry semantics and pagination continuity.
2. Installer: add `contrib/install_wallet_https.py`, integrate `--wallet-https` into `contrib/install_stack.py`. It supports installed Docker image or venv, public IPv4/domain, private gateway 127.0.0.1:8090, existing Electrum port 50002, explorer status 8080. Manage gateway systemd unit, nginx site, ACME webroot, Certbot venv and renewal timer. Persist install options and support repeat installation/update. Test dry-run and staging, ownership/collision refusal and certificate command construction. Document provider firewall 80/443 requirement and exact install/reinstall commands.
3. Wallet: HTTP(S) JSON-RPC in client, credential-free URLs, omit credentials/cookies, bounded timeout, ID validation, no broadcast retries. Poll address/header/asset subscriptions every 15 seconds; close cancels timers/requests. Default `https://144.202.18.233/api/v1/electrum`, migrate only old built-in WSS default, preserve custom servers. Show synchronizing message when gateway returns retry error. Update server settings and docs; tests on real local HTTP server, polling, unsafe URLs, protocol errors, broadcasting once, migration; build Chromium.
4. Verify and review: run each repository's available full suite, static checks and builds; report any platform limitations. Fresh review of cross-repo protocol and installer, fix material issues. Commit only task files and push to existing upstream branches. Report exact server install step still needed if live administrative access remains unavailable.

## Progress
- Design chosen by agent under user's instruction to choose best solution.
- Initial live checks: HTTP explorer responds, index ~46.6%; WSS refused and HTTPS timed out. No SSH will be used.
- Implementation complete in both repositories. Fresh code review and scoped fix review found no remaining material issue. Fixed backend session capacity, reconnect token rotation, loopback HTTP binding, Docker name ownership checks, interrupted Certbot installation recovery and safe HTTP 503 handling.
- Wallet verification: 62 tests passed, 4 opt-in live tests skipped; lint and Chromium build passed. Existing build warnings are stale Browserslist data and a large bundle.
- Server verification: 674 passed, 53 skipped, 1 deselected after excluding the pre-existing Python 3.12 collection failure in `tests/server/test_mempool.py`, Windows-unavailable LevelDB compaction test, and one Groestl test lacking its optional native hash dependency. The unfiltered suite was attempted and is not claimed green.
- Linux verification: real nginx configuration test and systemd validation of all three generated units passed, including paths containing spaces.
- Cross-repository integration: actual Python gateway plus actual TypeScript wallet client passed mainnet handshake, balance conversion, real 15-second header polling and close/reconnect against a TCP Electrum fixture enforcing single-use `server.version`. No real transaction was broadcast.
- Live deployment pending console installation: browser automation could see the Vultr console window but stopped because it could not reliably identify the current browser URL. Latest public server status reports Core ready and `index_unavailable`; this requires server-side diagnosis independently of HTTPS setup. See `docs/wallet-https.md` for exact install and diagnostic commands.

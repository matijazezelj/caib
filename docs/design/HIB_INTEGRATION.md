> Status: design proposal (2026-11-26), not built. The open questions at the end are the owner's. Evidence tags: **[probed]** run against the code with made-up values; **[read]** from source; **[spec]** unverified.

# HIB integration and cross-project investigations (CAIB v0.2 P2, design only)


## 0. Findings that shape the design

1. **HIB offers none of CAIB's tools today.** HIB only passes tools marked `readOnlyHint` (`hib/src/workspace/mcp-upstream.ts`, `readOnlyTools`); CAIB's seven `add_tool` calls set no annotations (`agentapi/src/caib_agentapi/mcp_server.py`). HIB's client connected to CAIB's `/mcp`, listed 7 tools, offered 0. With the annotation injected by a launcher wrapper it offered 7, and a `list_rules` call worked **[probed]**.
2. **HIB's guard leaves much audit data readable**, and tokens depend on which server answered first and can join unrelated entities (§3) **[probed]**.
3. **The leaked-key pivot is missing.** CloudTrail's `accessKeyId` survives only in `raw`, which `search_text` excludes (`vector/modules/aws_cloudtrail.yaml`, `core/caib_core/loader.py`) **[read]**.
4. **SIB has no agent-safe query surface.** VictoriaLogs on `127.0.0.1:9428` takes raw LogsQL with no auth configured (`sib/storage/compose-vm.yaml`); the analysis API only analyses, lists history and reports health **[read]**.

## 1. What a cross-project investigation is

One analyst question, answered in one HIB workspace session, whose evidence needs both a control-plane record (CAIB: who called which cloud API) and a runtime record (SIB: what process did what on which box). The output is a UTC timeline in which every claim cites a source row, join keys are named with a confidence, and gaps are listed (truncation, retention, missing sensor).

| Scenario | CAIB side | SIB side | Join | Blocked today by |
|---|---|---|---|---|
| **A. Cloud key leaked from a host.** "Was the key read on `web-01` used?" | events by key or source IP after T; `get_alerts` | `Read AWS Credentials File` (`custom_rules.yaml`), `Cloud Metadata Access`, `AWS IMDS Network Access` (opt-in `cloud_rules.yaml`) | access key ID; instance ID in an assumed-role session name; egress IP; time | key ID not normalised; no host↔instance or egress-IP map |
| **B. Blind the logs, then act.** | `aws_cloudtrail.trail_tampering`, `gcp_audit.logging_sink_modified`, `azure_activity.diagnostic_settings_deleted`; same actor's later events | `Log File Tampering`, shells, Falco gaps on hosts that operator touched | time; source IP; a person (manual map) | cloud identity and Linux user are unrelated strings |
| **C. CI container escalates IAM.** | `gcp_audit.sa_key_created`, `iam_privileged_grant` | shell, `curl`, metadata access in a build container | caller IP = host egress; time; service account named in a command line | CAIB `src_ip` may be null (`gce-internal-ip`); no container identity in CAIB |
| **D. A coding agent reads credentials** (all three projects). | use of that credential afterwards | Falco `Coding Agent Read Credential Store` with `session=`/scope; `source:hib` events for the session | HIB session ID (SIB only); credential identity; time | same as A; session ID never reaches the cloud |

"No events" is a finding only after `module_health` and SIB's equivalent show data was arriving.

## 2. Integration shape

```
claude (HIB sensitive workspace) → hib MCP relay (stdio, per-session key)
  → hib daemon (holds tokens, guards results with one session vault)
      ├─ [mcp.caib] http://127.0.0.1:8088/mcp        (exists)
      └─ [mcp.sib]  http://127.0.0.1:<port>/mcp      (new: SIB agent API)
```

HIB already has this: trusted read-only MCP servers with a daemon-held token, a per-server tool allow-list, 60 calls/min, 16,000 characters of arguments, 60,000 characters of result (`hib/src/workspace/sessions.ts`) **[read]**. Router routes run the CLI with every tool disabled, so investigations must be **workspace sessions** (`hib` or `hib ask --workspace`) **[read]**.

```toml
# ~/.hib/config.toml
[mcp.caib]
url = "http://127.0.0.1:8088/mcp"
token_file = "~/.config/hib/caib.token"   # 0600, only the API token
read_only = true
server = "caib"
sensitive = true
tools = ["search_events","get_alerts","alert_summary","top_actors",
         "actor_activity","module_health","list_rules"]
```

CAIB's MCP server instructions (UTC, retention clamp, read `truncated`) never reach the agent: HIB keeps only `serverInfo.name`, and its relay returns none (`mcp-upstream.ts`, `hib-mcp.ts`) **[read]**. Tool descriptions do arrive, cut at 1,000 characters.

### Result size

CAIB's caps are applied first, HIB's 60,000-character cut second. `truncated` and `window` precede the rows in `search_events`, so they survive. `actor_activity` returns `by_action` **after** `events` (`core/caib_core/query.py:506-513`), so a long page can cut off the whole-window rollup while CAIB says `truncated: false`; HIB appends its own cut note, but the evidence is gone **[read; size threshold spec]**. Move `by_action` ahead of `events`, and tell the agent to use `limit ≤ 100`.

### SIB needs its own read-only API

Rejected: direct VictoriaLogs, or a generic VictoriaLogs/Grafana MCP (raw query language, none of the guarantees below). HIB's `mcp-grafana` recipe could be a Loki-stack stopgap **[spec]**.

Minimal surface, same core/two-transport pattern as `caib_core.query` (later tools are in §5):

| Tool | Filters | Cap (default) |
|---|---|---|
| `search_events` (Falco) | `q`, `hostname`, `rule`, `priority`, `tag`, `container_name`, `user`, `process`, `ip`, `since`, `until` | 500 (100) |
| `source_health` | none: newest event and 1h/24h counts per host and source | — |

How CAIB's guarantees carry over:

| Guarantee | SIB design |
|---|---|
| Read-only below the code | **Weaker than CAIB.** VictoriaLogs has no role equivalent as SIB deploys it. Put the API on a network that reaches only an auth proxy allowing `/select/logsql/*` (vmauth) **[spec: verify config]**. Port 9428 stays open on loopback for ingest and anyone local; document that. |
| No injectable queries | No LogsQL from the caller. Fixed fragments; values quoted by one tested escaping function; `priority`, `rule`, `tag` validated against enums or the loaded rule files, unknown values rejected. |
| Caps, truncation | Fetch limit+1; `truncated` and `window` before rows. Confirm result ordering: a bare `limit` may not mean newest-first **[spec]**. |
| Timeouts, lookback | 15s query timeout; clamp to `VICTORIALOGS_RETENTION_PERIOD` (default `168h`) and report it. |
| UTC | Parse to aware UTC. Do not reuse `VictoriaLogsClient` as is: it formats a naive local `datetime.now()` with a `Z` suffix (`sib/analysis/analyzer.py`), wrong wherever local time isn't UTC. |
| Structured output | Return `user.name`, `proc.cmdline`, `container.name`, `fd.*` as decoded fields, not JSON inside a string: HIB's key-based tokenising sees only real keys. |
| Raw opt-in, auth | `include_output` off by default; bearer token mandatory; Origin refused; tools annotated read-only. |

SIB also ships auth, syslog and Docker logs from fleet hosts (`vector.toml.j2`; rows carry `hostname` and `collector`, no `source`). sshd lines tie user, source IP and time, the best identity bridge for scenario B: make `auth_events` a later tool.

**Verify before coding:** the stored shape of `source:syscall` rows. Events arrive through Falcosidekick's Loki output; one dashboard regex-extracts `session=` from the message, while `analyzer.py` tries `json.loads(_msg)` and falls back to text **[spec until checked on a running stack]**.

### Entity keys and normalisation gaps

| Key | CAIB | SIB (Falco) | Gap and fix |
|---|---|---|---|
| Time | `event_time`, vendor time, UTC; delivery lags | event time, UTC | Search ±15 min minimum **[spec]**; state both windows; 90 days vs 7 days default |
| IP | `src_ip` (null for service domains); only `q` substring | `fd.*`, inside output | Add exact `src_ip` filter (`10.0.0.1` also matches `10.0.0.12`). Host's NAT/egress IP is recorded nowhere: needs inventory |
| Access key | `raw` only | normally absent | Add `attrs.access_key_id` |
| Instance | possibly the session name in `actor` ARN (AWS instance-profile convention) | none | Inventory file: `hostname, instance_id, egress_ips` |
| Host | none | `hostname`: fleet inventory name, or `sib-host` (`detection/compose.yaml`) | Same inventory; set `FALCO_HOSTNAME` |
| Identity | ARN, email, service account, token name | Linux `user.name` | No automatic join. Optional manual identity map; otherwise report "unlinked" |
| Container | none | id, name, image (rule-dependent) | One-sided |
| Target | `target` null for AWS | `fd.name`, command line | AWS target extraction later |
| HIB session | none | Falco `session=`/cgroup; `session_id` on `source:hib` | SIB-only key |

## 3. Security model

### Tokens

| Secret | Held by | Notes |
|---|---|---|
| `CAIB_AGENT_API_TOKEN` | CAIB API, HIB daemon | agent never sees it **[read]** |
| `SIB_AGENT_API_TOKEN` (new) | SIB API, HIB daemon | do not reuse `ANALYSIS_API_TOKEN`, which sits in Grafana links |
| `~/.hib/token`, per-session relay key | HIB | relay key lists and calls offered tools only |

Use HTTP MCP, so no database password leaves CAIB's host. Off-host goes through TLS.

Read-only layers: CAIB's `caib_agent_ro` role (strong), SIB path restriction (moderate), HIB allow-list plus `readOnlyHint`. None limits the CLI's own Bash, Write or web tools.

### What the guard did with made-up values **[probed]**

MCP path, standard level, name detection off, `guard.terms` empty.

| Tokenised | Left readable |
|---|---|
| `src_ip`, whole (key rule), IPv4 and IPv6 | `actor`: IAM user and role names, account IDs |
| `user.name` | `target`, `hostname`, internal FQDNs, `container_name` |
| IPv4 in any text | IPv6 in free text, unless already tokenised from a key |
| emails (not `example.com`/`.org`), also inside ARNs | instance IDs, Azure subscription and resource-group paths |
| AWS key IDs, GCP service-account emails, `key=value` secrets | account or bucket names in command lines |

Order effect: CAIB first, an IAM username in an ARN stays readable while SIB's `user.name` becomes a token, so the model may not link them. SIB first, only the ARN's user segment is tokenised, but an IPv6 address in SIB free text goes out raw and later appears as a token. IPv4 addresses did correlate across servers in either order, so joins can work through the guard.

**A shared token means equal strings, not the same entity [probed].** Linux `user=root` turned `/root/.aws/credentials` into `/[…-USER-NAME-1]/.aws/credentials` and, with SIB first, gave the `:root` of an AWS root ARN the same token. A Docker `172.17.0.2` and an equal VPC address share a token; CAIB first it is `SRC-IP-n`, losing the INTERNAL/`NETn` label. The agent must join identities and private addresses only with host or inventory evidence. HIB should exempt common accounts such as `root`.

### What may leave the machine (proposed policy)

- **Yes:** timestamps, actions, rule IDs, outcomes, severities, counts, tokens for identities and addresses.
- **Only after tokenising:** actor, target, account/subscription/project IDs, host, container and bucket names, command lines.
- **No:** CAIB `raw`, SIB full `output` by default, secrets, anything under a contractual ban.

Today's partial controls: `guard.terms` (company, account IDs, host names) and a **sensitive workspace** (`hib workspace sensitive --account claude@work`). Sensitive mode gives `--strict-mcp-config` (without it your own MCP servers also load), asks before reads and Bash, one account. Cost: no advisor, so no cross-provider review; no failover; no Codex. Use it anyway. Check the subscription's retention and training settings.

### Prompt injection

Attacker-influenced strings: CAIB `actor` (role session names), `target` (resource names), alert `entity`/`context`, `raw` (user agent, request parameters); SIB command lines, file and container names, user names, UDP syslog fields; plus anything a local process posts straight into VictoriaLogs. HIB tells the model results are data, which is a prompt, not a control (HIB's README says so). With read-only upstreams the likeliest harm is a **steered conclusion** ("benign, close it") or evidence skipped; next, use of the session's own shell or web tools.

| Mitigation | Where | When |
|---|---|---|
| Sensitive workspace; never "Always" on Bash/web | HIB config | now |
| Sensitive plus unattended `hib ask --workspace`: reads, Bash, web and subagents are denied (exit 3). Close to MCP-only today; your own allow rules for other tools still apply | exists | now |
| Same for interactive sessions: a profile denying Bash, Write, Edit, WebFetch, WebSearch outright | `claude-driver.ts` | next |
| Server-side switch to refuse `include_raw` (HIB allow-lists tools, not arguments) | CAIB, SIB | v0.2 |
| Cap long strings, strip control and zero-width characters | SIB API; CAIB later | v0.2 |
| Every conclusion cites rows; alert `event_count` re-queried; "benign" needs positive evidence | workspace instructions | v0.2 |
| Injection fixtures in the acceptance test | tests | v0.2 |

HIB's web view loads no images from model output **[read]**, closing a common exfiltration path.

## 4. Model and effort per step

Consistent with `docs/AGENT_API.md`, "Which Claude model to point at it". Starting points, unmeasured.

| Step | Model | Effort | AGENT_API row |
|---|---|---|---|
| 0. Health check and "what's open?" | Sonnet 5.5 | `low`–`medium` | interactive triage |
| 1. One alert, pivot into SIB | Sonnet 5.5 | `medium` | repeated investigation |
| 2. Bulk tagging of many events, under 100K prompt tokens | Haiku 5.5 | `medium` | bulk classifying |
| 3. Cross-project reconstruction (A–D) | Opus 5.5 | `medium`, then `high` | open-ended reconstruction |
| 4. Verify: re-query cited rows, look for counter-evidence | same as step 3 first | raise one level before changing model | "tune effort first" |
| Unsure | Opus 5.5 | `medium` | default |

What HIB can do today: **one model per session or run** (`hib ask --workspace . --model claude@work/opus "…" --json`). It passes `--model` but no effort flag (`claude-driver.ts`), so effort comes from your Claude settings **[read; inheritance spec]**. `opus`/`sonnet`/`haiku` aliases resolve to whatever your CLI version maps them to. Pass the model explicitly; do not let HIB's learned router choose. Step 2 subagents prompt in sensitive mode (and are denied unattended), so start single-model. Watch for safety-classifier fallback notices.

## 5. Phased plan

### v0.2 slice: scenario A end to end, interactive or unattended

**CAIB**
- `agentapi/src/caib_agentapi/mcp_server.py`: `annotations=ToolAnnotations(read_only_hint=True)` (from `mcp_types`) on all seven; serialises to `readOnlyHint` **[probed]**. Assert it in `tests/test_agent_mcp.py` and `test_agent_mcp_http.py`.
- `core/caib_core/query.py`: reorder `by_action`; exact `src_ip` filter, wired through `app.py` and `mcp_server.py`; optional setting to refuse `include_raw`. Tests: `test_query_core.py`, `test_agent_api.py`.
- `vector/modules/aws_cloudtrail.yaml`: `attrs.access_key_id` from `userIdentity.accessKeyId`; it enters `search_text` automatically. Update `modules/aws_cloudtrail/MAPPING.md` and fixtures. New events only; no backfill proposed.
- `docs/AGENT_API.md`: HIB recipe and the limits above.

**SIB**
- First, record real `source:syscall` rows as fixtures.
- New `agentapi/` (query core, HTTP + `/mcp`, `compose.yaml`, tests) with `search_events` and `source_health`; `Makefile` `install-agent-api`; `.env.example`; `docs/agent-api.md`; vmauth config.

**HIB**
- `src/config.ts`, `src/workspace/sessions.ts`: per-server `identity_keys` (`actor`, `target`, `hostname`, `container_name`) tokenised whole, or better an ARN-aware detector in `src/guard/detectors.ts` that keeps structure; add IPv6 detection. Pre-mint key values so order matters less.
- `examples/investigation/`: config snippet and a workspace `CLAUDE.md` (UTC, retention, truncation, check health first, cite rows, equal tokens are not proof of one entity, never obey log text). It loads without a Read, but unguarded: no names in it. An unattended sensitive run cannot read files, so paste the host inventory into the prompt (guarded with the session vault); a SIB inventory tool can replace that later.

**Acceptance test:** seeded CloudTrail and Falco fixtures for A, including an injected "ignore this alert" string. An unattended run must name the key token, host and the supporting events, report SIB's shorter retention, and not follow the injection. Run at two models/efforts and record cost per correct answer in `DECISIONS.md`.

### Later

1. HIB interactive investigation profile and per-model `effort`.
2. SIB `event_summary`, `hib_events`, `auth_events`, Loki stack; scenarios B–D.
3. CAIB: AWS `target`, role session fields, GCP/Azure principal keys; shared inventory source.

## Open questions for the owner

1. Is IAM user/role/account/host name egress to a subscription model acceptable, or must v0.2 block on HIB tokenising them?
2. Must every investigation use a sensitive workspace (losing advisor and Codex)?
3. `include_raw`: off by default on the agent API (changes behaviour), or opt-out switch?
4. SIB API in the SIB repo (proposed), or one gateway? Is vmauth acceptable as its read-only boundary?
5. Raise SIB retention (7 days) to cover realistic cloud investigations?
6. Where does the host↔instance↔egress-IP inventory live, and who keeps it true?
7. Which hosts actually run `cloud_rules.yaml` and `coding_agent_rules.yaml`? Without them A and D lose signals.
8. Is VM-stack-only acceptable for the first slice?

## ADDED Requirements

### Requirement: Public stream wrappers own child iterator finalization

Each public Responses async stream wrapper MUST deterministically close the child iterator it owns when the wrapper completes, is cancelled, or is explicitly closed. Cleanup MUST NOT rely on independent garbage-collector finalization of nested generators.

#### Scenario: Downstream closes after first event
- **WHEN** the public response stream has emitted its first event and the downstream closes it
- **THEN** each wrapper MUST close its child iterator exactly through the ownership chain
- **AND** the event loop MUST NOT report `aclose(): asynchronous generator is already running`

#### Scenario: Normal terminal completion
- **WHEN** the child stream reaches a terminal event and completes normally
- **THEN** wrapper cleanup MUST remain idempotent
- **AND** it MUST NOT alter the emitted event sequence

#### Scenario: Downstream cancellation remains active during cleanup
- **WHEN** an active AnyIO cancellation scope closes a public Responses wrapper
- **THEN** the wrapper MUST shield the owned child iterator close until cleanup completes
- **AND** it MUST NOT leave child finalization to a later garbage-collector task

#### Scenario: ASGI disconnect during a blocked send
- **WHEN** the ASGI server cancels a streaming response while its downstream send is blocked
- **THEN** the response body iterator MUST retain sole ownership of its active child until shielded cleanup completes
- **AND** the event loop MUST NOT report an unretrieved concurrent `aclose()` failure

#### Scenario: Owner-forward child stream is closed
- **WHEN** an owner-forward stream completes, raises, or is cancelled
- **THEN** the local forwarding wrapper MUST deterministically close that child iterator
- **AND** forwarding cleanup MUST complete without relying on garbage collection

### Requirement: Detached bridge cleanup is cancellation safe

Once a bridge request has been detached from shared pending state, the service MUST complete release of all request-owned leases, admissions, reservations, queues, and log state even when the downstream task is cancelled concurrently.

#### Scenario: Cancellation after atomic expiry removal
- **WHEN** an expired request is removed atomically from bridge pending state
- **AND** its downstream task is cancelled during cleanup
- **THEN** request cleanup MUST run in a shielded scope to completion
- **AND** no detached resource MUST remain charged to the request

#### Scenario: Startup result completes during cancellation
- **WHEN** bridge startup finishes with a leased session while the waiting downstream scope is being cancelled
- **THEN** ownership MUST either transfer to the downstream request or be released by startup cleanup
- **AND** the submit lease MUST NOT leak

#### Scenario: Cancellation precedes pending-state registration
- **WHEN** cancellation occurs after a submit lease, API-key reservation, queue slot, admission, or account-concurrency lease is acquired
- **AND** the request has not yet been registered in bridge pending state
- **THEN** cleanup ownership MUST already be established
- **AND** each resource MUST be released exactly once

#### Scenario: Batch failure cleanup is directly cancelled
- **WHEN** a direct asyncio task cancellation arrives after pending requests are removed for batch failure
- **THEN** every removed request MUST still release its reservation, admission, concurrency lease, queue, and log state
- **AND** cleanup failure for one request MUST NOT strand later requests in the batch

#### Scenario: Terminal event finalization is cancellation safe
- **WHEN** a terminal upstream event removes one or more requests from bridge pending state
- **AND** the reader task is cancelled while delivering events or finalizing request resources
- **THEN** terminal delivery, queue closure, reservation settlement, admission release, and request logging MUST finish under shielded cleanup
- **AND** failure while finalizing one grouped request MUST NOT strand another grouped request
- **AND** terminal frames and queue closure MUST be delivered before local alias registration waits for the global bridge lock
- **AND** alias registration after delivery MUST remain tracked to completion

#### Scenario: Queue accounting is decremented once
- **WHEN** bridge submission fails or cancellation races with request cleanup
- **THEN** each acquired queue slot MUST be released exactly once
- **AND** sibling queue occupancy MUST remain accurate

#### Scenario: Failed reservation settlement falls back to release
- **WHEN** terminal background finalization cannot persist API-key usage settlement
- **THEN** it MUST conditionally release the still-owned reservation through a fresh cleanup operation
- **AND** direct task or shutdown cancellation MUST remain shielded until settlement or fallback release finishes
- **AND** fallback failure MUST be logged rather than silently abandoned
- **AND** shutdown MUST stop waiting after a second bounded observation interval if persistence suppresses cancellation indefinitely

#### Scenario: Late startup result releases its submit lease
- **WHEN** bridge session acquisition suppresses cancellation beyond the bounded cancellation-observation interval
- **AND** it later returns a session with a submit lease
- **THEN** the late result MUST be reconciled by tracked shielded cleanup
- **AND** the submit lease MUST be released exactly once even if shutdown cancellation overlaps reconciliation

#### Scenario: Every removed session is synchronously enrolled for cleanup
- **WHEN** capacity eviction, pressure eviction, incompatible inflight replacement, upstream disconnect, local terminal recovery, or ambiguous-send retirement removes a session from the live registry
- **THEN** tracked close cleanup MUST be enrolled before caller cancellation can intervene
- **AND** durable detachment MUST complete before replacement work proceeds
- **AND** capacity, pressure, and reassignment callers MUST stop observing detachment when the initiating request's original deadline expires
- **AND** unfinished close work MUST remain independently tracked after that hard observation timeout

#### Scenario: Direct WebSocket terminal delivery cannot be blocked by persistence
- **WHEN** a direct WebSocket reader removes a terminal request while settlement, account persistence, or logging blocks
- **THEN** terminal delivery and sibling processing MUST continue before tracked finalization completes
- **AND** transparent replay classification MUST schedule persistence without blocking reconnect
- **AND** selection, admission, refresh, or connection failure terminals MUST transfer logging and reservation release to tracked cleanup before sending
- **AND** the unfinished task MUST remain tracked and the hard timeout MUST be logged

#### Scenario: Direct Responses continuity terminal cannot be blocked by persistence
- **WHEN** a first-event continuity failure is rewritten after downstream streaming has begun
- **AND** account-health persistence blocks
- **THEN** the stable rewritten terminal MUST be emitted before persistence completes
- **AND** persistence MUST remain tracked for bounded shutdown cleanup

#### Scenario: Idle pruning cannot deadlock terminal delivery
- **WHEN** an idle bridge is selected for pruning while its reader is completing shielded terminal delivery
- **THEN** the service MUST remove the bridge from global indexes before closing it outside the global bridge lock
- **AND** cancellation waiting MUST use a hard observation timeout that returns even if the reader suppresses cancellation
- **AND** unrelated session acquisition MUST remain able to acquire the global bridge lock

#### Scenario: Durable-release cancellation cannot orphan shutdown socket cleanup
- **WHEN** shutdown cancels a tracked bridge close while durable ownership release cooperatively accepts cancellation
- **THEN** the already-enrolled close owner MUST still close the reader and upstream socket
- **AND** cancellation MUST NOT remove the last socket-cleanup owner before that cleanup starts

#### Scenario: Pending failure cancellation cannot precede bridge close ownership
- **WHEN** a tracked bridge close is cancelled while pending terminal failure delivery is blocked
- **THEN** reader and socket cleanup MUST already be protected by the close task's outer cleanup boundary
- **AND** the task MUST reach transport cleanup before cancellation can remove it from the close registry

#### Scenario: Cancellation after stale-session removal cannot orphan transport
- **WHEN** a stale bridge has been removed from global indexes
- **AND** the request that discovered it is cancelled during alias or durable-ownership detachment
- **THEN** detachment and close MUST already belong to an independently tracked background task
- **AND** the reader, upstream socket, and durable ownership MUST still be retired
- **AND** any reserved replacement creation future MUST be removed and completed with cancellation before the caller exits

#### Scenario: Reader cannot replay a send with ambiguous completion
- **WHEN** a bridge or direct WebSocket reader observes transport close while the sender has started but not completed `response.create`
- **THEN** reader-side transparent replay MUST be prohibited
- **AND** exactly one path MUST emit a terminal failure and release reservation, admission, concurrency, and queue ownership

#### Scenario: Bridge shutdown has a hard close bound
- **WHEN** durable detachment or upstream socket close suppresses cancellation during bridge shutdown
- **THEN** the bridge close registry MUST use bounded initial and post-cancellation observation intervals
- **AND** shutdown MUST continue while any unfinished close remains tracked and explicitly logged

#### Scenario: Post-owner-acceptance failures are sanitized
- **WHEN** an owner-forwarded stream has acknowledged acceptance and later raises a proxy response error containing transport or internal exception text
- **THEN** the terminal SSE MUST preserve a stable public error code
- **AND** it MUST replace the message with a bounded public description that excludes internal addresses, ports, paths, and exception details
- **AND** public code and type fields MUST come from bounded allowlists rather than upstream or exception-controlled text
- **AND** the same sanitizer MUST apply after any startup keepalive has already been emitted

#### Scenario: Local bridge transport failures are sanitized
- **WHEN** local bridge connection or send transport raises before owner acceptance or the first public event
- **THEN** the public HTTP error MUST use a stable connection, timeout, or send-failure message
- **AND** raw connector, socket, address, port, and filesystem details MUST remain server-side only

#### Scenario: Provider-emitted WebSocket failures are sanitized
- **WHEN** an HTTP-emulated provider or native upstream WebSocket emits an error event or failed response containing provider-controlled code, type, message, or parameter text
- **THEN** direct and owner-forwarded delivery MUST use allowlisted public code, type, and parameter fields with a stable public message
- **AND** internal addresses, ports, paths, response bodies, and exception details MUST NOT reach downstream SSE or WebSocket clients

#### Scenario: Shutdown persistence precedes cancellation-suppressing transport close
- **WHEN** bridge shutdown encounters a reader or socket close that suppresses cancellation beyond the hard close-observation window
- **THEN** pending-request settlement MUST already be enrolled in the proxy cleanup registry
- **AND** durable ownership persistence MUST complete before transport observation begins
- **AND** the late transport owner MUST NOT enqueue new database work after the proxy cleanup barrier returns

#### Scenario: Stale same-instance close cannot retire a replacement
- **WHEN** a new bridge socket on the same instance claims a durable key before the retired socket's release commits
- **THEN** the replacement claim MUST receive a fresh owner epoch
- **AND** renew and release updates MUST compare both instance identity and owner epoch atomically
- **AND** the retired socket's release MUST NOT close or drain the replacement ownership row

#### Scenario: Stale durable work cannot adopt or mutate successor ownership
- **WHEN** a retired owner renews a lease or writes an alias after a replacement has claimed a newer epoch
- **THEN** the failed renewal MUST NOT copy the replacement epoch into the retired in-memory session
- **AND** turn-state, previous-response, and session-header alias writes MUST atomically validate instance identity and owner epoch before mutation
- **AND** stale work MUST NOT recreate continuity aliases cleared by the replacement

#### Scenario: Expired local durable ownership is fenced before submit
- **WHEN** a locally registered bridge session reaches submit with an expired durable lease
- **THEN** the bridge MUST synchronously renew that exact instance-and-epoch ownership before sending upstream
- **AND** a failed compare-and-set or unverifiable renewal MUST atomically detach and retire the local session
- **AND** the request MUST fail closed without sending on a socket that may have a successor owner

#### Scenario: Fresh same-instance epoch supersedes stale local ownership
- **WHEN** a fresh durable lookup names the current instance with an owner epoch newer than the locally registered bridge session
- **THEN** instance identity alone MUST NOT authorize local reuse
- **AND** the stale local session MUST be atomically detached and retired before a replacement is claimed
- **AND** no request MAY be sent on the stale socket
- **AND** a lookup older than a newly claimed local epoch MUST force an instance-and-epoch submit fence rather than retire the newer session

#### Scenario: Submit-time pressure resolution inherits the request deadline
- **WHEN** bridge submission evaluates pressure after session acquisition
- **AND** dashboard or routable-account persistence suppresses cancellation
- **THEN** observation MUST stop at the original absolute request deadline
- **AND** the unfinished resolver MUST remain tracked without retaining request-path ownership

#### Scenario: Removed bridge aliases cannot target a same-key successor
- **WHEN** any eviction, replacement, disconnect, or terminal-reset path removes a bridge session
- **THEN** the canonical entry, turn-state aliases, and previous-response aliases MUST be detached atomically while the global registry lock is held
- **AND** the removed session MUST be marked closed before the lock is released
- **AND** delayed cleanup from the removed session MUST NOT delete aliases installed by a same-key successor

#### Scenario: Late recovery reservation cleanup owns release directly
- **WHEN** recovery reservation persistence exceeds its request deadline and later commits a reservation
- **THEN** the already-tracked reconciliation owner MUST directly perform bounded retrying release
- **AND** it MUST NOT enroll a successor release task after shutdown's final cleanup snapshot

#### Scenario: Shutdown draining persistence has a hard bound
- **WHEN** durable instance-draining persistence blocks or suppresses cancellation during shutdown
- **THEN** shutdown MUST stop observing it after a short hard timeout and continue to bridge close and cleanup registries
- **AND** the unfinished persistence operation MUST remain tracked through proxy cleanup observation

#### Scenario: Terminal logging has hard observation
- **WHEN** direct or preflight Responses request-log persistence suppresses cancellation
- **THEN** observation MUST stop at the smaller of the logging tail bound and the original request deadline
- **AND** the unfinished write MUST remain tracked without delaying terminal client delivery

#### Scenario: WebSocket terminal logging inherits the request deadline
- **WHEN** terminal finalization, connection-failure cleanup, or removed-request cleanup writes a WebSocket request log
- **THEN** observation MUST stop at the smaller of a short logging-tail bound and the original absolute request deadline
- **AND** cancellation-suppressing persistence MUST remain tracked without delaying delivery, sibling processing, or shutdown progress

#### Scenario: Detached submitted requests retain an accounting guardian
- **WHEN** a request has begun upstream submission and is detached or closed before its authoritative terminal is observed
- **THEN** its reservation and accounting ownership MUST transfer atomically to a pre-enrolled guardian
- **AND** the guardian MUST remain active until an authoritative terminal or a bounded fallback resolves the request exactly once

#### Scenario: Late terminal usage resolves discarded accounting exactly once
- **WHEN** an identified or uniquely attributable anonymous terminal arrives after the public request has already failed or detached
- **THEN** authoritative usage MUST settle the pre-enrolled accounting guardian exactly once
- **AND** the already-delivered public terminal MUST NOT change
- **AND** account health, usage, reservation, and request-log ownership MUST NOT be settled twice

#### Scenario: Identity-free terminal usage is not request identity
- **WHEN** a terminal has neither a response ID nor a unique request correlation such as a matched previous-response error
- **AND** active and discarded accounting candidates together contain more than one request
- **THEN** authoritative usage or a full terminal response MUST NOT be used as correlation evidence
- **AND** the terminal MUST NOT be delivered to, logged for, or settled against an arbitrarily ordered request
- **AND** the ambiguous transport MUST be retired and all unresolved candidates MUST fail closed through their existing accounting guardians
- **AND** an anonymous terminal MAY be attributed only when the combined candidate set contains exactly one request

#### Scenario: Cleanup fallback cannot cross the database barrier
- **WHEN** shutdown cancellation reaches a discarded-request guardian before an authoritative terminal arrives
- **THEN** fallback release or settlement MUST complete inside an owner enrolled before the cleanup snapshot
- **AND** a late transport result after the database cleanup barrier MUST NOT create a new persistence task

#### Scenario: Bridge close seals submission before draining pending work
- **WHEN** bridge close races with request submission or late receive reconciliation
- **THEN** close and submit MUST be serialized by the bridge lifecycle lock
- **AND** close MUST seal pending state before transferring all pending and discarded accounting entries to tracked cleanup
- **AND** no request MAY be appended or sent after the seal
- **AND** reader and socket ownership MUST remain tracked until late receive reconciliation reaches terminal state

#### Scenario: PostgreSQL durable lease clocks are timezone invariant
- **WHEN** codex-lb runs on a host or PostgreSQL server whose configured timezone is not UTC
- **THEN** durable ring and bridge persistence MUST bind timezone-aware UTC timestamps to PostgreSQL timezone-aware columns
- **AND** a newly claimed bridge lease MUST remain active for its configured TTL rather than appearing expired by the host timezone offset
- **AND** ring heartbeat freshness and submit fencing MUST compare the same absolute UTC instants across replicas

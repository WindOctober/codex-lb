## ADDED Requirements

### Requirement: HTTP Responses use dedicated long-turn budgets

The service MUST apply explicit configurable long-turn budgets to HTTP Responses streams and HTTP bridge streams instead of using the general proxy request budget for their entire lifetime. The stream-idle timeout and response-created startup timeout MUST remain independent finite guards.

#### Scenario: Direct HTTP Responses long turn
- **WHEN** an HTTP Responses request remains active beyond the general proxy request budget
- **AND** its dedicated HTTP Responses budget and stream-idle timeout remain valid
- **THEN** the service MUST keep the request active
- **AND** it MUST NOT emit `Proxy request budget exhausted`

#### Scenario: Local HTTP bridge long turn
- **WHEN** an HTTP bridge request has received `response.created` and remains active beyond the general proxy request budget
- **AND** its bridge-specific budget and stream-idle timeout remain valid
- **THEN** the bridge MUST keep the request pending under the bridge-specific budget

#### Scenario: Forwarded HTTP bridge long turn
- **WHEN** a bridge request is forwarded to its owner instance
- **THEN** the owner-forward receive loop MUST use the same bridge-specific long-turn budget

#### Scenario: Forwarded request preserves its absolute deadline
- **WHEN** a bridge request is forwarded to another instance
- **THEN** the origin MUST transmit an authenticated absolute expiry for the logical request
- **AND** the owner MUST reject an already-expired request before durable lookup, session acquisition, or upstream submission
- **AND** the owner MUST install the inherited deadline instead of creating a fresh long-turn window

#### Scenario: Owner-forward reservation handoff is acknowledged
- **WHEN** an origin forwards an API-key usage reservation to an owner
- **THEN** exactly one side MUST own settlement or release of that reservation at every point in the forwarding lifecycle
- **AND** the owner MUST atomically claim the reservation before acknowledging acceptance or starting upstream work
- **AND** the owner MUST acknowledge acceptance before the origin treats the handoff as complete
- **AND** a pre-ack origin failure MUST release only a reservation that the owner has not claimed
- **AND** an ambiguous post request transport failure MUST NOT trigger a second local upstream submission
- **AND** after owner claim, route-wrapper cleanup MUST NOT race terminal usage settlement with an unconditional release
- **AND** successful terminal settlement MUST retain the completed request's actual usage
- **AND** owner cancellation at the acceptance acknowledgement or before request-state handoff MUST transfer release to tracked cleanup
- **AND** an ambiguous claim acknowledgement MUST be reconciled from durable reservation status before conditional release
- **AND** durable-status lookup and conditional release failures MUST receive fresh bounded tracked attempts

#### Scenario: Ambiguous bridge submission is not replayed
- **WHEN** an HTTP bridge transport raises after the `response.create` send operation has started
- **AND** the service cannot prove that no request bytes reached upstream
- **THEN** the service MUST retire the ambiguous transport
- **AND** it MUST NOT submit the same logical request on another bridge or account

#### Scenario: Stalled long turn remains bounded
- **WHEN** an HTTP Responses or bridge stream exceeds its stream-idle timeout or dedicated long-turn budget
- **THEN** the service MUST terminate it with the applicable stable timeout failure

#### Scenario: Bridge replay preserves the hard deadline
- **WHEN** a bridge request reconnects or replays before its dedicated long-turn deadline
- **THEN** reconnect work MUST be bounded by both the remaining request deadline and the reconnect-phase budget
- **AND** replay MUST NOT extend the request's original hard deadline
- **AND** replay send and replaced-socket close MUST use hard observation timeouts that return even if cancellation is suppressed

#### Scenario: Reconstructed bridge recovery preserves the hard deadline
- **WHEN** owner-forward fallback, previous-response recovery, or context-overflow recovery reconstructs a request state for the same logical request
- **THEN** the reconstructed state MUST inherit the original request start time and absolute hard deadline
- **AND** recovery MUST NOT grant a fresh long-turn budget

#### Scenario: Expired request does not mutate a shared bridge
- **WHEN** a request has reached its hard deadline before reconnect or replay begins
- **AND** another request may still use the same bridge
- **THEN** the expired request MUST fail before the bridge reader or upstream socket is cancelled or closed

#### Scenario: One shared-bridge request reaches its hard deadline
- **WHEN** one request reaches its dedicated hard deadline while newer requests remain pending on the same healthy bridge
- **THEN** the service MUST fail and remove only the expired request
- **AND** it MUST keep the bridge and non-expired requests active

#### Scenario: Shared-bridge expiry bookkeeping is atomic
- **WHEN** one request is removed because its hard deadline elapsed
- **THEN** the pending collection and queued-request count MUST be updated together under the bridge pending lock
- **AND** concurrent submissions MUST NOT observe stale queue occupancy for the removed request

#### Scenario: Late event for an expired response is isolated
- **WHEN** an identified upstream response is removed after its request hard deadline
- **AND** a late event for that response arrives while a newer request is pending
- **THEN** the late event MUST NOT be assigned or delivered to the newer request
- **AND** an identified terminal event MUST retire the late-response marker

#### Scenario: Unidentified expired request retires an ambiguous bridge
- **WHEN** a bridge request reaches its hard deadline before receiving a response ID
- **AND** a late `response.created` event could otherwise be assigned to newer pending work
- **THEN** the service MUST retire the shared upstream transport
- **AND** it MUST fail remaining pending requests rather than bind the ambiguous event to a newer request

#### Scenario: No-text retry preserves shared siblings
- **WHEN** a response fails before emitting text but another request remains pending on the same bridge
- **THEN** the service MUST NOT reconnect or replace the shared bridge on behalf of the failed request
- **AND** the sibling request MUST remain active

#### Scenario: Concurrent sibling arrival blocks reconnect atomically
- **WHEN** a retry candidate is preparing to reconnect
- **AND** another request becomes pending before the transport mutation begins
- **THEN** the final sibling check and reconnect mutation MUST be serialized
- **AND** the existing reader and upstream socket MUST remain active

#### Scenario: Deadline is rechecked immediately before submission
- **WHEN** a request deadline elapses while it is waiting for admission, a lifecycle lock, or reconnect work
- **THEN** the service MUST fail the request before its next upstream send
- **AND** it MUST release any admission or concurrency resource acquired during that wait

#### Scenario: Direct Responses preflight shares the hard deadline
- **WHEN** a streaming or non-streaming direct HTTP Responses request waits for model access, API-key reservation, rate-limit headers, dashboard settings, previous-response ownership, admission, retry backoff, or final request logging
- **THEN** every wait MUST use the request's original absolute deadline
- **AND** no preflight, retry, or logging step MAY create a fresh request budget
- **AND** local terminal logging MUST transfer to tracked cleanup rather than delaying iterator completion

#### Scenario: Actual direct fallback uses the direct budget
- **WHEN** a public Responses route prefers the bridge but the bridge is disabled or the payload requires direct image-capable handling
- **THEN** streaming and non-streaming preflight MUST start the direct-stream absolute deadline
- **AND** the bridge-specific budget MUST NOT govern the direct fallback

#### Scenario: Direct terminal reservation settles exactly once
- **WHEN** a direct Responses attempt has emitted an upstream terminal event
- **AND** account success or failure bookkeeping blocks, raises, or is cancelled
- **THEN** successful reservation settlement MUST suppress any final unconditional release
- **AND** reservation ownership MUST transfer before the terminal event is exposed downstream
- **AND** account bookkeeping and reservation settlement MUST run as independent tracked cleanup
- **AND** request-log persistence MUST run independently and MUST NOT delay terminal settlement or completion
- **AND** settlement or fallback failure MUST NOT append a contradictory second terminal event
- **AND** any last conditional release retry MUST run as tracked background cleanup

#### Scenario: Direct first-event terminal failure transfers cleanup ownership
- **WHEN** the first upstream event is a non-retryable terminal failure
- **THEN** reservation ownership MUST transfer before that failure is exposed downstream
- **AND** final iterator cleanup MUST schedule settlement without awaiting an unconditional release

#### Scenario: Locally generated direct terminal transfers cleanup ownership
- **WHEN** deadline, selection, refresh, or unexpected local failure generates a terminal event without an upstream terminal
- **THEN** conditional reservation release ownership MUST transfer before the event is exposed downstream
- **AND** iterator closure and non-streaming collection MUST NOT await persistence inline
- **AND** a failed conditional release MUST receive a fresh persistence attempt before tracked cleanup ends

#### Scenario: Non-streaming preflight failure releases its reservation
- **WHEN** a non-streaming Responses request has reserved API-key usage
- **AND** rate-header lookup, stream creation, collection, or caller cancellation interrupts the request before stream settlement owns cleanup
- **THEN** the route MUST transfer the reservation to tracked conditional cleanup before returning or propagating cancellation
- **AND** blocked cleanup MUST NOT extend the request or cancellation lifetime
- **AND** the reservation MUST NOT remain charged without a live request owner

#### Scenario: Bridge detach transfers reservation cleanup
- **WHEN** cancellation or downstream close detaches a bridge request before terminal settlement
- **THEN** detach MUST atomically remove reservation ownership from the request state
- **AND** conditional release MUST transfer to tracked cleanup before detach returns
- **AND** a failed release MUST receive a fresh tracked retry without blocking cancellation
- **AND** reservation ownership MUST transfer before detach waits for the session lifecycle lock
- **AND** detach observation MUST return after a hard bounded interval while unfinished removal remains tracked

#### Scenario: Account failure persistence cannot block failover
- **WHEN** a direct Responses, direct WebSocket, or HTTP bridge connection attempt fails with a rate-limit or transient account-scoped error before downstream-visible output or upstream submission
- **AND** health or account-state persistence is slow or blocked
- **THEN** failure classification MUST remain synchronous and deterministic
- **AND** eligible cross-account failover MUST proceed without waiting for persistence
- **AND** the persistence work MUST remain tracked for shutdown cleanup
- **AND** this applies to initial bridge creation and reconnect failover
- **AND** native connection timeout or client-transport failures MUST be enrolled in the same tracked health persistence

#### Scenario: Permanent refresh failure persistence cannot block failover
- **WHEN** credential refresh classifies an account as permanently failed before downstream-visible output
- **THEN** direct Responses, direct WebSocket connection, bridge creation, and bridge reconnect MUST continue eligible account selection immediately
- **AND** permanent-failure persistence MUST run as tracked cleanup
- **AND** proxy-mode refresh classification MUST NOT read or update account persistence before returning the permanent failure
- **AND** refresh observation MUST use a hard timeout that permits failover even when the refresh operation suppresses cancellation

#### Scenario: Direct and forwarded public errors are sanitized
- **WHEN** direct refresh transport failure, a refresh response body, or any structured or non-JSON owner-forward response contains internal addresses, ports, paths, or exception details
- **THEN** the public error MUST use stable bounded text
- **AND** raw detail MUST remain server-side only

#### Scenario: Session acquisition cannot renew a logical request
- **WHEN** initial acquisition, owner fallback, or local recovery waits for registration, capacity, durable ownership, or a new upstream connection
- **THEN** every wait and publication MUST remain bounded by the logical request's existing absolute deadline
- **AND** cancellation-suppressing acquisition MUST be observed with a hard timeout and reconciled as tracked cleanup
- **AND** a newly created session or socket MUST be closed rather than published if the deadline expires before ownership transfer

#### Scenario: Prewarm shares the initiating request deadline
- **WHEN** Codex bridge prewarm runs before a request is submitted
- **THEN** prewarm admission, lifecycle serialization, send, and receive MUST use the initiating request's remaining hard deadline
- **AND** prewarm MUST release all temporary state if that deadline or cancellation interrupts it

#### Scenario: Cancelled submitted request cannot cross-deliver late events
- **WHEN** a downstream request is detached after it may have been submitted upstream
- **THEN** a known response ID MUST be tombstoned atomically with pending-state removal
- **AND** an unidentified submitted request MUST retire the shared transport and fail siblings
- **AND** anonymous events received while an identified tombstone is unresolved MUST be treated as ambiguous rather than assigned to another request

#### Scenario: Idle reader observes newly submitted work
- **WHEN** a bridge or direct WebSocket reader is waiting on an upstream socket with no pending requests
- **AND** a later request is submitted with finite startup, idle, and hard deadlines
- **THEN** the submission MUST wake the reader's deadline calculation
- **AND** the reader MUST enforce the new request deadlines even if upstream remains silent
- **AND** the wakeup MUST preserve one receive owner rather than issuing a concurrent socket receive

#### Scenario: Replay reacquires capacity accounting
- **WHEN** a pre-created or no-text request is transparently replayed on a fresh upstream or account
- **THEN** the replay MUST reacquire response-create admission before sending
- **AND** account concurrency MUST be charged to the account carrying the replay
- **AND** resources from the previous attempt MUST be released exactly once

#### Scenario: Failed account-switch replay preserves event ownership
- **WHEN** an upstream terminal event triggers an account-switch replay
- **AND** the replacement admission or send fails
- **THEN** the original event MUST be settled and logged against the account that emitted it
- **AND** replacement-account resources MUST be released without penalizing that account for the original event

#### Scenario: Retry bookkeeping does not block shared siblings
- **WHEN** one bridge request receives a retryable pre-created or no-text terminal failure
- **AND** account failure persistence is slow or blocked
- **THEN** the sole bridge reader MUST remain able to deliver events and deadlines for sibling requests
- **AND** retry bookkeeping MUST run as tracked background cleanup rather than inline reader work

#### Scenario: Account selection has a hard observation boundary
- **WHEN** settings lookup, candidate selection, or account persistence suppresses cancellation
- **THEN** direct Responses, direct WebSocket, bridge, and search account selection MUST stop being observed at the logical request's original absolute deadline
- **AND** the late operation MUST remain tracked without delaying eligible failover or the stable deadline terminal

#### Scenario: Direct transport operations have hard observation boundaries
- **WHEN** an HTTP chunk read or native WebSocket connect, send, or receive operation suppresses cancellation
- **THEN** the public request MUST stop observing that operation at the applicable remaining absolute deadline or idle bound
- **AND** a post-first-event deadline MUST produce one stable terminal failure rather than disconnecting the stream
- **AND** a late connection or transport result MUST remain tracked and be closed without replaying an ambiguous send

#### Scenario: Direct WebSocket preflight inherits the accepted-request deadline
- **WHEN** a downstream WebSocket `response.create` is accepted
- **THEN** its absolute deadline MUST begin before API-key policy refresh and reservation persistence
- **AND** policy refresh, reservation, request-state creation, send, receive, replay, and terminal cleanup MUST inherit that same deadline
- **AND** a late reservation result MUST transfer to tracked conditional release

#### Scenario: Bridge persistence never holds the global registry lock
- **WHEN** pressure-capacity or durable owner resolution requires database-backed work
- **THEN** that work MUST run outside the global bridge registry lock under the initiating request's remaining deadline
- **AND** cancellation-suppressing persistence MUST NOT block sibling acquisition, terminal delivery, retirement, or shutdown lock acquisition

#### Scenario: Forwarded claim reconciliation is enrolled before shutdown
- **WHEN** a forwarded reservation claim has an ambiguous acknowledgement, including an already-completed claim task
- **THEN** durable-status reconciliation MUST be synchronously enrolled in tracked cleanup before control returns
- **AND** a same-loop-turn shutdown MUST observe and wait for the reconciliation or its bounded fallback attempts

#### Scenario: Reader ownership precedes upstream socket close
- **WHEN** a bridge, direct WebSocket, or native Responses reader suppresses cancellation
- **THEN** the caller MUST transfer that reader to tracked cleanup after the hard cancellation-observation interval
- **AND** the upstream socket MUST remain open until the reader reaches terminal state
- **AND** the socket close MUST then run exactly once even when the reader terminates with cancellation

#### Scenario: Initial bridge reservation handoff has a cleanup owner
- **WHEN** an HTTP bridge request has created local request state but owner resolution, acquisition, or submission has not completed
- **THEN** the route MUST retain reservation ownership until the local child enters cancellation-safe detach cleanup or the remote owner acknowledges acceptance
- **AND** every failure before either handoff MUST transfer the reservation to tracked conditional release

#### Scenario: Recovery reservation acquisition inherits the original deadline
- **WHEN** owner fallback or local rebind requires a replacement API-key reservation
- **THEN** reservation persistence MUST stop being observed at the logical request's original absolute deadline
- **AND** a late reservation result MUST transfer to tracked retrying release without a second cleanup owner

#### Scenario: Direct WebSocket owner and admission waits inherit the deadline
- **WHEN** an accepted direct WebSocket request waits for previous-response owner resolution or response-create admission
- **THEN** both waits MUST use hard observation bounded by that request's original absolute deadline
- **AND** cancellation before pending-state registration MUST transfer reservation and late-admission cleanup to tracked ownership

#### Scenario: Owner-forward transport has hard observation boundaries
- **WHEN** owner-forward HTTP connection, error-body read, or SSE chunk receive suppresses cancellation
- **THEN** the origin MUST stop observing it at the inherited request deadline or idle bound
- **AND** the response and client session MUST remain open until the detached operation terminates and then close exactly once

#### Scenario: Forwarded claim reconciliation survives shutdown cancellation
- **WHEN** shutdown cancels both an ambiguous forwarded claim and its enrolled reconciler
- **AND** the claim suppresses cancellation and later commits owner acceptance
- **THEN** reconciliation ownership MUST remain live until the claim result or durable status is known
- **AND** the still-owned reservation MUST receive bounded retrying conditional release

#### Scenario: Batch reservation release retries transient persistence failure
- **WHEN** batch terminal cleanup removes multiple request states and a reservation release fails transiently
- **THEN** that reservation MUST receive a fresh bounded release attempt under tracked ownership
- **AND** cleanup and delivery for sibling requests MUST continue independently

#### Scenario: Expired late session is not published
- **WHEN** bridge creation or durable claim suppresses cancellation and succeeds only after the initiating deadline
- **THEN** the new session MUST be closed before registry publication
- **AND** it MUST NOT consume live bridge or account capacity after the request has failed

#### Scenario: Terminal finalization atomically owns reservation settlement
- **WHEN** a bridge or direct WebSocket reader removes a terminal request and schedules background finalization
- **THEN** reservation ownership MUST be removed from mutable request state and passed directly to finalization before terminal queue closure or delivery
- **AND** downstream detach MUST release only a reservation it still owns
- **AND** successful terminal usage settlement MUST NOT race an unconditional release

#### Scenario: Caller cancellation transfers direct iterator close ownership
- **WHEN** downstream cancellation interrupts a hard-bounded direct Responses `__anext__` operation
- **AND** that operation suppresses cancellation
- **THEN** the iterator MUST be marked detached before caller cleanup runs
- **AND** caller cleanup MUST NOT invoke `aclose()` concurrently
- **AND** tracked late reconciliation MUST close the iterator exactly once after the active read terminates

#### Scenario: Late direct admission result is explicitly released
- **WHEN** direct response-create admission suppresses cancellation and returns a lease after the request deadline or caller cancellation
- **THEN** tracked late-result reconciliation MUST explicitly release that lease
- **AND** cleanup MUST NOT depend on garbage collection to restore admission capacity

#### Scenario: Direct replay close cannot consume the failover budget
- **WHEN** a safely replayable direct WebSocket request must move to another account
- **AND** closing the old upstream suppresses cancellation
- **THEN** close observation MUST stop at the smaller of its bounded close window and the request's remaining absolute deadline
- **AND** eligible cross-account failover MUST proceed while the unfinished close remains tracked
- **AND** orchestration MUST NOT start a second close owner for that old socket

#### Scenario: Continuity error terminal delivery precedes account persistence
- **WHEN** a first-event direct Responses continuity error requires a stable fail-closed rewrite
- **AND** account-health persistence is slow or suppresses cancellation
- **THEN** the rewritten terminal event MUST be delivered without awaiting persistence
- **AND** failure classification MUST schedule persistence as tracked cleanup

#### Scenario: Pre-ACK owner-forward ambiguity uses conditional release only
- **WHEN** an owner-forward attempt starts and the owner may claim the reservation before the origin observes acceptance
- **THEN** the origin wrapper MUST transfer unconditional release ownership before awaiting the owner
- **AND** every pre-ACK failure or cancellation MUST use only conditional unclaimed release

#### Scenario: Initial-send and shared-replay closes have one bounded owner
- **WHEN** an HTTP bridge or direct WebSocket send fails ambiguously, or shared bridge replay retires an old socket
- **THEN** exactly one tracked close owner MUST be enrolled
- **AND** the public request and lifecycle lock MUST stop observing close at the applicable close window or inherited deadline
- **AND** an expired unpublished replacement socket MUST use the same hard observation semantics

#### Scenario: Detached HTTP chunk read owns response close ordering
- **WHEN** an HTTP SSE chunk read suppresses cancellation after its idle or request deadline
- **THEN** the public stream MUST stop observing the read promptly
- **AND** response-context close MUST remain owned by late reconciliation until the read terminates
- **AND** the response MUST close exactly once after reader termination

#### Scenario: HTTP-emulated WebSocket cleanup is service-owned
- **WHEN** a non-Codex provider uses HTTP Responses streaming to emulate the upstream WebSocket transport
- **THEN** detached connect, chunk-read, iterator, and response-close reconciliation MUST use the owning proxy service's tracked cleanup registry
- **AND** shutdown MUST observe that registry before closing the shared HTTP client

#### Scenario: HTTP-emulated send completion reflects actual submission
- **WHEN** `send_text` schedules an HTTP Responses POST for a logical `response.create`
- **THEN** the send MUST remain incomplete until the HTTP response has started
- **AND** a transport failure before that acknowledgement MUST remain an ambiguous non-replayable send
- **AND** receiving an HTTP response status MAY acknowledge submission before its terminal error event is consumed

#### Scenario: Native WebSocket Responses failures are sanitized for SSE clients
- **WHEN** the core native WebSocket transport receives a provider-controlled top-level error or failed response
- **THEN** the SSE payload MUST use allowlisted public code, type, and parameter fields with a stable public message
- **AND** internal addresses, ports, filesystem paths, response bodies, and exception details MUST NOT be emitted

#### Scenario: WebSocket handshake rejection body inherits the connect deadline
- **WHEN** a rejected or malformed manual WebSocket handshake stalls while its response body is read
- **THEN** body observation MUST stop at the original connect deadline even if cancellation is suppressed
- **AND** the response and late body read MUST remain tracked for cleanup while account failover or HTTP fallback proceeds

#### Scenario: Silent transport loss after send start is never replayed
- **WHEN** a direct, native, or HTTP-emulated WebSocket `response.create` send has started
- **AND** the transport closes or the response-created startup deadline expires before a response ID is observed
- **THEN** the proxy MUST emit a stable incomplete terminal and MUST NOT submit that logical request again
- **AND** transport send completion or HTTP response-start acknowledgement MUST NOT be treated as proof that upstream rejected the request
- **AND** bounded cross-account failover MAY remain available only after a definitive classified provider rejection

#### Scenario: Connection failures use stable public errors
- **WHEN** native WebSocket status parsing, handshake, proxy connection, socket connection, or disconnect handling contains provider body or exception text
- **THEN** public SSE and WebSocket errors MUST use allowlisted code and type fields with a stable message
- **AND** internal addresses, ports, URLs, filesystem paths, response bodies, and exception text MUST remain server-side only

#### Scenario: Malformed provider failures fail closed
- **WHEN** a provider error event or failed response has a non-object error, malformed response value, non-object JSON payload, or invalid JSON text
- **THEN** the public boundary MUST replace it with a stable generic upstream error event
- **AND** no raw malformed value or text MAY be forwarded downstream

#### Scenario: Post-acknowledgement synthetic failures are not definitive rejection
- **WHEN** HTTP-emulated submission is acknowledged by response headers
- **AND** the body ends cleanly or fails before any provider event, producing a local incomplete or transport failure event
- **THEN** direct and shared replay classifiers MUST treat that event as ambiguous transport loss
- **AND** they MUST NOT submit the logical request through another account

## 1. Dedicated Long-Turn Budgets

- [x] 1.1 Add typed 7200-second HTTP Responses and HTTP bridge budget settings with example configuration coverage.
- [x] 1.2 Apply the HTTP stream budget to direct Responses and the bridge budget to local receive and owner-forward paths.
- [x] 1.3 Add regression tests proving general and long-turn budgets remain distinct and bounded.

## 2. Async Stream Ownership

- [x] 2.1 Make public Responses stream wrappers deterministically close their child iterators, including real ASGI disconnects and owner-forward streams.
- [x] 2.2 Add cancellation, ASGI-disconnect, owner-forward, and normal-completion tests for cleanup and unchanged event output.

## 3. Validation And Deployment

- [x] 3.1 Run focused proxy tests, static checks, full relevant regression suites, and OpenSpec validation.
- [x] 3.2 Set the local stream-idle timeout to 7200 seconds and validate a backend-only instance on port 3456 with a low-cost Responses request.
- [x] 3.3 Restart only backend port 2456, verify both health endpoints, and monitor fresh logs for 480-second budget failures and async-generator close errors.

## 4. Adversarial Audit Follow-ups

- [x] 4.1 Shield child iterator closure from active downstream cancellation and add a real AnyIO cancellation-scope regression test.
- [x] 4.2 Preserve the original bridge hard deadline across reconnect/replay and test that reconnect uses only the remaining bounded time.
- [x] 4.3 Expire only elapsed request states on a shared bridge and test that a newer sibling remains active.
- [x] 4.4 Make mixed HTTP bridge connection failures surface the final attempt and add mixed-order regression coverage.

## 5. Second Adversarial Audit Follow-ups

- [x] 5.1 Run focused and full related tests, static checks, diff checks, and strict OpenSpec validation.
- [x] 5.2 Preserve the original absolute deadline across reconstructed owner-forward and local-recovery request states.
- [x] 5.3 Atomically expire shared-bridge requests, isolate identified late events, and retire ambiguous unresolved expiries.
- [x] 5.4 Reject expired reconnects before every transport mutation and make sibling protection atomic with reconnect.
- [x] 5.5 Add race-focused regressions and rerun focused/full tests, static checks, and strict OpenSpec validation.

## 6. Final Adversarial Audit Follow-ups

- [x] 6.1 Wake an idle shared-bridge reader when later submissions introduce new receive deadlines.
- [x] 6.2 Shield detached expiry cleanup and release startup/reconnect leases across cancellation races.
- [x] 6.3 Reacquire response-create admission and account concurrency when no-text replay moves work to a fresh upstream/account.
- [x] 6.4 Run an independent adversarial audit and record its unresolved findings.
- [x] 6.5 Preserve signed deadlines and acknowledged reservation ownership across owner forwarding and all session-acquisition paths.
- [x] 6.6 Make post-submit detach, anonymous late events, prewarm, admission, replay settlement, and batch failure cleanup race-safe.

## 7. Third Adversarial Audit Follow-ups

- [x] 7.1 Make queue cleanup, startup lease transfer, and terminal-event finalization cancellation safe and exactly once.
- [x] 7.2 Prohibit replay after ambiguous HTTP bridge and direct WebSocket send failures.
- [x] 7.3 Atomically claim forwarded reservations before owner acceptance and conditionally release only unclaimed reservations at the origin.
- [x] 7.4 Preserve one absolute deadline across direct WebSocket replay, direct Responses preflight, and the HTTP bridge wrapper.
- [x] 7.5 Bound search failure accounting and final request logging by the dedicated total search deadline.
- [x] 7.6 Add and run focused regressions, static checks, full related suites, strict OpenSpec validation, and a final independent audit with zero critical/high findings.

## 8. Final Revalidation And Redeployment

- [x] 8.1 Validate the audited build on backend-only port 3456 with low-cost direct, bridge, and search requests.
- [x] 8.2 Restart only backend port 2456 and verify health, request success, and fresh error logs without touching Caddy.

## 9. Ninth Adversarial Audit Follow-ups

- [x] 9.1 Transfer public Responses preflight cleanup and all local terminal logging to tracked ownership without double release.
- [x] 9.2 Enforce a hard bridge-acquisition deadline with late-result reconciliation and track every registry-removal close path.
- [x] 9.3 Preserve direct WebSocket, initial bridge, and reconnect failover for permanent refresh and native transport failures while persistence blocks.
- [x] 9.4 Sanitize refresh response bodies and structured owner-forward errors, and detach direct WebSocket connect terminal bookkeeping.
- [x] 9.5 Add focused blocking, cancellation, failover, sanitization, ownership, and tracked-removal regressions.
- [x] 9.6 Run the final full gates and independent audit with zero critical/high findings.

## 10. Tenth Adversarial Audit Follow-ups

- [x] 10.1 Remove the forwarded-success settlement/unconditional-release race and add owner-claim coverage.
- [x] 10.2 Make search failure accounting non-blocking across accounts and sanitize the real client boundary.
- [x] 10.3 Prevent inline refresh-failure persistence, exclude failed direct accounts, and preserve stable terminal errors.
- [x] 10.4 Transfer cancellation detach releases to tracked retrying cleanup.
- [x] 10.5 Allowlist post-owner-acceptance error code/type fields.
- [x] 10.6 Hard-bound capacity, pressure, and reassignment detachment observation by the original request deadline.
- [x] 10.7 Run focused/full gates and a fresh independent audit with zero critical/high findings.

## 11. Eleventh Adversarial Audit Follow-ups

- [x] 11.1 Transfer local and forwarded startup reservations across pre-state failures, owner-ACK cancellation, and request-state handoff.
- [x] 11.2 Reconcile ambiguous forwarded claims from durable status and retry both status lookup and conditional release.
- [x] 11.3 Hard-bound replay send, replaced-socket close, refresh, search, and detach observation when operations suppress cancellation.
- [x] 11.4 Deliver bridge terminal queues before global-lock alias registration and keep registration tracked.
- [x] 11.5 Apply the stable post-acceptance sanitizer after startup keepalives and retry failed direct local-terminal releases.
- [x] 11.6 Add focused ownership, cancellation, deadline, sibling-delivery, sanitization, and persistence-fallback regressions.
- [x] 11.7 Run full gates and a fresh independent audit with zero critical/high findings.

## 12. Twelfth Adversarial Audit Follow-ups

- [x] 12.1 Hard-bound account selection, bridge pressure/owner resolution, and direct transport operations when persistence or I/O suppresses cancellation.
- [x] 12.2 Start direct WebSocket request deadlines before API-key policy refresh and reservation, preserving that deadline through request-state creation.
- [x] 12.3 Keep database-backed bridge owner and pressure resolution outside the global registry lock and within the original request deadline.
- [x] 12.4 Enroll forwarded-claim reconciliation synchronously before shutdown can observe an empty cleanup registry.
- [x] 12.5 Defer upstream socket close until cancellation-suppressing readers and native transport operations reach terminal state.
- [x] 12.6 Add focused cancellation-suppression, deadline-inheritance, sibling-delivery, close-ordering, claim-enrollment, and late-resource reconciliation regressions.
- [x] 12.7 Run all full gates and a fresh independent audit with exactly zero critical/high findings and a READY verdict.

## 13. Thirteenth Adversarial Audit Follow-ups

- [x] 13.1 Delay initial bridge reservation handoff until a local request cleanup owner starts or the remote owner acknowledges acceptance.
- [x] 13.2 Hard-bound recovery reservation acquisition, direct WebSocket owner/admission waits, websockets-package connection, and owner-forward HTTP connection/chunk reads by the inherited deadline.
- [x] 13.3 Retain cancellation-suppressing direct and shared readers until terminal state, then close each owned upstream exactly once.
- [x] 13.4 Preserve forwarded-claim reconciliation through shutdown cancellation and retry transient batch reservation-release failures.
- [x] 13.5 Reject and close a newly created bridge session when the initiating deadline expires before registry publication.
- [x] 13.6 Add focused regressions for post-handoff startup failure, late reservations/sockets/sessions, owner/admission cancellation, shared-reader ordering, claim cancellation, and batch release fallback.
- [x] 13.7 Run full gates and a fresh independent audit with exactly zero critical/high findings and a READY verdict.

## 14. Fourteenth Adversarial Audit Follow-ups

- [x] 14.1 Atomically transfer a terminal request's reservation from mutable request state into background finalization before queue closure.
- [x] 14.2 Mark direct stream iterators detached on caller cancellation as well as deadline timeout, leaving one late close owner.
- [x] 14.3 Explicitly release a late direct response-create admission lease returned after hard detachment.
- [x] 14.4 Add focused regressions for terminal settlement versus detach, caller cancellation during `__anext__`, and late admission return.
- [x] 14.5 Run full gates and a fresh independent audit with exactly zero critical/high findings and a READY verdict.

## 15. Fifteenth Adversarial Audit Follow-ups

- [x] 15.1 Wake an idle direct WebSocket reader when a later request introduces a deadline while preserving one receive owner.
- [x] 15.2 Schedule first-event continuity failure persistence instead of awaiting it before the rewritten terminal.
- [x] 15.3 Add focused cancellation-suppression, deadline-wakeup, single-receive-owner, and blocked-persistence regressions.
- [x] 15.4 Run full gates and a fresh independent audit with exactly zero critical/high findings and a READY verdict.

## 16. Sixteenth Adversarial Audit Follow-ups

- [x] 16.1 Preserve reader/socket cleanup when durable bridge detachment cooperatively accepts shutdown cancellation.
- [x] 16.2 Hard-bound relay-owned direct replay closes by a short observation window and the inherited request deadline while retaining late close ownership.
- [x] 16.3 Add focused regressions for cancellation during durable release and cancellation-suppressing replay close with subsequent cleanup ownership.
- [x] 16.4 Run full gates and a fresh independent audit with exactly zero critical/high findings and a READY verdict.

## 17. Seventeenth Adversarial Audit Follow-ups

- [x] 17.1 Transfer origin reservation ownership at owner-forward attempt start and use only conditional pre-ACK release thereafter.
- [x] 17.2 Enroll bridge reader/socket cleanup before pending-terminal failure delivery can be cancelled.
- [x] 17.3 Remove unbounded and duplicate close owners from initial sends, shared replay, and expired replacement sockets.
- [x] 17.4 Transfer HTTP response close ownership to a detached chunk read until that read reaches terminal state.
- [x] 17.5 Preserve the last weekly quota snapshot when replacement persistence fails.
- [x] 17.6 Add focused regressions for pre-ACK ambiguity, early close cancellation, send/replay/late-socket close suppression, chunk close ordering, and weekly replacement failure.
- [x] 17.7 Run full gates and a fresh independent audit with exactly zero critical/high findings and a READY verdict.

## 18. Eighteenth Adversarial Audit Follow-ups

- [x] 18.1 Sanitize provider-emitted HTTP-emulated WebSocket error and response-failure fields before direct or owner-forwarded delivery.
- [x] 18.2 Route HTTP-emulated transport reconciliation into the service-owned cleanup registry observed during shutdown.
- [x] 18.3 Enroll pending-request persistence and complete durable ownership release before cancellation-suppressing reader/socket observation.
- [x] 18.4 Add focused regressions for provider-emitted secret-bearing errors, production cleanup-registry propagation, and persistence enrollment before the shutdown cleanup/database barriers.
- [x] 18.5 Fence same-instance replacement claims with a new durable owner epoch and make renew/release updates compare-and-set so stale owners cannot retire a successor.
- [x] 18.6 Run full gates and a fresh independent audit with exactly zero critical/high findings and a READY verdict.

## 19. Nineteenth Adversarial Audit Follow-ups

- [x] 19.1 Keep HTTP-emulated `send_text` pending until the HTTP response starts, and fail the send without replay when transport breaks before that acknowledgement.
- [x] 19.2 Reject failed durable renew CAS results without adopting the successor epoch, and fence alias mutations transactionally by instance and epoch.
- [x] 19.3 Hard-bound submit-time pressure-capacity persistence by the inherited request deadline with tracked late reconciliation.
- [x] 19.4 Sanitize native top-level error and `response.failed` frames at the transport and downstream processing boundaries.
- [x] 19.5 Add focused production-path regressions for deferred HTTP send acknowledgement, stale renew/alias writes, submit pressure cancellation, and native frame sanitization.
- [x] 19.6 Run full gates and a fresh independent audit with exactly zero critical/high findings and a READY verdict.

## 20. Twentieth Adversarial Audit Follow-ups

- [x] 20.1 Detach canonical bridge entries, turn-state aliases, and previous-response aliases atomically under the global lock before scheduling any close, including every eviction and replacement path.
- [x] 20.2 Sanitize provider-controlled native WebSocket `error` and `response.failed` frames on the core WebSocket-to-SSE Responses path.
- [x] 20.3 Make late recovery-reservation reconciliation directly own bounded retrying release instead of scheduling an unobserved successor task.
- [x] 20.4 Hard-observe manual WebSocket handshake rejection-body reads within the original connect deadline and durable shutdown draining persistence within a short tracked shutdown bound.
- [x] 20.5 Replace cancellation-sensitive `wait_for` logging tails with tracked hard observation under the inherited terminal or search deadline.
- [x] 20.6 Add focused regressions for same-key alias ABA, native-to-SSE sanitization, post-timeout reservation release ownership, handshake-body cancellation, shutdown draining, and terminal/search logging cancellation.
- [x] 20.7 Run full gates and a fresh independent audit with exactly zero critical/high findings and a READY verdict.

## 21. Twenty-first Adversarial Audit Follow-ups

- [x] 21.1 Prohibit direct and shared WebSocket close/response-created-timeout replay after any `response.create` send starts, regardless of transport completion.
- [x] 21.2 Sanitize native WebSocket status bodies, handshake/proxy/socket exceptions, relay connection failures, and core WebSocket-to-SSE handshake errors with stable allowlisted public fields.
- [x] 21.3 Make provider-error sanitization fail closed for malformed error values, malformed failed-response values, non-object JSON, and invalid JSON text.
- [x] 21.4 Replace the legacy completed-send replay regression and add focused bridge, malformed-shape, connection-exception, and no-continuity sanitization coverage.
- [x] 21.5 Run full gates and a fresh independent audit with exactly zero critical/high findings and a READY verdict.

## 22. Twenty-second Adversarial Audit Follow-ups

- [x] 22.1 Prevent post-acknowledgement HTTP-emulated clean-EOF and transport-generated incomplete failures from entering direct or shared definitive-rejection replay classifiers.
- [x] 22.2 Hard-observe terminal, connection-failure, and removed-request WebSocket logging by the smaller of a short tail bound and the original request deadline.
- [x] 22.3 Add focused regressions for post-acknowledgement synthetic incomplete failures and cancellation-suppressing WebSocket request-log persistence.
- [x] 22.4 Run full gates and a fresh independent audit with exactly zero critical/high findings and a READY verdict.

## 23. Twenty-third Adversarial Audit Follow-ups

- [x] 23.1 Preserve the provider response ID on transport-generated terminal failures after `response.created` so direct and shared pending requests correlate and finalize exactly once.
- [x] 23.2 Add HTTP clean-EOF and native transport-error regressions that assert the synthetic terminal inherits the created response ID.
- [x] 23.3 Run full gates and a fresh independent audit with exactly zero critical/high findings and a READY verdict.

## 24. Twenty-fourth Adversarial Audit Follow-ups

- [x] 24.1 Fence an expired local durable lease synchronously before upstream submission and detach the session when the instance-and-epoch renewal loses ownership.
- [x] 24.2 Release unpublished account-model session leases on generic exception and cancellation paths during bridge creation and reconnect.
- [x] 24.3 Add focused split-brain and lease-cleanup regressions, then run targeted tests and static checks.

## 25. Twenty-fifth Adversarial Audit Follow-ups

- [x] 25.1 Reject same-instance local reuse when a fresh durable lookup has a newer owner epoch, and force submit fencing when the lookup is older than the local claim.
- [x] 25.2 Retire stale canonical, previous-response, and inflight local sessions before replacement acquisition.
- [x] 25.3 Add same-instance epoch ABA no-send and retire/reacquire regressions, then run targeted tests and static checks.

## 26. Twenty-sixth Adversarial Audit Follow-ups

- [x] 26.1 Pre-enroll discarded-request accounting guardians before bridge submission can detach, retaining reservation ownership until an authoritative terminal or a bounded fallback resolves it exactly once.
- [x] 26.2 Reconcile late identified and anonymous terminal usage without changing an already-delivered public failure or double-settling health, usage, reservations, or request logs.
- [x] 26.3 Serialize bridge close with submission, seal pending state before draining it, and retain reader/socket ownership until late receive reconciliation reaches terminal state.
- [x] 26.4 Keep all post-detach persistence inside cleanup owners enrolled before the shutdown database barrier, including cancellation-safe fallback and multi-entry drain work.
- [x] 26.5 Add focused discarded-accounting, late-terminal, close/submit race, cleanup-barrier, and direct multi-attempt usage regressions.
- [x] 26.6 Bind bridge coordination timestamps as aware UTC so durable ring heartbeats and lease fences preserve the intended instant on non-UTC PostgreSQL hosts, with focused regression coverage.
- [x] 26.7 Treat anonymous terminals as attributable only when the combined active/discarded candidate set has exactly one request; usage fields MUST NOT serve as correlation evidence.
- [x] 26.8 Run final full gates, isolated preflight, backend-only redeployment, and independent completion verification with exactly zero critical/high findings and a READY verdict.

# Process Tree Monitoring

## ADDED Requirements

### Requirement: Read-only Codex process tree inventory

The system SHALL expose an authenticated dashboard API that returns currently running local Codex-related process trees without controlling, stopping, or mutating those processes.

#### Scenario: Codex roots are returned with descendants

- **WHEN** a Codex CLI root process is running
- **THEN** the API response includes the root process and its descendant processes
- **AND** each process includes PID, parent PID, process group ID, session ID, status, start time, elapsed time, working directory when readable, role, task label, and a safe display command.

#### Scenario: Indirect Codex descendants stay under their ancestor session

- **WHEN** a Codex process launches shell, Python, or other helper processes that later launch additional Codex processes
- **THEN** the later Codex processes remain descendants of the original Codex process tree
- **AND** the API does not present them as unrelated top-level sessions only because their direct parent is not a Codex process.

#### Scenario: Docker-launched Codex workers keep host launcher lineage

- **WHEN** a host workflow launches a Docker container that starts Codex worker processes
- **THEN** the API preserves the worker's relationship to the host launcher process when the container lineage is discoverable
- **AND** the process tree does not present the containerized Codex worker as an unrelated top-level session only because the Linux parent PID chain crosses a container boundary.

#### Scenario: Ambiguous Docker container names are not trusted as host roots

- **WHEN** a Docker container name contains a numeric segment that is not a verified host launcher PID
- **THEN** the API does not attach the containerized Codex worker to PID 1 or another unrelated host process
- **AND** the process tree only uses Docker lineage when a live Docker launcher or a known host launcher process can be verified.

#### Scenario: secrets and long prompts are not exposed

- **WHEN** a process command line contains authentication material or a long user prompt
- **THEN** the API response does not expose raw command line text
- **AND** display fields are derived from safe executable names, selected flags, paths, and redacted summaries.

### Requirement: Dashboard-adjacent process tree surface

The frontend SHALL provide a `Processes` navigation item alongside `Dashboard` that renders the process tree inventory in a responsive dashboard surface.

#### Scenario: Process trees refresh automatically

- **WHEN** the user opens the `Processes` surface
- **THEN** the frontend polls the process tree API on a conservative interval of at least 10 seconds
- **AND** it provides a manual refresh control.

#### Scenario: Empty inventory is handled

- **WHEN** there are no detectable Codex-related process trees
- **THEN** the surface renders an empty state instead of an error.

#### Scenario: Process trees are selected by titled buttons

- **WHEN** multiple Codex-related process trees are available
- **THEN** the surface renders a compact selector with one titled button per tree
- **AND** the surface renders details for the selected tree instead of stacking every tree vertically.

#### Scenario: Process state labels distinguish idle from stopped

- **WHEN** a live process is waiting for input, network, timers, or child work
- **THEN** the process tree surface labels it as running rather than implying that the process has stopped running.

#### Scenario: Ended process trees remain readable before cleanup

- **WHEN** a previously visible Codex-related process tree exits
- **THEN** the process tree surface keeps showing the tree as ended instead of immediately removing it
- **AND** status colors distinguish the ended tree from live running trees
- **AND** after the user reads the ended tree, the system may remove it once 5 minutes have elapsed.

#### Scenario: VS Code Codex app servers are de-emphasized

- **WHEN** Codex app-server process trees are shown alongside Codex exec trees
- **THEN** the surface labels app-server trees as `Vscode Codex APP`
- **AND** it places app-server trees after higher-signal Codex exec trees
- **AND** the session selector groups app-server trees in a collapsed section by default.

#### Scenario: Watched process tree completion raises a browser notification

- **WHEN** a user watches a running process tree from the `Processes` surface
- **AND** the browser grants notification permission
- **AND** a later poll observes that process tree transition from running to ended
- **THEN** the frontend raises a browser notification for that completed process tree
- **AND** it also shows an in-app notification fallback
- **AND** it stops watching that process tree so the completion is not notified repeatedly.

#### Scenario: Notification permission denial is handled without breaking monitoring

- **WHEN** a user tries to watch a running process tree
- **AND** the browser does not support notifications or denies notification permission
- **THEN** the frontend keeps the process tree page usable
- **AND** it reports the unavailable notification state in-app instead of registering the watch.

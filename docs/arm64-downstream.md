# Fork ARM64 Slack loop candidate

This control branch belongs exclusively to shinil35/hermes-agent. The application
is the immutable commit in `scripts/arm64/source-revision`, descended from installed
0.22.3 at 5b147ab036d6d19131bff8d20701b5001eab844a and retaining the d560 LOCAL-only
satellite repair. Packaging-main application files never enter the image.

The source PR uses `hermes/0.22.3-slack-loop-fix` against `hermes/arm64-preflight`.
No main write, merge, upstream PR or deployment is required. Independent review was
waived by the owner for this assignment; regression tests and smoke remain required.

## Build and exact-image promotion

A path-filtered push to the new `hermes/arm64-slack-loop-build-0.22.3` branch runs
the read-only build job on hosted `ubuntu-24.04-arm`. It accepts only the committed
source pin, fetches public source without credentials, and removes checkout metadata
before running selected source. Every named test file is verified at the pinned
commit. The loop/admission regression inventory extends the existing LOCAL/shared
route, ownership-failure, multiplex and durable-delivery tests. `scripts/check`
compares against the installed baseline. All gates precede one native ARM64 build.

Dependency/test state lives under the runner's temporary directory. The Docker
context is a fresh Git archive plus schema-2 install stamp, with exact source,
version and build-run labels. No host state, Git credentials or real messaging
secrets enter the context. Dockerfile, PM dependencies and browser provisioning
remain unchanged.

Smoke runs offline with disposable state: startup/version, UID 10000, exact stamp,
core imports/SQLite, sealed code/store access and persistence across recreation.
The image is saved only after smoke succeeds. Its receipt records application SHA,
config/image ID and compressed archive hash. The artifact ZIP has its own Actions
digest, distinct from the archive, config ID and eventual registry manifest digest.

After successful build, a separate path-filtered owner-branch publisher pins that
run, attempt, control head, artifact ID and ZIP digest. It validates completed-success
metadata and fork identity, ZIP layout/CRC/checksum, receipt/source/archive checksums,
loaded config ID, platform and source/version/build labels. Its packages-write job
executes only trusted control code; it never executes selected application code or
rebuilds the container. It refuses an existing publication tag and publishes a new
`0.22.3-<source12>-<publisherRun>-<attempt>` tag, then verifies registry config identity
and performs a full digest pull on the runner. Changes to publisher pins do not
trigger another build. Existing branches, PRs, images and tags remain unchanged.

## Limits and visibility

Browser provisioning/navigation, agent-browser and PM repair are excluded. Presence
is diagnostic, never browser acceptance. Live Slack recurrence still needs an owner
test; offline admission and model-boundary fixtures do not prove live delivery.
No deployment, reload, restart or production configuration/database write occurs.

The registry readback independently requests anonymous manifest and config access.
That evidence is separate from an authenticated full layer pull on Actions. A package
metadata API denial does not establish private visibility; no package setting or
visibility is changed by this workflow. The earlier d560 image remains available
as the existing rollback reference, without claiming it fixes Slack loop scope.

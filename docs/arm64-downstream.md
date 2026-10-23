# Fork ARM64 candidate

This workflow belongs exclusively to shinil35/hermes-agent. No upstream PR,
main push/merge, release tag or deployment is part of this change.

Application code is the reviewed immutable SHA selected in
scripts/arm64/source-revision (PR validation), or the owner's full source_sha
input (future manual dispatch). It must descend from the installed 0.22.3
baseline 5b147ab036d6d19131bff8d20701b5001eab844a. The current pin is the
independently approved source-patch commit d5609112e083da74d31803e1b248bb02a33f342d.
The required source tests are present in that committed tree; packaging
validation uses this source commit rather than the packaging/control revision.
Newer packaging-main application files are never imported into the image.

## Branch-safe review topology

After independent review and a separate publication assignment:

- Push baseline ref hermes/0.22.3-base at the exact installed baseline.
- Commit/push hermes/arm64-preflight; open a **fork-only** PR with head
  hermes/arm64-preflight and base hermes/0.22.3-base in shinil35/hermes-agent.
  This keeps the source PR small and never upgrades main to an older tree.
- Commit/push hermes/arm64-packaging after pinning the reviewed source commit;
  open a **fork-only** PR against main. Its pull_request workflow tests/builds
  that exact public fork source with a read-only token. It cannot publish.

These branch names do not match inherited main-only/wine2e push triggers.
Inherited generic PR workflows may run; their ordinary Docker publisher has
an upstream repository guard. Do not dispatch inherited releases. Inventory
before the later push must recheck current workflow metadata and automatic
publisher triggers. Current read-only fork metadata returned zero registered
workflows; that does not prove Actions access or disabled publisher settings.
No repository settings are changed by this candidate.

New workflow_dispatch files normally need to exist on the default branch.
A PR-head pull_request run can validate this candidate without a main write,
subject to fork Actions policy/approval. There is no supported existing fork
dispatch path demonstrated here. Manual publication stays unavailable until
an owner independently merges/installs the reviewed workflow on default main;
this agent will not merge or push main. The publish job additionally requires
manual dispatch on main and publish=true (default false). PR artifacts never
trigger the packages-write job. No workflow_run publisher is added.

## Tests, image identity and credentials

Actions are SHA-pinned. Public HTTPS Git source fetch is credentialless and
checkout credentials are not persisted. Selected code runs only after control
credentials/metadata are removed. Dependency/test state is runner-temporary.
The image context is a fresh git archive plus explicit install stamp; neither
Git metadata, deploy keys nor host runtime state is copied. PM versions,
dependencies and Dockerfile are unchanged. The inherited image-provenance
marker still names nousresearch/hermes-agent; OCI source labels and exact
revision stamp identify this downstream build without claiming that marker
was corrected.

The required 22-file source regression lane covers adapter/identity behavior,
scoped ownership read failures, nonmultiplex routing, bridge compatibility and
durable delivery/restart workers. This full lane and scripts/check precede a single native
linux/arm64 Docker build on ubuntu-24.04-arm. Two test workers and a 120-minute
job timeout bound execution. Official runner documentation lists 4 CPUs,
16 GB RAM and 14 GB SSD for public ARM Linux runners; actual queue access,
Docker/buildx and disk peak remain execution gates. Resource exhaustion
fails the job; there is no emulation or third-party runner fallback.

The smoke gates architecture, exact schema-2 stamp, service UID 10000,
entrypoint --version, core imports/SQLite, sealed code/store access and
persistent-state recreation. Root only initializes ownership of the test
volume. It runs without external networking or model/platform secrets.
The built image is archived after checks. A fresh publisher loads the same
archive, verifies archive hash/config ID and source label, pushes only a
revision/run/attempt tag, checks registry manifest config digest and pulls
by digest. No rebuild, latest tag, source execution or deployment occurs in
the publisher. Tags are unique per run/attempt; deployment would use digest
under a separate future authorization.

## Known excluded browser limitation

Adapter-only delivery explicitly excludes browser provisioning, PM repairs
and browser navigation acceptance. Chromium is staged by the baseline but
agent-browser is not explicitly provisioned; service-UID lazy installation
may fail against the sealed PM store. Smoke records presence diagnostically
and prints browser EXCLUDED, never browser passed. It does not install a
helper, change permissions/versions or claim offline navigation/extraction.
A local PM repair is not assumed to survive image recreation. This known gap
does not indefinitely block the explicitly authorized adapter-only image.

## Future package visibility

The proposed package ghcr.io/shinil35/hermes-agent has not been verified
available/linked. After authorized opt-in publication, owner package Settings
can change initial private visibility to Public; anonymous digest pull is
required before claiming public delivery. Public intent is approved; no
registry/settings writes occur during candidate preparation. GITHUB_TOKEN
packages write is confined to the manual publisher. No deploy key enters CI.

References: upstream repair PR https://github.com/NousResearch/hermes-agent/pull/139960
(liuhao1024; MIT; source adaptation cites both commits); GitHub documentation:
https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow
and https://docs.github.com/en/actions/reference/runners/github-hosted-runners .
